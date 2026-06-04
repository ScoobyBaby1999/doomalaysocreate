from __future__ import annotations
import asyncio
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from content.roles import looks_like_refusal, make_prompt
from merge import merge_critiques
from oplog import log_event
from providers import (
    make_model_family,
    make_model_role,
    make_provider_registry,
    make_slot_registry,
    provider,
    slot,
)
from scheduler import ProviderError, SlotScheduler, call_slot

# ---------------------------------------------------------------------------
# loom's multi-model critique panel, exposed as a slim standalone service.
#
#   POST /api/critique   (Bearer-token guarded)
#     { "plan": "<markdown OR loom schematic JSON>",
#       "format": "auto"|"markdown"|"schematic",
#       "panel":  ["provider/model", ...]   (optional, defaults to panel.json),
#       "rubric": "<optional inline rubric override>" }
#
# fans the plan out to a diverse judge panel in parallel (reusing loom's
# SlotScheduler/call_slot for per-provider pacing + rate-limit handling), then
# merges the critiques deterministically. one judge failing never fails the call.
#
# see HANDOFF.md (sections 1, 5, 7) for the full contract this is built to.
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
PANEL_PATH = Path(os.environ.get("PANEL_PATH", HERE / "panel.json"))

# per-judge directives spliced into the rubric's {{INSTRUCTIONS}} slot. the
# universal checks live in the .md rubrics; these focus the judge on a *plan*.
MARKDOWN_INSTRUCTIONS = (
    "You are reviewing a PLAN — a proposal, design, or strategy — not a finished "
    "article. Judge whether the plan is sound, complete, and executable. Look for: "
    "unstated assumptions, missing or out-of-order steps, undefined success criteria, "
    "risks and edge cases left unaddressed, infeasible or hand-wavy steps, scope that "
    "is too broad to execute, and decisions presented without justification. Flag the "
    "places where this plan would fail or stall in practice."
)
SCHEMATIC_INSTRUCTIONS = (
    "Judge whether this schematic will actually execute and produce the intended "
    "artifact. Concentrate on the concrete wiring between stages — inputs that no "
    "prior stage produces, fanout over non-list keys, destructive transformer "
    "stitches, role misuse, and ordering/dependency mistakes."
)
OUTPUT_RULES = (
    "- Reference the specific part of the input each issue is about.\n"
    "- Each bullet: exactly one issue paired with one concrete fix.\n"
    "- Prioritise high-leverage problems; do not pad with nitpicks or filler.\n"
    "- If the plan is genuinely strong, still surface its 1-3 weakest points."
)

CRITIQUE_MAX_TOKENS = int(os.environ.get("CRITIQUE_MAX_TOKENS", "1500"))
JUDGE_TIMEOUT_S = float(os.environ.get("JUDGE_TIMEOUT_S", "180"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1_000_000)))


class Panel:
    """immutable-ish view of the configured judge panel + resolvable slots."""

    def __init__(self) -> None:
        self.providers: list[provider] = make_provider_registry()
        self.provider_by_name: dict[str, provider] = {p.name: p for p in self.providers}
        base_slots = make_slot_registry(self.providers)
        self.slot_by_who: dict[str, slot] = {s.who: s for s in base_slots}

        cfg = _load_panel_cfg()
        self.default_panel: list[str] = cfg["judges"]
        self.max_parallel: int = cfg["max_parallel"]
        self.default_rubric: str = cfg["rubric"]

        #   register any panel-named model that isn't in the base catalog, against
        #   its provider's key. this is what lets panel.json name newer models
        #   (glm-5.1, the largest nemotron) with zero code changes.
        for who in self.default_panel:
            self._ensure_slot(who)

    def _ensure_slot(self, who: str) -> slot | None:
        if who in self.slot_by_who:
            return self.slot_by_who[who]
        if "/" not in who:
            return None
        prov_name, model = who.split("/", 1)
        prov = self.provider_by_name.get(prov_name)
        if prov is None:
            #       provider not configured (no api key) - judge will report missing.
            return None
        synth = slot(
            provider=prov,
            model=model,
            model_family=make_model_family(model),
            roles=make_model_role(model),
        )
        self.slot_by_who[who] = synth
        log_event("panel_slot_registered", slot=who, provider=prov_name, model=model)
        return synth

    def resolve(self, who: str) -> slot | None:
        return self._ensure_slot(who)


def _load_panel_cfg() -> dict:
    #   tolerant loader: strips // line comments so panel.json can stay annotated,
    #   accepts judges as bare "prov/model" strings or {"slot": ...} objects.
    raw = PANEL_PATH.read_text(encoding="utf-8")
    no_comments = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("//")
    )
    cfg = json.loads(no_comments)
    judges_raw = cfg.get("judges", [])
    judges: list[str] = []
    for entry in judges_raw:
        if isinstance(entry, str):
            judges.append(entry)
        elif isinstance(entry, dict) and entry.get("slot"):
            judges.append(entry["slot"])
    return {
        "judges": judges,
        "max_parallel": int(cfg.get("max_parallel", len(judges) or 1)),
        "rubric": cfg.get("rubric", "critiquer"),
    }


def detect_format(plan: str) -> tuple[str, dict | None]:
    #   loom schematic = JSON object carrying 'stages' or 'task_type'. anything
    #   else is treated as a markdown plan.
    text = plan.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return "markdown", None
        if isinstance(obj, dict) and ("stages" in obj or "task_type" in obj):
            return "schematic", obj
    return "markdown", None


def build_system_prompt(fmt: str, plan: str, inline_rubric: str | None,
                        rubric_name: str) -> str:
    if inline_rubric:
        #       inline override: use it verbatim as the system prompt, with the
        #       plan appended so the judge still sees what it is reviewing.
        return f"{inline_rubric.strip()}\n\n## Plan under review\n{plan}"
    if fmt == "schematic":
        return make_prompt("schematic_critiquer", instructions=SCHEMATIC_INSTRUCTIONS,
                           output_rules=OUTPUT_RULES, inputs=plan)
    return make_prompt(rubric_name, instructions=MARKDOWN_INSTRUCTIONS,
                       output_rules=OUTPUT_RULES, inputs=plan)


async def _run_one_judge(client: httpx.AsyncClient, scheduler: SlotScheduler,
                         picked: slot, system_prompt: str,
                         sem: asyncio.Semaphore) -> dict:
    async with sem:
        await scheduler.wait_for_provider_pacing(picked)
        try:
            content = await call_slot(
                client, picked,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Produce the requested critique now."},
                ],
                max_tokens=CRITIQUE_MAX_TOKENS,
                timeout_s=JUDGE_TIMEOUT_S,
            )
        except ProviderError as e:
            err = str(e)
            code = err.split(":", 1)[0]
            scheduler.record_failure(picked, code, reason=err[:400])
            return {"model": picked.who, "ok": False, "error": err[:300]}

        text = content.strip()
        if looks_like_refusal(text):
            scheduler.record_failure(picked, "refusal", reason=text[:160])
            return {"model": picked.who, "ok": False, "error": f"refusal: {text[:160]!r}"}

        scheduler.record_success(picked)
        return {"model": picked.who, "ok": True, "critique": text}


async def run_critique(panel: Panel, plan: str, fmt_req: str,
                       panel_override: list[str] | None,
                       inline_rubric: str | None) -> dict:
    t0 = time.monotonic()

    if fmt_req == "markdown":
        fmt, _ = "markdown", None
    elif fmt_req == "schematic":
        fmt = "schematic"
    else:
        fmt, _ = detect_format(plan)

    system_prompt = build_system_prompt(fmt, plan, inline_rubric, panel.default_rubric)

    who_list = panel_override or panel.default_panel
    picked_slots: list[slot] = []
    missing: list[dict] = []
    for who in who_list:
        s = panel.resolve(who)
        if s is None:
            missing.append({"model": who, "ok": False,
                            "error": "not registered (provider has no API key configured)"})
        else:
            picked_slots.append(s)

    results: list[dict] = []
    if picked_slots:
        scheduler = SlotScheduler(picked_slots)
        sem = asyncio.Semaphore(max(1, panel.max_parallel))
        async with httpx.AsyncClient() as client:
            tasks = [
                _run_one_judge(client, scheduler, s, system_prompt, sem)
                for s in picked_slots
            ]
            results = await asyncio.gather(*tasks)

    judges = results + missing
    merged = merge_critiques(judges)
    ok_count = sum(1 for j in judges if j.get("ok"))

    elapsed = round(time.monotonic() - t0, 2)
    log_event("critique_done", format=fmt, judges_total=len(judges),
              judges_ok=ok_count, elapsed_s=elapsed)
    return {
        "format_detected": fmt,
        "judges": judges,
        "merged": merged,
        "meta": {"elapsed_s": elapsed, "judges_ok": ok_count, "judges_total": len(judges)},
    }


# --- HTTP layer -------------------------------------------------------------

def _token_ok(header_value: str | None) -> bool:
    expected = os.environ.get("CRITIQUE_TOKEN", "").strip()
    if not expected:
        return False  # never serve an open endpoint - require the secret to be set
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value[len("Bearer "):].strip()
    return hmac.compare_digest(presented, expected)


class Handler(BaseHTTPRequestHandler):
    server_version = "loom-critique/1.0"
    panel: Panel  # injected on the server instance

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - quiet default logging
        return  # telemetry goes through oplog; suppress the stderr access log spam

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("", "/health"):
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            self._send_json(200, {
                "service": "loom critique panel",
                "status": "ok",
                "token_required": True,
                "token_configured": bool(os.environ.get("CRITIQUE_TOKEN", "").strip()),
                "providers_configured": [p.name for p in panel.providers],
                "default_panel": panel.default_panel,
                "default_rubric": panel.default_rubric,
                "endpoint": "POST /api/critique",
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/api/critique":
            self._send_json(404, {"error": "not found"})
            return

        if not _token_ok(self.headers.get("Authorization")):
            if not os.environ.get("CRITIQUE_TOKEN", "").strip():
                self._send_json(503, {"error": "CRITIQUE_TOKEN not configured on server"})
            else:
                self._send_json(401, {"error": "missing or invalid bearer token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return

        plan = payload.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            self._send_json(400, {"error": "'plan' (non-empty string) is required"})
            return

        fmt_req = payload.get("format", "auto")
        if fmt_req not in ("auto", "markdown", "schematic"):
            self._send_json(400, {"error": "'format' must be auto|markdown|schematic"})
            return

        panel_override = payload.get("panel")
        if panel_override is not None and (
            not isinstance(panel_override, list)
            or not all(isinstance(x, str) for x in panel_override)
        ):
            self._send_json(400, {"error": "'panel' must be a list of 'provider/model' strings"})
            return

        inline_rubric = payload.get("rubric")
        if inline_rubric is not None and not isinstance(inline_rubric, str):
            self._send_json(400, {"error": "'rubric' must be a string"})
            return

        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        try:
            result = asyncio.run(run_critique(
                panel, plan, fmt_req, panel_override or None, inline_rubric or None,
            ))
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to the client
            log_event("critique_error", error=repr(e)[:300])
            self._send_json(500, {"error": f"internal error: {type(e).__name__}"})
            return

        self._send_json(200, result)


def main() -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "7860")))

    panel = Panel()
    if not panel.providers:
        log_event("startup_warning", msg="no providers have API keys - every judge will fail")

    server = ThreadingHTTPServer((host, port), Handler)
    server.panel = panel  # type: ignore[attr-defined]

    log_event("startup", host=host, port=port,
              providers=[p.name for p in panel.providers],
              default_panel=panel.default_panel,
              token_configured=bool(os.environ.get("CRITIQUE_TOKEN", "").strip()))
    print(f"loom critique panel listening on {host}:{port}  "
          f"(providers={[p.name for p in panel.providers]})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
