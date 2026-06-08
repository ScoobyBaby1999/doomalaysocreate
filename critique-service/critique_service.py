from __future__ import annotations
import asyncio
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orchestrate
from content.roles import make_prompt
from jobs import JobRunner, finalize_judges, run_panel_slots
from metrics import MetricStore
from oplog import log_event
from orchestrate import OrchestrateError
from orchestrator.orchestrator import execute as orchestrator_execute
from providers import (
    load_models_catalog,
    make_provider_registry,
    make_slot,
    make_slot_registry,
    provider,
    slot,
)
from scheduler import SlotScheduler

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
JUDGE_TIMEOUT_S = float(os.environ.get("JUDGE_TIMEOUT_S", "900"))  # frontier reasoning models are slow; async mode makes long waits free
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1_000_000)))

# generalized /api/panel: any loom role (or a fully custom system prompt) fanned
# out across the model panel. each role maps to a prompt skeleton in
# content/prompts/<role>.md and a sensible default merge + token budget.
VALID_ROLES = (
    "critiquer", "schematic_critiquer", "verifier",
    "generator", "transformer", "parser", "planner",
)
ROLE_DEFAULT_MERGE = {
    "critiquer": "dedupe", "schematic_critiquer": "dedupe", "verifier": "vote",
    "generator": "concat", "transformer": "concat", "parser": "concat",
    "planner": "concat",
}
ROLE_DEFAULT_MAX_TOKENS = {
    "critiquer": 1500, "schematic_critiquer": 1500, "verifier": 1000,
    "parser": 1500, "planner": 3000, "generator": 4000, "transformer": 4000,
}
MERGE_MODES = ("dedupe", "vote", "concat", "none")

# manual effort modes (no auto-prediction). each scales how wide the fan-out is,
# the per-judge token budget, and the per-judge timeout. applied in build_*_params.
DEFAULT_EFFORT = os.environ.get("DEFAULT_EFFORT", "med").strip() or "med"
EFFORT_MODES = {
    "low":  {"num_models": 1, "max_tokens_mult": 0.5, "timeout_s": 120.0},
    "med":  {"num_models": 3, "max_tokens_mult": 1.0, "timeout_s": 300.0},
    "high": {"num_models": 5, "max_tokens_mult": 1.5, "timeout_s": 600.0},
    "max":  {"num_models": 99, "max_tokens_mult": 2.0, "timeout_s": JUDGE_TIMEOUT_S},
}


def resolve_effort(effort: str | None) -> dict:
    return EFFORT_MODES.get((effort or DEFAULT_EFFORT), EFFORT_MODES["med"])


def apply_effort(params: dict, effort: str) -> dict:
    #   trim the panel width, scale tokens, and set the per-judge timeout per the
    #   manual effort mode. mutates + returns params.
    cfg = resolve_effort(effort)
    params["effort"] = effort if effort in EFFORT_MODES else DEFAULT_EFFORT
    params["who_list"] = params["who_list"][: cfg["num_models"]]
    params["max_tokens"] = max(64, int(params["max_tokens"] * cfg["max_tokens_mult"]))
    params["timeout_s"] = min(JUDGE_TIMEOUT_S, cfg["timeout_s"])
    return params


class Panel:
    """the configured judge panel + a shared scheduler/metrics substrate."""

    def __init__(self) -> None:
        self.providers: list[provider] = make_provider_registry()
        self.provider_by_name: dict[str, provider] = {p.name: p for p in self.providers}
        base_slots = make_slot_registry(self.providers)
        self.slot_by_who: dict[str, slot] = {s.who: s for s in base_slots}
        self.logical_models: dict[str, dict] = load_models_catalog()

        cfg = _load_panel_cfg()
        self.default_panel: list[str] = cfg["judges"]
        self.max_parallel: int = cfg["max_parallel"]
        self.default_rubric: str = cfg["rubric"]

        #   one shared scheduler over EVERY known slot (base catalog + logical
        #   candidates + panel-named) drives rotation + cross-provider failover;
        #   one shared metrics store captures per-profile cost/throttle/latency.
        self.scheduler = SlotScheduler(base_slots)
        self.metrics = MetricStore()
        self.templates = orchestrate.TemplateStore()

        #   pre-register logical-model candidate slots + default-panel slots so they
        #   join rotation from boot.
        for spec in self.logical_models.values():
            for cand in spec.get("candidates", []):
                self._ensure_slot(f"{cand['provider']}/{cand['model']}")
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
        synth = make_slot(prov, model)
        self.slot_by_who[who] = synth
        self.scheduler.add_slots([synth])
        log_event("panel_slot_registered", slot=who, provider=prov_name, model=model)
        return synth

    def resolve(self, who: str) -> slot | None:
        return self._ensure_slot(who)

    def resolve_candidates(self, who: str) -> tuple[str, list[slot]]:
        #   resolve a panel entry to (logical_name, ordered candidate slots).
        #     - "provider/model" (contains '/') -> a single physical slot (back-compat).
        #     - a logical key in models_catalog  -> every configured host for it.
        if "/" in who:
            s = self._ensure_slot(who)
            return (who, [s] if s is not None else [])
        spec = self.logical_models.get(who)
        if spec is None:
            return (who, [])
        cands: list[slot] = []
        for cand in spec.get("candidates", []):
            s = self._ensure_slot(f"{cand['provider']}/{cand['model']}")
            if s is not None:
                cands.append(s)
        return (who, cands)


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


def build_critique_params(panel: Panel, plan: str, fmt_req: str,
                          panel_override: list[str] | None,
                          inline_rubric: str | None) -> dict:
    #   resolve a critique request into a provider-agnostic execution plan that
    #   either run_sync() or the JobRunner can carry out.
    if fmt_req == "markdown":
        fmt = "markdown"
    elif fmt_req == "schematic":
        fmt = "schematic"
    else:
        fmt, _ = detect_format(plan)
    system_prompt = build_system_prompt(fmt, plan, inline_rubric, panel.default_rubric)
    return {
        "kind": "critique",
        "role": panel.default_rubric,
        "merge": "dedupe",
        "system_prompt": system_prompt,
        "user_msg": "Produce the requested critique now.",
        "max_tokens": CRITIQUE_MAX_TOKENS,
        "who_list": list(panel_override or panel.default_panel),
        "format_detected": fmt,
    }


def build_panel_params(panel: Panel, *, input_text: str, role: str,
                       system: str | None, instructions: str | None,
                       output_rules: str | None, template: str | None,
                       panel_override: list[str] | None, merge_mode: str | None,
                       max_tokens: int | None, want_artifacts: bool = False) -> dict:
    #   generalized: any loom role, or a fully custom system prompt. this is what
    #   turns the critique panel into a general "ask my frontier panel to do X".
    if system:
        system_prompt = f"{system.strip()}\n\n## Input\n{input_text}"
        role_label = "custom"
        mode = merge_mode or "concat"
        mt = max_tokens or 4000
    else:
        system_prompt = make_prompt(
            role,
            instructions=instructions or "",
            output_rules=output_rules or "(no specific output rules)",
            inputs=input_text,
            template=template or "",
        )
        role_label = role
        mode = merge_mode or ROLE_DEFAULT_MERGE.get(role, "none")
        mt = max_tokens or ROLE_DEFAULT_MAX_TOKENS.get(role, 2000)
    #   artifacts mode: ask each model to emit a multi-file tree (marker blocks +
    #   nonce); each judge's reply is parsed into its OWN tree, kept fully separate.
    nonce = ""
    if want_artifacts:
        import artifacts as _artifacts
        nonce = _artifacts.make_nonce()
        system_prompt = system_prompt + _artifacts.artifact_instructions(nonce)
    return {
        "kind": "panel",
        "role": role_label,
        "merge": mode,
        "system_prompt": system_prompt,
        "user_msg": "Produce the requested output now.",
        "max_tokens": mt,
        "who_list": list(panel_override or panel.default_panel),
        "nonce": nonce,
    }


async def run_sync(panel: Panel, params: dict) -> dict:
    #   synchronous execution: every judge in parallel and independent, returns
    #   once all have settled. for slow frontier panels prefer async (JobRunner).
    t0 = time.monotonic()
    judges = await run_panel_slots(
        panel, params["who_list"], params["system_prompt"],
        role_label=params["role"], effort=params.get("effort", DEFAULT_EFFORT),
        profile=params.get("profile", "default"), metrics=panel.metrics,
        user_msg=params["user_msg"], max_tokens=params["max_tokens"],
        timeout_s=params.get("timeout_s", JUDGE_TIMEOUT_S),
        nonce=params.get("nonce", ""),
    )
    finished, merged = finalize_judges(judges, params["kind"], params["merge"])
    ok_count = sum(1 for j in finished if j.get("ok"))
    elapsed = round(time.monotonic() - t0, 2)
    log_event("panel_sync_done", req_kind=params["kind"], role=params["role"],
              merge=params["merge"], profile=params.get("profile", "default"),
              effort=params.get("effort"), judges_total=len(finished),
              judges_ok=ok_count, elapsed_s=elapsed)
    resp: dict = {
        "role": params["role"],
        "merge": params["merge"],
        "judges": finished,
        "merged": merged,
        "meta": {"elapsed_s": elapsed, "judges_ok": ok_count, "judges_total": len(finished)},
    }
    if params["kind"] == "critique":
        return {"format_detected": params.get("format_detected"), **resp}
    return resp


def submit_async(server_jobs: JobRunner, params: dict) -> dict:
    #   hand the execution plan to the background JobRunner; returns the initial
    #   (all-pending) snapshot immediately. the client polls GET /api/jobs/<id>.
    return server_jobs.submit(
        who_list=params["who_list"], system_prompt=params["system_prompt"],
        user_msg=params["user_msg"], max_tokens=params["max_tokens"],
        role=params["role"], merge_mode=params["merge"], kind=params["kind"],
        profile=params.get("profile", "default"), effort=params.get("effort", DEFAULT_EFFORT),
        timeout_s=params.get("timeout_s"), nonce=params.get("nonce", ""),
    )


async def run_orchestration_sync(panel: Panel, schematic, prompt: str, *,
                                 profile: str, effort: str, nonce: str,
                                 plan_mode: bool = False) -> dict:
    #   synchronous multi-stage run (blocks until the pipeline settles). long
    #   templates should prefer async ({"async": true} -> poll GET /api/jobs/<id>).
    import httpx
    async with httpx.AsyncClient() as client:
        if plan_mode:
            schematic, nonce = await orchestrate.plan_now(prompt, panel.scheduler, client)
        res = await orchestrator_execute(
            schematic, panel.scheduler, client, initial_context={"prompt": prompt},
            log=lambda *_: None, metrics=panel.metrics, profile=profile,
            effort=effort, nonce=nonce)
    return orchestrate.serialize_run_result(res, task=schematic.task)


# --- HTTP layer -------------------------------------------------------------

class _BadRequest(ValueError):
    """raised by request builders to signal a 400 with a client-safe message."""


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
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        if route in ("", "/health"):
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            self._send_json(200, {
                "service": "loom model panel",
                "status": "ok",
                "token_required": True,
                "token_configured": bool(os.environ.get("CRITIQUE_TOKEN", "").strip()),
                "providers_configured": [p.name for p in panel.providers],
                "default_panel": panel.default_panel,
                "default_rubric": panel.default_rubric,
                "endpoints": {
                    "POST /api/critique": "critique a plan/schematic (preset)",
                    "POST /api/panel": "general: any role or custom system prompt",
                    "POST /api/run": "orchestrator: template id | inline schematic | template:'auto' (planner); judge loop + file artifacts",
                    "GET /api/templates": "list built-in + user templates",
                    "GET /api/templates/<id>": "fetch one template's schematic",
                    "POST /api/templates": "save a user template {id, schematic} (persisted)",
                    "DELETE /api/templates/<id>": "delete a user template",
                    "GET /api/jobs/<id>": "poll an async job/run (when called with async:true)",
                    "GET /api/stats": "live rotation/health per provider + slot",
                    "GET /api/metrics?profile=<id>": "per-profile cost/throttle/latency aggregates",
                },
                "async": "add \"async\": true to any POST to get a job_id back instantly; "
                         "each judge runs independently, poll GET /api/jobs/<id> for partial results",
                "roles": list(VALID_ROLES),
                "merge_modes": list(MERGE_MODES),
                "effort_modes": list(EFFORT_MODES),
                "logical_models": sorted(panel.logical_models.keys()),
            })
            return
        if route == "/api/templates" or route.startswith("/api/templates/"):
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            if route == "/api/templates":
                self._send_json(200, {"templates": panel.templates.list()})
            else:
                tid = route[len("/api/templates/"):]
                data = panel.templates.get_dict(tid)
                if data is None:
                    self._send_json(404, {"error": f"no such template {tid!r}"})
                else:
                    self._send_json(200, {"id": tid, "schematic": data})
            return
        if route in ("/api/stats", "/api/metrics"):
            #   telemetry endpoints share the bearer token with the POST routes.
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            if route == "/api/stats":
                self._send_json(200, {
                    "providers": panel.scheduler.provider_rollup(),
                    "slots": panel.scheduler.snapshot(),
                    "profiles": panel.metrics.profiles(),
                })
            else:
                #       /api/metrics?profile=<id> (defaults to "default").
                profile = "default"
                if "?" in self.path:
                    from urllib.parse import parse_qs, urlsplit
                    q = parse_qs(urlsplit(self.path).query)
                    profile = (q.get("profile", ["default"])[0] or "default")
                self._send_json(200, panel.metrics.aggregates(profile))
            return
        if route.startswith("/api/jobs/"):
            #   polling an async job needs the same bearer token as submitting one.
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            job_id = route[len("/api/jobs/"):]
            snap = self.server.jobs.snapshot(job_id)  # type: ignore[attr-defined]
            if snap is None:
                self._send_json(404, {"error": "no such job (unknown id or expired)"})
                return
            self._send_json(200, snap)
            return
        self._send_json(404, {"error": "not found"})

    def _auth_and_body(self) -> dict | None:
        #   shared gate for POST routes: bearer auth + JSON body parse. on any
        #   failure it writes the error response and returns None.
        if not _token_ok(self.headers.get("Authorization")):
            if not os.environ.get("CRITIQUE_TOKEN", "").strip():
                self._send_json(503, {"error": "CRITIQUE_TOKEN not configured on server"})
            else:
                self._send_json(401, {"error": "missing or invalid bearer token"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    @staticmethod
    def _valid_panel(p) -> bool:
        return p is None or (isinstance(p, list) and all(isinstance(x, str) for x in p))

    @staticmethod
    def _profile_and_effort(payload: dict) -> tuple[str, str]:
        profile = payload.get("profile", "default")
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 64:
            raise _BadRequest("'profile' must be a non-empty string (<=64 chars)")
        effort = payload.get("effort", DEFAULT_EFFORT)
        if effort not in EFFORT_MODES:
            raise _BadRequest(f"'effort' must be one of {list(EFFORT_MODES)}")
        return profile.strip(), effort

    def do_POST(self) -> None:
        route = self.path.rstrip("/")
        if route not in ("/api/critique", "/api/panel", "/api/run", "/api/templates"):
            self._send_json(404, {"error": "not found"})
            return
        payload = self._auth_and_body()
        if payload is None:
            return
        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        is_async = bool(payload.get("async"))

        if route == "/api/templates":
            self._handle_template_save(payload, panel)
            return
        if route == "/api/run":
            self._handle_run(payload, panel, is_async)
            return

        try:
            if route == "/api/critique":
                params = self._params_critique(payload, panel)
            else:
                params = self._params_panel(payload, panel)
        except _BadRequest as e:
            self._send_json(400, {"error": str(e)})
            return

        try:
            if is_async:
                snap = submit_async(self.server.jobs, params)  # type: ignore[attr-defined]
                self._send_json(202, snap)
            else:
                result = asyncio.run(run_sync(panel, params))
                self._send_json(200, result)
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to the client
            log_event("request_error", route=route, error=repr(e)[:300])
            self._send_json(500, {"error": f"internal error: {type(e).__name__}"})

    def _handle_template_save(self, payload: dict, panel: Panel) -> None:
        #   POST /api/templates {id, schematic} -> validate + persist a user template.
        tid = payload.get("id")
        schematic_obj = payload.get("schematic")
        if not isinstance(tid, str) or not tid.strip():
            self._send_json(400, {"error": "'id' (string) is required"})
            return
        if not isinstance(schematic_obj, dict):
            self._send_json(400, {"error": "'schematic' (object) is required"})
            return
        try:
            summary = panel.templates.save(tid.strip(), schematic_obj)
        except OrchestrateError as e:
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(201, {"saved": summary})

    def do_DELETE(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        if not route.startswith("/api/templates/"):
            self._send_json(404, {"error": "not found"})
            return
        if not _token_ok(self.headers.get("Authorization")):
            self._send_json(401, {"error": "missing or invalid bearer token"})
            return
        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        tid = route[len("/api/templates/"):]
        try:
            removed = panel.templates.delete(tid)
        except OrchestrateError as e:
            self._send_json(400, {"error": str(e)})
            return
        if not removed:
            self._send_json(404, {"error": f"no such user template {tid!r}"})
        else:
            self._send_json(200, {"deleted": tid})

    def _handle_run(self, payload: dict, panel: Panel, is_async: bool) -> None:
        #   /api/run: drive a multi-stage orchestrator template/schematic with the
        #   built-in judge loop. returns the final body + multi-file artifacts.
        try:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise _BadRequest("'prompt' (non-empty string) is required")
            template_id = payload.get("template")
            if template_id is not None and not isinstance(template_id, str):
                raise _BadRequest("'template' must be a string id")
            schematic_obj = payload.get("schematic")
            if schematic_obj is not None and not isinstance(schematic_obj, dict):
                raise _BadRequest("'schematic' must be a JSON object")
            profile, effort = self._profile_and_effort(payload)
            schematic, nonce, plan_mode = orchestrate.resolve_schematic(
                panel.templates, prompt, template_id=template_id,
                schematic_obj=schematic_obj, label=payload.get("label", ""))
        except (_BadRequest, OrchestrateError) as e:
            self._send_json(400, {"error": str(e)})
            return
        try:
            if is_async:
                snap = self.server.jobs.submit_run(  # type: ignore[attr-defined]
                    schematic=schematic, prompt=prompt, nonce=nonce,
                    profile=profile, effort=effort, plan_mode=plan_mode)
                self._send_json(202, snap)
            else:
                result = asyncio.run(run_orchestration_sync(
                    panel, schematic, prompt, profile=profile, effort=effort,
                    nonce=nonce, plan_mode=plan_mode))
                self._send_json(200, result)
        except Exception as e:  # noqa: BLE001 - never leak a stack trace
            log_event("request_error", route="/api/run", error=repr(e)[:300])
            self._send_json(500, {"error": f"internal error: {type(e).__name__}"})

    def _params_critique(self, payload: dict, panel: Panel) -> dict:
        plan = payload.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            raise _BadRequest("'plan' (non-empty string) is required")
        fmt_req = payload.get("format", "auto")
        if fmt_req not in ("auto", "markdown", "schematic"):
            raise _BadRequest("'format' must be auto|markdown|schematic")
        panel_override = payload.get("panel")
        if not self._valid_panel(panel_override):
            raise _BadRequest("'panel' must be a list of 'provider/model' strings")
        inline_rubric = payload.get("rubric")
        if inline_rubric is not None and not isinstance(inline_rubric, str):
            raise _BadRequest("'rubric' must be a string")
        profile, effort = self._profile_and_effort(payload)
        params = build_critique_params(panel, plan, fmt_req, panel_override or None,
                                       inline_rubric or None)
        params["profile"] = profile
        return apply_effort(params, effort)

    def _params_panel(self, payload: dict, panel: Panel) -> dict:
        input_text = payload.get("input")
        if not isinstance(input_text, str) or not input_text.strip():
            raise _BadRequest("'input' (non-empty string) is required")
        system = payload.get("system")
        if system is not None and not isinstance(system, str):
            raise _BadRequest("'system' must be a string")
        role = payload.get("role", "critiquer")
        if not system:
            if not isinstance(role, str) or role not in VALID_ROLES:
                raise _BadRequest(f"'role' must be one of {list(VALID_ROLES)} (or pass 'system')")
        for k in ("instructions", "output_rules", "template"):
            if payload.get(k) is not None and not isinstance(payload[k], str):
                raise _BadRequest(f"'{k}' must be a string")
        merge_mode = payload.get("merge")
        if merge_mode is not None and merge_mode not in MERGE_MODES:
            raise _BadRequest(f"'merge' must be one of {list(MERGE_MODES)}")
        max_tokens = payload.get("max_tokens")
        if max_tokens is not None and (not isinstance(max_tokens, int) or not 1 <= max_tokens <= 32000):
            raise _BadRequest("'max_tokens' must be an int in 1..32000")
        panel_override = payload.get("panel")
        if not self._valid_panel(panel_override):
            raise _BadRequest("'panel' must be a list of 'provider/model' strings")
        profile, effort = self._profile_and_effort(payload)
        params = build_panel_params(
            panel, input_text=input_text, role=role, system=system or None,
            instructions=payload.get("instructions"), output_rules=payload.get("output_rules"),
            template=payload.get("template"), panel_override=panel_override or None,
            merge_mode=merge_mode, max_tokens=max_tokens,
            want_artifacts=bool(payload.get("artifacts")),
        )
        params["profile"] = profile
        return apply_effort(params, effort)


def main() -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "7860")))

    panel = Panel()
    if not panel.providers:
        log_event("startup_warning", msg="no providers have API keys - every judge will fail")

    server = ThreadingHTTPServer((host, port), Handler)
    server.panel = panel  # type: ignore[attr-defined]
    server.jobs = JobRunner(panel, judge_timeout_s=JUDGE_TIMEOUT_S)  # type: ignore[attr-defined]

    log_event("startup", host=host, port=port,
              providers=[p.name for p in panel.providers],
              default_panel=panel.default_panel,
              token_configured=bool(os.environ.get("CRITIQUE_TOKEN", "").strip()))
    print(f"loom model panel listening on {host}:{port}  "
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
