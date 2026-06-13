from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent_sessions
import authtoken
import orchestrate
from content.roles import make_prompt
from jobs import JobCapExceeded, JobRunner, finalize_judges, run_panel_slots
from metrics import MetricStore
from oplog import log_event
from orchestrate import OrchestrateError
from orchestrator.orchestrator import execute as orchestrator_execute
import repopack
from promptcache import PromptCache
from providers import (
    load_benchmarks,
    load_models_catalog,
    load_reasoning_catalog,
    make_provider_registry,
    make_slot,
    make_slot_registry,
    provider,
    slot,
    slot_is_privacy_safe,
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
#   the built web app (Docker build drops the frontend's dist/ here). when present the
#   gateway serves it at /, making one Space the whole product; absent => API-only.
STATIC_DIR = Path(os.environ.get("STATIC_DIR", HERE / "static"))

#   OAuth onboarding: the template Space users get duplicated to their own account.
TEMPLATE_SPACE_ID = os.environ.get("TEMPLATE_SPACE_ID", "ScoobyBaby1999/Loom")
#   in-memory one-time provision results (token → (timestamp, result)), expires in 5 min.
_provision_results: dict[str, tuple[float, dict]] = {}
_provision_lock = threading.Lock()

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

# budgets are UPPER BOUNDS, not targets: models stop when done and free tiers bill
# nothing extra for headroom, so we set them at the playground-grade 16k the frontier
# hosts use. the only true per-request output ceiling lives in providers_catalog
# limits.max_out (e.g. GitHub Models ~4k) and is clamped per-provider in call_slot.
CRITIQUE_MAX_TOKENS = int(os.environ.get("CRITIQUE_MAX_TOKENS", "16384"))
JUDGE_TIMEOUT_S = float(os.environ.get("JUDGE_TIMEOUT_S", "1800"))  # frontier reasoning models are slow; async mode makes long waits free
#   8MB default: a whole-repo "files" pack is far larger than a typical prompt (the
#   full critique-service source is ~0.5MB; leaves headroom for real projects).
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8_000_000)))
REPO_MAX_BYTES = int(os.environ.get("REPO_MAX_BYTES", str(50_000_000)))
#   server concurrency bounds (free multi-user safety). MAX_WORKERS caps simultaneous
#   sync requests; REQUEST_TIMEOUT_S kills slow/slowloris connections holding a worker.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "48"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "30"))
DEFAULT_CTX_TOKENS = int(os.environ.get("DEFAULT_CTX_TOKENS", "131072"))
#   reserved for the role/rubric scaffolding around the packed repo in the system prompt.
PACK_SYSTEM_OVERHEAD_TOKENS = 2000

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
    "critiquer": 16384, "schematic_critiquer": 16384, "verifier": 8192,
    "parser": 8192, "planner": 16384, "generator": 16384, "transformer": 16384,
}
MERGE_MODES = ("dedupe", "vote", "concat", "none")

# manual effort modes (no auto-prediction). each scales how wide the fan-out is,
# the per-judge token budget, and the per-judge timeout. applied in build_*_params.
DEFAULT_EFFORT = os.environ.get("DEFAULT_EFFORT", "med").strip() or "med"
EFFORT_MODES = {
    "low":  {"num_models": 1, "max_tokens_mult": 0.5, "timeout_s": 300.0},
    "med":  {"num_models": 3, "max_tokens_mult": 1.0, "timeout_s": 900.0},
    "high": {"num_models": 5, "max_tokens_mult": 1.5, "timeout_s": 1200.0},
    "max":  {"num_models": 99, "max_tokens_mult": 2.0, "timeout_s": JUDGE_TIMEOUT_S},
}


def resolve_effort(effort: str | None) -> dict:
    return EFFORT_MODES.get((effort or DEFAULT_EFFORT), EFFORT_MODES["med"])


# deep-reasoning / research "burn tokens" budget. when reasoning or research is on,
# the per-judge budget jumps to this floor and the timeout maxes out (long by design;
# prefer async). research adds the web ReAct loop on top.
RESEARCH_MAX_TOKENS = int(os.environ.get("RESEARCH_MAX_TOKENS", "32768"))


def apply_effort(params: dict, effort: str) -> dict:
    #   trim the panel width, scale tokens, and set the per-judge timeout per the
    #   manual effort mode. mutates + returns params.
    cfg = resolve_effort(effort)
    params["effort"] = effort if effort in EFFORT_MODES else DEFAULT_EFFORT
    params["who_list"] = params["who_list"][: cfg["num_models"]]
    params["max_tokens"] = max(64, int(params["max_tokens"] * cfg["max_tokens_mult"]))
    params["timeout_s"] = min(JUDGE_TIMEOUT_S, cfg["timeout_s"])
    if params.get("reasoning") or params.get("research"):
        params["max_tokens"] = max(params["max_tokens"], RESEARCH_MAX_TOKENS)
        params["timeout_s"] = JUDGE_TIMEOUT_S
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

        #   per-slot context windows (who -> tokens), shared with the scheduler BY
        #   REFERENCE so ctx-aware routing sees every slot registered below.
        self.ctx_by_who: dict[str, int] = {}
        #   one shared scheduler over EVERY known slot (base catalog + logical
        #   candidates + panel-named) drives rotation + cross-provider failover;
        #   one shared metrics store captures per-profile cost/throttle/latency.
        self.scheduler = SlotScheduler(base_slots, ctx_by_who=self.ctx_by_who)
        self.metrics = MetricStore()
        self.templates = orchestrate.TemplateStore()
        self.reasoning_catalog = load_reasoning_catalog()
        self.benchmarks = load_benchmarks()
        self.cache = PromptCache()

        #   pre-register logical-model candidate slots + default-panel slots so they
        #   join rotation from boot. also index each candidate's context window for
        #   repo-pack budget fitting + ctx-aware orchestrator routing.
        for spec in self.logical_models.values():
            for cand in spec.get("candidates", []):
                who = f"{cand['provider']}/{cand['model']}"
                self._ensure_slot(who)
                if cand.get("ctx"):
                    self.ctx_by_who[who] = int(cand["ctx"])
        for who in self.default_panel:
            self._ensure_slot(who)

    def judge_ctx(self, who: str) -> int:
        #   a judge's usable context window: for a logical model, the LARGEST among
        #   its candidate hosts (failover prefers big-ctx hosts anyway; small-ctx
        #   candidates simply fail over). physical slots look up directly.
        if "/" in who:
            return self.ctx_by_who.get(who, DEFAULT_CTX_TOKENS)
        spec = self.logical_models.get(who) or {}
        vals = [int(c["ctx"]) for c in spec.get("candidates", []) if c.get("ctx")]
        return max(vals) if vals else DEFAULT_CTX_TOKENS

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

    def roster(self) -> dict:
        #   join the logical catalog with benchmarks + LIVE host health so a caller can
        #   see, per model: how strong it is, who hosts it, which hosts are privacy-safe,
        #   and whether it's routable RIGHT NOW. Also computes the >=2-frontier guarantee.
        now = time.time()
        models: list[dict] = []
        frontier_total = 0
        frontier_safe_available = 0
        for logical in sorted(self.logical_models.keys()):
            _, cands = self.resolve_candidates(logical)
            bench = self.benchmarks.get(logical, {})
            is_frontier = bool(bench.get("frontier"))
            hosts = []
            routable = safe_routable = False
            for s in cands:
                st = self.scheduler.slot_state.get(s.who)
                cooling = bool(st and st.cooldown_until > now)
                blacklisted = bool(st and st.blacklisted)
                live = not (cooling or blacklisted)
                safe = slot_is_privacy_safe(s)
                hosts.append({"slot": s.who, "privacy_safe": safe,
                              "stability_tier": s.provider.stability_tier,
                              "cooling": cooling, "blacklisted": blacklisted})
                routable = routable or live
                safe_routable = safe_routable or (live and safe)
            if is_frontier:
                frontier_total += 1
                if safe_routable:
                    frontier_safe_available += 1
            models.append({
                "logical": logical, "frontier": is_frontier,
                "arena_elo": bench.get("arena_elo"), "aa_index": bench.get("aa_index"),
                "hosts": hosts, "routable": routable,
                "privacy_safe_routable": safe_routable,
            })
        models.sort(key=lambda m: (m["arena_elo"] or 0), reverse=True)
        return {
            "models": models,
            "frontier_total": frontier_total,
            "frontier_privacy_safe_available": frontier_safe_available,
            "frontier_ok": frontier_safe_available >= 2,
            "benchmark_note": "indicative, hand-curated (see benchmarks.json) - not authoritative",
        }


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
                       max_tokens: int | None, want_artifacts: bool = False,
                       reasoning: bool = False, research: bool = False) -> dict:
    #   generalized: any loom role, or a fully custom system prompt. this is what
    #   turns the critique panel into a general "ask my frontier panel to do X".
    if system:
        system_prompt = f"{system.strip()}\n\n## Input\n{input_text}"
        role_label = "custom"
        mode = merge_mode or "concat"
        mt = max_tokens or 16384
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
        mt = max_tokens or ROLE_DEFAULT_MAX_TOKENS.get(role, 8192)
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
        "reasoning": bool(reasoning or research),
        "research": bool(research),
    }


def resolve_repo_files(payload: dict) -> dict[str, str] | None:
    #   codebase-ingestion request forms. returns {path: content} or None when the
    #   request isn't a repo review. raises _BadRequest with a client-safe message.
    #     "files":    {"path": "content", ...}      client-packed (curl/CI friendly)
    #     "repo_url": public/private github url     server fetches the tarball
    #       + "repo_ref" (branch/tag/sha, default HEAD), "repo_token" (auth for
    #         private repos - used for the fetch only, never logged or persisted),
    #       + "diff_mode": {"base_ref": ..., "head_ref": ...} -> changed files only
    files = payload.get("files")
    repo_url = payload.get("repo_url")
    if files is None and repo_url is None:
        return None
    if files is not None and repo_url is not None:
        raise _BadRequest("pass either 'files' or 'repo_url', not both")
    if files is not None:
        if (not isinstance(files, dict) or not files
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
            raise _BadRequest("'files' must be a non-empty {path: content} object of strings")
        return dict(files)
    if not isinstance(repo_url, str) or "github.com/" not in repo_url:
        raise _BadRequest("'repo_url' must be a github.com repository URL")
    ref = payload.get("repo_ref", "HEAD")
    token = payload.get("repo_token", "") or ""
    diff = payload.get("diff_mode")
    try:
        if diff is not None:
            if not (isinstance(diff, dict) and diff.get("base_ref") and diff.get("head_ref")):
                raise _BadRequest("'diff_mode' needs {'base_ref':..., 'head_ref':...}")
            base = repopack.fetch_repo_files(repo_url, ref=str(diff["base_ref"]),
                                             token=token, max_bytes=REPO_MAX_BYTES)
            head = repopack.fetch_repo_files(repo_url, ref=str(diff["head_ref"]),
                                             token=token, max_bytes=REPO_MAX_BYTES)
            return _diff_files(base, head)
        return repopack.fetch_repo_files(repo_url, ref=str(ref), token=token,
                                         max_bytes=REPO_MAX_BYTES)
    except repopack.RepoFetchError as e:
        raise _BadRequest(str(e)) from e


def _diff_files(base: dict[str, str], head: dict[str, str]) -> dict[str, str]:
    #   diff-aware review: the pack carries each CHANGED/ADDED file in full (head
    #   version) plus a synthetic CHANGES.diff summary, so judges see both the delta
    #   and enough surrounding context to judge it.
    import difflib
    changed = {p: c for p, c in head.items() if base.get(p) != c}
    removed = sorted(p for p in base if p not in head)
    if not changed and not removed:
        raise repopack.RepoFetchError("diff_mode: no differences between the two refs")
    diffs: list[str] = [f"removed: {p}" for p in removed]
    for p in sorted(changed):
        ud = difflib.unified_diff((base.get(p) or "").splitlines(), changed[p].splitlines(),
                                  fromfile=f"a/{p}", tofile=f"b/{p}", lineterm="", n=3)
        diffs.append("\n".join(list(ud)[:400]))
    out = dict(changed)
    out["CHANGES.diff"] = "\n\n".join(diffs)[:repopack.MAX_FILE_CHARS]
    return out


def apply_repo_form(panel: Panel, params: dict, pack, *, rebuild) -> dict:
    #   fit an already-built repo pack to each judge's context budget. `rebuild(text)`
    #   is the route's own params builder (panel or critique flavor) - re-invoked with
    #   a judge's REDUCED pack to produce that judge's system prompt, so both routes
    #   share this logic without caring how the prompt is scaffolded.
    full_total = pack.total_tokens
    per_judge: dict = {}
    smallest_fit = None
    for who in params["who_list"]:
        budget = panel.judge_ctx(who) - PACK_SYSTEM_OVERHEAD_TOKENS - int(params["max_tokens"])
        if budget <= 0:
            raise _BadRequest(f"judge '{who}': max_tokens leaves no room for the pack "
                              f"(ctx={panel.judge_ctx(who)})")
        if full_total <= budget:
            per_judge[who] = {"coverage": {"files_included": len(pack.files),
                                           "files_total": len(pack.files),
                                           "pack_tokens": full_total, "dropped": []}}
            continue
        reduced, dropped = repopack.fit_to_budget(pack, budget)
        if not reduced.files:
            raise _BadRequest(f"repo pack (~{full_total} tokens) cannot fit judge '{who}' "
                              f"(budget ~{budget} tokens) - drop judges or split the repo")
        rp = rebuild(repopack.render(reduced, dropped=dropped))
        per_judge[who] = {"system_prompt": rp["system_prompt"],
                          "coverage": {"files_included": len(reduced.files),
                                       "files_total": len(pack.files),
                                       "pack_tokens": reduced.total_tokens,
                                       "dropped": dropped[:50]}}
        smallest_fit = min(smallest_fit or budget, budget)
    params["per_judge"] = per_judge
    params["pack_meta"] = {"files": len(pack.files), "skipped": len(pack.skipped),
                           "total_tokens": full_total}
    log_event("repo_pack", files=len(pack.files), tokens=full_total,
              judges_reduced=sum(1 for v in per_judge.values() if "system_prompt" in v))
    return params


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
        nonce=params.get("nonce", ""), reasoning=params.get("reasoning", False),
        research=params.get("research", False),
        privacy=params.get("privacy", "off"), no_store=params.get("no_store", False),
        per_judge=params.get("per_judge"),
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
    if params.get("pack_meta"):
        resp["pack"] = params["pack_meta"]
    if params["kind"] == "critique":
        return {"format_detected": params.get("format_detected"), **resp}
    return resp


def submit_async(server_jobs: JobRunner, params: dict) -> dict:
    #   hand the execution plan to the background JobRunner; returns the initial
    #   (all-pending) snapshot immediately. the client polls GET /api/jobs/<id>.
    snap = server_jobs.submit(
        who_list=params["who_list"], system_prompt=params["system_prompt"],
        user_msg=params["user_msg"], max_tokens=params["max_tokens"],
        role=params["role"], merge_mode=params["merge"], kind=params["kind"],
        profile=params.get("profile", "default"), effort=params.get("effort", DEFAULT_EFFORT),
        timeout_s=params.get("timeout_s"), nonce=params.get("nonce", ""),
        reasoning=params.get("reasoning", False), research=params.get("research", False),
        privacy=params.get("privacy", "off"), no_store=params.get("no_store", False),
        per_judge=params.get("per_judge"),
    )
    if params.get("pack_meta") and isinstance(snap, dict):
        snap["pack"] = params["pack_meta"]
    return snap


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


def _auth_configured() -> bool:
    return bool(os.environ.get("CRITIQUE_TOKEN", "").strip()
                or os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip())


def _token_ok(header_value: str | None) -> bool:
    static = os.environ.get("CRITIQUE_TOKEN", "").strip()
    rotation_secret = os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip()
    if not (static or rotation_secret):
        return False  # never serve an open endpoint - require a secret to be set
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value[len("Bearer "):].strip()
    #   accept a static token (back-compat) OR an auto-rotating windowed token derived
    #   from the root secret (current + grace windows). evaluate BOTH paths (no early
    #   exit) so a present/absent static token doesn't change timing.
    ok = False
    if static and hmac.compare_digest(presented, static):
        ok = True
    if rotation_secret and authtoken.token_matches(presented, rotation_secret):
        ok = True
    return ok


# ---------------------------------------------------------------------------
# OAuth / onboarding helpers
# ---------------------------------------------------------------------------

def _oauth_configured() -> bool:
    return bool(os.environ.get("OAUTH_CLIENT_ID", "").strip())


def _make_oauth_state(nonce: str) -> str:
    """HMAC-signed state token: nonce.timestamp.sig — verifiable without server storage."""
    secret = os.environ.get("OAUTH_CLIENT_SECRET", "x").encode()
    ts = str(int(time.time()))
    data = f"{nonce}.{ts}"
    sig = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{data}.{sig}"


def _verify_oauth_state(state: str) -> bool:
    try:
        nonce, ts, sig = state.rsplit(".", 2)
        if abs(time.time() - float(ts)) > 600:  # 10-minute window
            return False
        secret = os.environ.get("OAUTH_CLIENT_SECRET", "x").encode()
        data = f"{nonce}.{ts}"
        expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def _hf_api(url: str, *, method: str = "GET", token: str = "",
            body: dict | None = None) -> dict:
    """Sync HF Hub API call via stdlib urllib — no extra deps."""
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _store_provision_result(result: dict) -> str:
    token = secrets.token_urlsafe(24)
    with _provision_lock:
        now = time.time()
        stale = [k for k, (ts, _) in _provision_results.items() if now - ts > 300]
        for k in stale:
            del _provision_results[k]
        _provision_results[token] = (now, result)
    return token


def _pop_provision_result(token: str) -> dict | None:
    with _provision_lock:
        entry = _provision_results.pop(token, None)
    if entry is None:
        return None
    ts, result = entry
    return result if time.time() - ts <= 300 else None


class Handler(BaseHTTPRequestHandler):
    server_version = "loom-panel/2.0"
    #   socketserver enforces this on the request socket: a client that opens a
    #   connection but sends its body slowly (slowloris) is dropped instead of pinning
    #   a worker forever. HTTP/1.0 default => no keep-alive holding workers between calls.
    timeout = REQUEST_TIMEOUT_S
    panel: Panel  # injected on the server instance

    def _send_json(self, status: int, payload: dict, *, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        #   CORS: the web app may be served from a different origin (separate static
        #   host, or local dev without the proxy). auth is a bearer header - no cookies -
        #   so a wildcard origin grants nothing by itself; requests still need the token.
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        #   CORS preflight for cross-origin POSTs with Authorization/Content-Type.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, raw_path: str) -> bool:
        #   serve the bundled web app from STATIC_DIR. returns False when there is no
        #   bundle (API-only deployment) or the path resolves outside it, so callers
        #   fall through to the JSON behavior. extension-less unknown paths get
        #   index.html (SPA routing); missing real assets still 404.
        if not STATIC_DIR.is_dir():
            return False
        rel = raw_path.split("?", 1)[0].lstrip("/")
        root = STATIC_DIR.resolve()
        try:
            target = (root / rel).resolve() if rel else root / "index.html"
            if not target.is_relative_to(root):
                return False
        except OSError:
            return False
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if "." in Path(rel).name:
                return False
            target = root / "index.html"
            if not target.is_file():
                return False
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        #   vite emits content-hashed filenames under assets/ -> cache forever;
        #   index.html must revalidate so a redeploy shows up on next load.
        if rel.startswith("assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - quiet default logging
        return  # telemetry goes through oplog; suppress the stderr access log spam

    def do_GET(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        if route == "" and self._serve_static("/"):
            return  # bundled web app owns the root; the JSON overview stays on /health
        if route in ("", "/health"):
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            roster = panel.roster()
            self._send_json(200, {
                "service": "loom model panel",
                "status": "ok",
                "web_app": STATIC_DIR.is_dir(),  # bundled frontend served at / ?
                "token_required": True,
                "token_configured": _auth_configured(),
                #   auto-rotating windowed tokens (CRITIQUE_ROTATION_SECRET); no value exposed.
                "token_rotation": (
                    {"enabled": True, "window_s": authtoken.TOKEN_WINDOW_S,
                     "seconds_until_rotation": authtoken.seconds_until_rotation()}
                    if os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip()
                    else {"enabled": False}),
                "providers_configured": [p.name for p in panel.providers],
                #   load/saturation: bounded HTTP workers + in-flight async jobs.
                "workers": {"max": getattr(self.server, "max_workers", None),
                            "busy": self.server.busy() if hasattr(self.server, "busy") else None},
                "jobs_inflight": self.server.jobs.inflight_count(),  # type: ignore[attr-defined]
                #   the >=2-frontier guarantee, measured over privacy-safe routable hosts.
                "frontier_ok": roster["frontier_ok"],
                "frontier_privacy_safe_available": roster["frontier_privacy_safe_available"],
                "frontier_total": roster["frontier_total"],
                "privacy_default": ("strict" if os.environ.get("PRIVACY_MODE", "").strip().lower()
                                     in ("1", "true", "on", "strict")
                                     else os.environ.get("DEFAULT_PRIVACY", "strict")),
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
                    "POST /api/agent": "agent chat: {message, session_id?} -> 202 {session_id}",
                    "GET /api/agent/<sid>?since=<n>": "poll the agent transcript (delta events)",
                    "GET /api/agent/<sid>/files": "list workspace artifacts",
                    "GET /api/agent/<sid>/file?path=": "download a workspace artifact",
                    "GET /api/stats": "live rotation/health per provider + slot",
                    "GET /api/metrics?profile=<id>": "per-profile cost/throttle/latency aggregates",
                    "GET /api/roster": "per-model benchmark + hosts + privacy-safe routability + frontier guarantee",
                    "GET /oauth/login": "start HF OAuth flow (redirects to HF authorize)",
                    "GET /oauth/callback": "HF OAuth callback — provisions user Space",
                    "GET /oauth/result/<token>": "exchange one-time token for provision result",
                    "POST /oauth/set-provider-key": "set a provider API key on the user's Space via HF API",
                },
                "oauth_configured": _oauth_configured(),
                #   agentic orchestrator tier: "claude" (ANTHROPIC_API_KEY + SDK),
                #   "open" (free provider key + OpenHands SDK), or null.
                "agent": agent_sessions.agent_tier(),
                "repo_review": "POST /api/panel or /api/critique with 'files': {path: content} "
                               "or 'repo_url' (+'repo_ref', 'repo_token', 'diff_mode': {base_ref, head_ref}) "
                               "instead of input/plan; each judge gets a pack fitted to its context "
                               "window and reports 'coverage' of what it actually saw",
                "privacy_modes": ["strict", "fallback", "off"],
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
        if route in ("/api/stats", "/api/metrics", "/api/roster"):
            #   telemetry endpoints share the bearer token with the POST routes.
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            if route == "/api/roster":
                roster = panel.roster()
                roster["cache"] = panel.cache.stats()
                self._send_json(200, roster)
            elif route == "/api/stats":
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
            from urllib.parse import parse_qs, urlsplit
            want_trace = parse_qs(urlsplit(self.path).query).get("trace", ["0"])[0] in ("1", "true", "yes")
            snap = self.server.jobs.snapshot(job_id, trace=want_trace)  # type: ignore[attr-defined]
            if snap is None:
                self._send_json(404, {"error": "no such job (unknown id or expired)"})
                return
            self._send_json(200, snap)
            return
        if route == "/api/agent/models":
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._send_json(200, {"tier": agent_sessions.agent_tier(),
                                  "models": agent_sessions.agent_models()})
            return
        if route.startswith("/api/agent/"):
            #   transcript polling + artifact access share the bearer token.
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._handle_agent_get(route)
            return
        if route == "/oauth/login":
            self._handle_oauth_login()
            return
        if route == "/oauth/callback":
            self._handle_oauth_callback()
            return
        if route.startswith("/oauth/result/"):
            self._handle_oauth_result(route[len("/oauth/result/"):])
            return
        if not route.startswith("/api") and self._serve_static(urlsplit(self.path).path):
            return
        self._send_json(404, {"error": "not found"})

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _space_host(self) -> str:
        return os.environ.get("SPACE_HOST", self.headers.get("Host", ""))

    def _handle_oauth_login(self) -> None:
        if not _oauth_configured():
            self._send_json(503, {"error": "OAuth not configured on this Space "
                                           "(OAUTH_CLIENT_ID missing — set hf_oauth:true in README)"})
            return
        from urllib.parse import urlencode
        nonce = secrets.token_urlsafe(16)
        state = _make_oauth_state(nonce)
        host = self._space_host()
        redirect_uri = f"https://{host}/oauth/callback"
        params = urlencode({
            "client_id": os.environ["OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "scope": "openid profile manage-repos",
            "response_type": "code",
            "state": state,
        })
        self._redirect(f"https://huggingface.co/oauth/authorize?{params}")

    def _handle_oauth_callback(self) -> None:
        from urllib.parse import parse_qs, urlencode, urlsplit
        import base64 as _b64
        qs = parse_qs(urlsplit(self.path).query)
        code = (qs.get("code", [""])[0] or "").strip()
        state = (qs.get("state", [""])[0] or "").strip()
        error_param = (qs.get("error", [""])[0] or "").strip()
        host = self._space_host()

        if error_param:
            self._redirect(f"https://{host}/#provision-error={error_param}")
            return
        if not code or not state or not _verify_oauth_state(state):
            self._redirect(f"https://{host}/#provision-error=invalid_state")
            return

        client_id = os.environ.get("OAUTH_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "").strip()
        redirect_uri = f"https://{host}/oauth/callback"

        # 1. Exchange code for user token
        try:
            import urllib.request as _ureq
            body_data = urlencode({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }).encode()
            creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            req = _ureq.Request("https://huggingface.co/oauth/token",
                                data=body_data, method="POST")
            req.add_header("Authorization", f"Basic {creds}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with _ureq.urlopen(req, timeout=30) as resp:
                token_data = json.loads(resp.read().decode())
            user_token = token_data.get("access_token", "").strip()
            if not user_token:
                raise ValueError("no access_token in token response")
        except Exception as exc:
            log_event("oauth_token_exchange_error", error=str(exc)[:200])
            self._redirect(f"https://{host}/#provision-error=token_exchange_failed")
            return

        # 2. Get HF username
        try:
            whoami = _hf_api("https://huggingface.co/api/whoami-v2", token=user_token)
            username = whoami.get("name", "").strip()
            if not username:
                raise ValueError("empty username")
        except Exception as exc:
            log_event("oauth_whoami_error", error=str(exc)[:200])
            self._redirect(f"https://{host}/#provision-error=whoami_failed")
            return

        from urllib.parse import quote as _q
        target_repo = f"{username}/loom"
        rotation_secret = secrets.token_urlsafe(32)
        existing = False

        # 3. Returning user? Repo ids are unique case-insensitively but API lookups
        #    are exact-case — find the canonical id of any existing "loom" Space
        #    (e.g. "<user>/Loom") instead of blindly assuming lowercase.
        try:
            spaces = _hf_api(
                f"https://huggingface.co/api/spaces?author={_q(username)}&limit=100",
                token=user_token)
            for sp in (spaces if isinstance(spaces, list) else []):
                sid = str(sp.get("id", ""))
                if sid.lower() == target_repo.lower():
                    target_repo = sid          # canonical casing
                    existing = True
                    break
        except Exception as exc:
            log_event("oauth_space_list_error", user=username, error=str(exc)[:200])
            # non-fatal: fall through to duplicate; a 409 there also means "exists"

        # 4. First sign-in: duplicate the template Space into their account
        if not existing:
            try:
                _hf_api(f"https://huggingface.co/api/spaces/{TEMPLATE_SPACE_ID}/duplicate",
                        method="POST", token=user_token,
                        body={"repository": target_repo, "private": False})
            except RuntimeError as exc:
                if "409" in str(exc):
                    existing = True            # raced / listing missed it — it's there
                else:
                    log_event("oauth_duplicate_error", user=username, error=str(exc)[:200])
                    self._redirect(f"https://{host}/#provision-error=duplicate_failed"
                                   f"&provision-detail={_q(str(exc)[:160])}")
                    return

        # 5. Set (first run) or rotate (re-login) CRITIQUE_ROTATION_SECRET.
        #    HF restarts the Space on secret change, so the new secret goes live.
        try:
            _hf_api(f"https://huggingface.co/api/spaces/{target_repo}/secrets",
                    method="POST", token=user_token,
                    body={"key": "CRITIQUE_ROTATION_SECRET", "value": rotation_secret})
        except Exception as exc:
            log_event("oauth_set_secret_error", user=username, error=str(exc)[:200])
            self._redirect(f"https://{host}/#provision-error=set_secret_failed"
                           f"&provision-detail={_q(str(exc)[:160])}")
            return

        space_name = target_repo.split("/", 1)[1].lower()
        space_url = f"https://{username.lower()}-{space_name}.hf.space"
        result = {
            "space_url": space_url,
            "space_repo": target_repo,
            "rotation_secret": rotation_secret,
            "username": username,
            "oauth_token": user_token,
            "existing": existing,
        }
        provision_token = _store_provision_result(result)
        log_event("oauth_provision_ok", user=username, space=target_repo)
        self._redirect(f"https://{host}/#provision-token={provision_token}")

    def _handle_oauth_result(self, token: str) -> None:
        result = _pop_provision_result(token.strip())
        if result is None:
            self._send_json(404, {"error": "provision token not found or expired (max 5 min)"})
            return
        self._send_json(200, result)

    # -- agent orchestrator routes ------------------------------------------

    def _handle_agent_post(self, payload: dict) -> None:
        #   POST /api/agent {message, session_id?} -> 202 {session_id, tier, status}
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._send_json(400, {"error": "'message' (non-empty string) is required"})
            return
        if len(message) > 100_000:
            self._send_json(413, {"error": "'message' too large (max 100k chars)"})
            return
        if agent_sessions.agent_tier() is None:
            self._send_json(503, {"error": "no agent tier configured — set ANTHROPIC_API_KEY "
                                           "(Claude agent) or any free provider key (open agent)"})
            return
        session_id = payload.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) else None
        model = payload.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        if model and not any(m["model"] == model for m in agent_sessions.agent_models()):
            self._send_json(400, {"error": f"model not available: {model}"})
            return
        try:
            session = agent_sessions.get_or_create(session_id, model)
        except agent_sessions.CapacityError as e:
            self._send_json(429, {"error": str(e)}, headers={"Retry-After": "30"})
            return
        except Exception as e:  # noqa: BLE001
            log_event("agent_create_error", error=repr(e)[:200])
            self._send_json(500, {"error": f"agent session error: {type(e).__name__}"})
            return
        session.submit(message.strip())
        self._send_json(202, {"session_id": session.id, "tier": session.tier,
                              "model": session.model, "status": session.status})

    def _handle_agent_get(self, route: str) -> None:
        from urllib.parse import parse_qs, urlsplit
        rest = route[len("/api/agent/"):]
        parts = rest.split("/")
        session = agent_sessions.get_session(parts[0])
        if session is None:
            self._send_json(404, {"error": "no such agent session (unknown id or expired)"})
            return
        query = parse_qs(urlsplit(self.path).query)

        if len(parts) == 1:                       # poll transcript
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send_json(200, session.snapshot(since=since))
            return

        if len(parts) == 2 and parts[1] == "files":   # list artifacts
            files = []
            root = session.workspace.resolve()
            for p in sorted(root.rglob("*")):
                if len(files) >= 500:
                    break
                rel = p.relative_to(root)
                #   hide agent plumbing (.claude/, .git/, any dotfiles)
                if any(seg.startswith(".") for seg in rel.parts):
                    continue
                if p.is_file():
                    st = p.stat()
                    files.append({"path": str(rel), "size": st.st_size,
                                  "mtime": int(st.st_mtime)})
            self._send_json(200, {"session_id": session.id, "files": files})
            return

        if len(parts) == 2 and parts[1] == "file":    # download one artifact
            rel = (query.get("path", [""])[0] or "").strip()
            root = session.workspace.resolve()
            target = (root / rel).resolve()
            #   same traversal guard as _serve_static: stay inside the workspace.
            if not rel or not target.is_relative_to(root) or not target.is_file():
                self._send_json(404, {"error": "no such file"})
                return
            size = target.stat().st_size
            if size > 50 * 1024 * 1024:
                self._send_json(413, {"error": "file too large to download (max 50MB)"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{target.name}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with target.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return

        self._send_json(404, {"error": "not found"})

    def _auth_and_body(self) -> dict | None:
        #   shared gate for POST routes: bearer auth + JSON body parse. on any
        #   failure it writes the error response and returns None.
        if not _token_ok(self.headers.get("Authorization")):
            if not _auth_configured():
                self._send_json(503, {"error": "no auth secret configured on server "
                                               "(set CRITIQUE_TOKEN or CRITIQUE_ROTATION_SECRET)"})
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

    @staticmethod
    def _privacy_and_store(payload: dict) -> tuple[str, bool]:
        #   privacy mode: strict (never use a training/logging host) | fallback (safe
        #   first, unsafe only as last resort) | off. default from DEFAULT_PRIVACY env
        #   (=strict). PRIVACY_MODE locks the whole space to strict regardless of request.
        lock = os.environ.get("PRIVACY_MODE", "").strip().lower()
        if lock in ("1", "true", "on", "strict"):
            privacy = "strict"
        else:
            privacy = str(payload.get("privacy") or os.environ.get("DEFAULT_PRIVACY", "strict")).strip().lower()
            if privacy not in ("strict", "fallback", "off"):
                raise _BadRequest("'privacy' must be one of ['strict','fallback','off']")
        no_store = bool(payload.get("no_store")) or \
            os.environ.get("NO_STORE", "").strip().lower() in ("1", "true", "on")
        return privacy, no_store

    def _handle_set_provider_key(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= 4096:
            self._send_json(400, {"error": "body required (1–4096 bytes)"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        oauth_token = str(payload.get("oauth_token", "")).strip()
        repo = str(payload.get("repo", "")).strip()
        key_name = str(payload.get("key_name", "")).strip()
        key_value = str(payload.get("key_value", "")).strip()
        if not all([oauth_token, repo, key_name, key_value]):
            self._send_json(400, {"error": "oauth_token, repo, key_name, key_value all required"})
            return
        # allowlist of safe provider keys (never allow setting auth secrets on the gateway)
        _ALLOWED = {
            "NVIDIA_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
            "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "GITHUB_TOKEN",
            "CF_API_TOKEN", "CF_ACCOUNT_ID", "ZAI_API_KEY", "MOONSHOT_API_KEY",
            "TAVILY_API_KEY", "ANTHROPIC_API_KEY",
        }
        if key_name not in _ALLOWED:
            self._send_json(400, {"error": f"key_name not allowed (must be one of {sorted(_ALLOWED)})"})
            return
        # verify the oauth token belongs to the repo owner
        try:
            whoami = _hf_api("https://huggingface.co/api/whoami-v2", token=oauth_token)
            username = whoami.get("name", "").strip()
            if not repo.lower().startswith(username.lower() + "/"):
                self._send_json(403, {"error": "repo does not belong to authenticated user"})
                return
        except Exception as exc:
            self._send_json(401, {"error": f"oauth_token invalid: {exc}"})
            return
        try:
            _hf_api(f"https://huggingface.co/api/spaces/{repo}/secrets",
                    method="POST", token=oauth_token,
                    body={"key": key_name, "value": key_value})
        except Exception as exc:
            self._send_json(502, {"error": f"HF API error: {exc}"})
            return
        self._send_json(200, {"ok": True, "set": key_name, "repo": repo})

    def do_POST(self) -> None:
        route = self.path.rstrip("/")
        if route == "/oauth/set-provider-key":
            self._handle_set_provider_key()
            return
        #   POST /api/agent/<sid>/interrupt — stop the in-flight turn (bearer-gated,
        #   no body required). handled before the body-parsing gate below.
        if route.startswith("/api/agent/") and route.endswith("/interrupt"):
            if not _token_ok(self.headers.get("Authorization")):
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            sid = route[len("/api/agent/"):-len("/interrupt")]
            session = agent_sessions.get_session(sid)
            if session is None:
                self._send_json(404, {"error": "no such agent session"})
                return
            stopped = session.interrupt()
            self._send_json(200, {"interrupted": stopped, "status": session.status})
            return
        if route not in ("/api/critique", "/api/panel", "/api/run", "/api/templates",
                         "/api/agent"):
            self._send_json(404, {"error": "not found"})
            return
        payload = self._auth_and_body()
        if payload is None:
            return
        if route == "/api/agent":
            self._handle_agent_post(payload)
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
        except JobCapExceeded as e:
            self._send_json(429, {"error": f"too many jobs in flight: {e}"},
                            headers={"Retry-After": "5"})
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
        except JobCapExceeded as e:
            self._send_json(429, {"error": f"too many jobs in flight: {e}"},
                            headers={"Retry-After": "5"})
        except Exception as e:  # noqa: BLE001 - never leak a stack trace
            log_event("request_error", route="/api/run", error=repr(e)[:300])
            self._send_json(500, {"error": f"internal error: {type(e).__name__}"})

    def _params_critique(self, payload: dict, panel: Panel) -> dict:
        repo_files = resolve_repo_files(payload)
        plan = payload.get("plan")
        if repo_files is None and (not isinstance(plan, str) or not plan.strip()):
            raise _BadRequest("'plan' (non-empty string) is required (or pass 'files'/'repo_url')")
        if repo_files is not None and plan is not None:
            raise _BadRequest("pass either 'plan' or a repo form ('files'/'repo_url'), not both")
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

        def _build(text: str) -> dict:
            return build_critique_params(panel, text, "markdown" if repo_files else fmt_req,
                                         panel_override or None, inline_rubric or None)

        if repo_files is not None:
            full_pack = repopack.pack_files(repo_files,
                                            include_lockfiles=bool(payload.get("include_lockfiles")))
            if not full_pack.files:
                raise _BadRequest("repo pack is empty after filtering (binaries/lockfiles only?)")
            params = _build(repopack.render(full_pack))
            params["profile"] = profile
            params["privacy"], params["no_store"] = self._privacy_and_store(payload)
            params = apply_effort(params, effort)
            return apply_repo_form(panel, params, full_pack, rebuild=_build)
        params = _build(plan)
        params["profile"] = profile
        params["privacy"], params["no_store"] = self._privacy_and_store(payload)
        return apply_effort(params, effort)

    def _params_panel(self, payload: dict, panel: Panel) -> dict:
        repo_files = resolve_repo_files(payload)
        input_text = payload.get("input")
        if repo_files is None and (not isinstance(input_text, str) or not input_text.strip()):
            raise _BadRequest("'input' (non-empty string) is required (or pass 'files'/'repo_url')")
        if repo_files is not None and input_text is not None:
            raise _BadRequest("pass either 'input' or a repo form ('files'/'repo_url'), not both")
        if repo_files is not None and payload.get("artifacts"):
            raise _BadRequest("'artifacts' is not supported with repo review (nonce per rebuild)")
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
        if max_tokens is not None and (not isinstance(max_tokens, int) or not 1 <= max_tokens <= 131072):
            raise _BadRequest("'max_tokens' must be an int in 1..131072")
        panel_override = payload.get("panel")
        if not self._valid_panel(panel_override):
            raise _BadRequest("'panel' must be a list of 'provider/model' strings")
        profile, effort = self._profile_and_effort(payload)

        def _build(text: str) -> dict:
            return build_panel_params(
                panel, input_text=text, role=role, system=system or None,
                instructions=payload.get("instructions"), output_rules=payload.get("output_rules"),
                template=payload.get("template"), panel_override=panel_override or None,
                merge_mode=merge_mode, max_tokens=max_tokens,
                want_artifacts=bool(payload.get("artifacts")),
                reasoning=bool(payload.get("reasoning")), research=bool(payload.get("research")),
            )

        if repo_files is not None:
            full_pack = repopack.pack_files(repo_files,
                                            include_lockfiles=bool(payload.get("include_lockfiles")))
            if not full_pack.files:
                raise _BadRequest("repo pack is empty after filtering (binaries/lockfiles only?)")
            params = _build(repopack.render(full_pack))
            params["profile"] = profile
            params["privacy"], params["no_store"] = self._privacy_and_store(payload)
            params = apply_effort(params, effort)
            return apply_repo_form(panel, params, full_pack, rebuild=_build)
        params = _build(input_text)
        params["profile"] = profile
        params["privacy"], params["no_store"] = self._privacy_and_store(payload)
        return apply_effort(params, effort)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent worker threads.

    Without a cap, a flood of concurrent connections spawns one thread each, exhausting
    memory/FDs (a trivial DoS on a free multi-user deployment). We gate thread creation
    with a BoundedSemaphore: when full, the connection gets an immediate 503 + Retry-After
    and is closed - honest backpressure, no thread spawned. The permit is released in a
    finally so a crashing handler can't leak capacity.
    """

    daemon_threads = True

    def __init__(self, *args, max_workers: int = MAX_WORKERS, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sem = threading.BoundedSemaphore(max(1, max_workers))
        self.max_workers = max(1, max_workers)
        self._busy = 0
        self._busy_lock = threading.Lock()

    def busy(self) -> int:
        with self._busy_lock:
            return self._busy

    def process_request(self, request, client_address) -> None:
        if not self._sem.acquire(blocking=False):
            #   over capacity: reject without spawning a worker.
            try:
                body = b'{"error":"server at capacity, retry shortly"}'
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Retry-After: 2\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            log_event("server_at_capacity", max_workers=self.max_workers)
            return
        with self._busy_lock:
            self._busy += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._busy_lock:
                self._busy -= 1
            self._sem.release()


def main() -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "7860")))

    panel = Panel()
    if not panel.providers:
        log_event("startup_warning", msg="no providers have API keys - every judge will fail")

    server = BoundedThreadingHTTPServer((host, port), Handler, max_workers=MAX_WORKERS)
    server.panel = panel  # type: ignore[attr-defined]
    server.jobs = JobRunner(panel, judge_timeout_s=JUDGE_TIMEOUT_S)  # type: ignore[attr-defined]

    log_event("startup", host=host, port=port,
              providers=[p.name for p in panel.providers],
              default_panel=panel.default_panel,
              token_configured=_auth_configured(),
              token_rotation=bool(os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip()))
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
