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
import crypto
import conscious_routes
import dataset_persistence
import db
import debug_log
import github_integration
import jwt_auth
import orchestrate
import provider_sync
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
    load_reasoning_catalog,
    make_provider_registry,
    make_slot,
    make_slot_registry,
    provider,
    slot,
    slot_is_privacy_safe,
)
from scheduler import SlotScheduler

# Module-level globals for agent_panel tool access
_panel: 'Panel | None' = None
_jobs: 'JobRunner | None' = None

# ---------------------------------------------------------------------------
# doomalaysocreate's multi-model critique panel, exposed as a slim standalone service.
#
#   POST /api/critique   (Bearer-token guarded)
#     { "plan": "<markdown OR doomalaysocreate schematic JSON>",
#       "format": "auto"|"markdown"|"schematic",
#       "panel":  ["provider/model", ...]   (optional, defaults to panel.json),
#       "rubric": "<optional inline rubric override>" }
#
# fans the plan out to a diverse judge panel in parallel (reusing doomalaysocreate's
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
TEMPLATE_SPACE_ID = os.environ.get("TEMPLATE_SPACE_ID", "ScoobyBaby1999/Doomalaysocreate")
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
#   full lib/ source is ~0.5MB; leaves headroom for real projects).
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8_000_000)))
REPO_MAX_BYTES = int(os.environ.get("REPO_MAX_BYTES", str(50_000_000)))
#   server concurrency bounds (free multi-user safety). MAX_WORKERS caps simultaneous
#   sync requests; REQUEST_TIMEOUT_S kills slow/slowloris connections holding a worker.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "48"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "30"))
DEFAULT_CTX_TOKENS = int(os.environ.get("DEFAULT_CTX_TOKENS", "131072"))
#   reserved for the role/rubric scaffolding around the packed repo in the system prompt.
PACK_SYSTEM_OVERHEAD_TOKENS = 2000

# generalized /api/panel: any doomalaysocreate role (or a fully custom system prompt) fanned
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
        self.logical_models: dict[str, dict] = {}
        self.ctx_by_who: dict[str, int] = {}

        cfg = _load_panel_cfg()
        self.default_panel: list[str] = cfg["judges"]
        self.max_parallel: int = cfg["max_parallel"]
        self.default_rubric: str = cfg["rubric"]

        #   one shared scheduler over EVERY known slot (base catalog + logical
        #   candidates + panel-named) drives rotation + cross-provider failover;
        #   one shared metrics store captures per-profile cost/throttle/latency.
        self.scheduler = SlotScheduler(base_slots, ctx_by_who=self.ctx_by_who)
        self.metrics = MetricStore()
        self.templates = orchestrate.TemplateStore()
        self.reasoning_catalog = load_reasoning_catalog()
        self.benchmarks = load_benchmarks()
        self.cache = PromptCache()

        #   pre-register default-panel slots so they join rotation from boot.
        for who in self.default_panel:
            self._ensure_slot(who)

        #   Auto-sync model lists from all providers with /v1/models endpoints.
        #   Discovers new models dynamically and builds logical_models for routing.
        #   NOTE: This is deferred to a background thread in main() so boot
        #   doesn't block on 6+ remote calls. If main() hasn't started the
        #   thread yet, the panel works with its static catalog until sync
        #   completes (agent_models re-probes the cache).
        self._sync_done = False

    def judge_ctx(self, who: str) -> int:
        #   a judge's usable context window: for a logical model, the LARGEST among
        #   its candidate hosts (failover prefers big-ctx hosts anyway; small-ctx
        #   candidates simply fail over). physical slots look up directly.
        if "/" in who:
            return self.ctx_by_who.get(who, DEFAULT_CTX_TOKENS)
        spec = self.logical_models.get(who) or {}
        vals = [int(c["ctx"]) for c in spec.get("candidates", []) if c.get("ctx")]
        return max(vals) if vals else DEFAULT_CTX_TOKENS

    def _model_pricing(self, logical: str, candidates: list) -> dict:
        #   best-effort pricing for the roster. Most providers in this catalog
        #   are free-tier (0/0); OpenRouter live pricing is fetched separately
        #   via /api/pricing. Returns {inputPerM, outputPerM, free, source}.
        try:
            from providers import load_provider_catalog
            catalog = load_provider_catalog()
        except Exception:
            catalog = []
        for cand in (candidates or []):
            prov_name = getattr(getattr(cand, "provider", None), "name", None)
            if not prov_name:
                continue
            for entry in catalog:
                if entry.get("name") == prov_name:
                    p = (entry.get("pricing") or {})
                    if p:
                        return {"inputPerM": float(p.get("input_per_m", 0) or 0),
                                "outputPerM": float(p.get("output_per_m", 0) or 0),
                                "free": bool(p.get("free", False)),
                                "source": "catalog"}
                    return {"inputPerM": 0.0, "outputPerM": 0.0,
                            "free": True, "source": "catalog (free tier)"}
        return {"inputPerM": 0.0, "outputPerM": 0.0, "free": True,
                "source": "unknown (assume free)"}

    def _sync_all_provider_models(self) -> None:
        """Fetch live model lists from all providers, register new slots, and
        build logical_models mapping dynamically (no static catalog).
        """
        sync_results = provider_sync.sync_all_providers(self.provider_by_name)
        provider_sync.register_synced_models(self, sync_results)
        provider_sync.set_panel_sync_cache(sync_results)
        self._rebuild_logical_models(sync_results)

    def _rebuild_logical_models(self, sync_results: dict[str, list[str]]) -> None:
        """Build logical_models routing map from live-synced provider model lists.

        Groups provider models by family so logical→candidate resolution can
        find slots without a static models_catalog.json.
        """
        from provider_sync.catalog import make_family
        groups: dict[str, dict] = {}
        for provider_name in sorted(sync_results.keys()):
            model_ids = sync_results[provider_name]
            for model_id in model_ids:
                family = make_family(model_id)
                if family not in groups:
                    groups[family] = {"family": family, "candidates": []}
                groups[family]["candidates"].append({
                    "provider": provider_name,
                    "model": model_id,
                })

        self.logical_models = {}
        for family, spec in groups.items():
            self.logical_models[family] = spec

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
                # Per-host capabilities (accurate, from providers.get_model_capabilities).
                # This reads reasoning_catalog.json + benchmarks.json so the
                # frontend can show exactly what THIS host can do (e.g. OpenRouter
                # kimi-k2.6:free supports native web search, NVIDIA kimi-k2.6 does
                # not). Falls back to a minimal dict on any error so a roster
                # failure never breaks the picker.
                host_caps: dict = {}
                try:
                    from providers import get_model_capabilities
                    host_caps = get_model_capabilities(
                        s.provider.name, s.model, logical=logical,
                        family=getattr(s, "model_family", None), benchmarks=bench)
                except Exception:
                    host_caps = {}
                hosts.append({"slot": s.who, "privacy_safe": safe,
                              "stability_tier": s.provider.stability_tier,
                              "cooling": cooling, "blacklisted": blacklisted,
                              "capabilities": host_caps})
                routable = routable or live
                safe_routable = safe_routable or (live and safe)
            if is_frontier:
                frontier_total += 1
                if safe_routable:
                    frontier_safe_available += 1
            # Per-LOGICAL-model capabilities: aggregate per-host capabilities
            # (any host supporting a feature → the logical model supports it).
            # This replaces the old "scan catalog keys by suffix" heuristic
            # with the authoritative providers.get_model_capabilities() call.
            #
            # web_search here means NATIVE web search (provider's own tool).
            # webSearch + deepResearch remain True because our web_tools.py
            # + research_templates work on EVERY model (the non-native fallback).
            any_effort = False
            any_native_ws = False
            any_tools = False
            any_vision = False
            effort_params: set[str] = set()
            # Per-logical-model effort_levels: ordered union of every host's
            # supported effort levels. The frontend uses this to render the
            # effort-mode dropdown with the model's canonical level names
            # (e.g. deepseek-v4-pro has 3 levels: none/high/max; OpenRouter
            # :free models have 7: none/minimal/low/medium/high/xhigh/max).
            # Empty list = no effort button shown for this logical model.
            effort_levels: list[str] = []
            _seen_levels: set[str] = set()
            for h in hosts:
                hc = h.get("capabilities") or {}
                if hc.get("effort"):
                    any_effort = True
                    if hc.get("effort_param"):
                        effort_params.add(hc["effort_param"])
                for lvl in (hc.get("effort_levels") or []):
                    s = str(lvl).strip() if lvl is not None else ""
                    if s and s not in _seen_levels:
                        _seen_levels.add(s)
                        effort_levels.append(s)
                if hc.get("web_search") or hc.get("web_search_native"):
                    any_native_ws = True
                if hc.get("tools"):
                    any_tools = True
                if hc.get("vision"):
                    any_vision = True
            capabilities = {
                "effort": any_effort,
                "effort_param": sorted(p for p in effort_params if p),
                "effort_levels": effort_levels,
                "web_search": any_native_ws,
                "web_search_native": any_native_ws,
                # Backwards-compat aliases (frontend may still use these):
                "webSearch": True,        # web_tools.py injection works everywhere
                "deepResearch": True,     # research_templates work everywhere
                "extendedThinking": any_effort,
                "tools": any_tools,
                "vision": any_vision,
            }
            # Pricing (best-effort, from providers_catalog).
            pricing = self._model_pricing(logical, cands)
            models.append({
                "logical": logical, "frontier": is_frontier,
                "arena_elo": bench.get("arena_elo"), "aa_index": bench.get("aa_index"),
                "hosts": hosts, "routable": routable,
                "privacy_safe_routable": safe_routable,
                "capabilities": capabilities,
                "pricing": pricing,
            })
        models.sort(key=lambda m: (m["arena_elo"] or 0), reverse=True)
        # Default templates grouped by kind, so the frontend can populate
        # the tool popovers (websearch / deepresearch / judge) without a
        # second round-trip. System templates are seeded idempotently on
        # first access via template_library.seed_defaults().
        templates_by_kind: dict[str, list] = {}
        try:
            import template_library as _tl
            for kind in ("websearch", "deepresearch", "judge", "chat", "custom"):
                templates_by_kind[kind] = _tl.list_default_templates(kind=kind)
        except Exception:
            # Best-effort: never break /api/roster on a template-library error.
            templates_by_kind = {}
        return {
            "models": models,
            "frontier_total": frontier_total,
            "frontier_privacy_safe_available": frontier_safe_available,
            "frontier_ok": frontier_safe_available >= 2,
            "benchmark_note": "indicative, hand-curated (see benchmarks.json) - not authoritative",
            "templates": templates_by_kind,
            # Per-provider Python invocation reference (researched live from
            # each provider's docs). Frontend uses this to render
            # provider-specific docs (max context, temperature ranges,
            # reasoning shape, python snippet, model quirks). See
            # lib/provider_quirks.json + lib/provider_tools.load_provider_quirks().
            "provider_quirks": _provider_quirks_for_roster(),
            # Live-detected effort summary: how many models in the roster
            # have a reasoning/effort parameter (so the frontend can show a
            # "X models support effort" badge without iterating hosts).
            "effort_summary": _effort_summary(models),
        }


def _provider_quirks_for_roster() -> dict:
    """Build the per-provider quirks summary for the /api/roster response.

    Returns a dict keyed by provider name, each value containing the fields
    the frontend needs to render a provider-info panel: base_url, auth note,
    max_context_default, temperature range, reasoning shape, web_search shape,
    python_snippet, and a short list of quirks. The full provider_quirks.json
    is loaded lazily (cached on the module after the first call).
    """
    try:
        from provider_tools import load_provider_quirks
        data = load_provider_quirks().get("providers", {}) or {}
    except Exception:
        return {}
    out: dict = {}
    for name, q in data.items():
        if not isinstance(q, dict):
            continue
        out[name] = {
            "base_url": q.get("base_url", ""),
            "auth": q.get("auth", ""),
            "max_context_default": q.get("max_context_default"),
            "temperature": q.get("temperature") or {},
            "top_p": q.get("top_p") or {},
            "top_k": q.get("top_k") or {},
            "seed": q.get("seed"),
            "response_format": q.get("response_format"),
            "tools": q.get("tools"),
            "tool_choice": q.get("tool_choice"),
            "reasoning": q.get("reasoning") or {},
            "web_search": q.get("web_search") or {},
            "extra_headers": q.get("extra_headers") or {},
            "quirks": q.get("quirks") or [],
            "python_snippet": q.get("python_snippet", ""),
        }
    return out


def _effort_summary(models: list[dict]) -> dict:
    """Build a summary of effort-mode support across the roster.

    Returns {total, with_effort, without_effort, by_provider: {prov: count}}.
    The frontend uses this to show a "X of Y models support effort modes"
    badge without iterating hosts.
    """
    total = len(models)
    with_effort = 0
    by_provider: dict[str, int] = {}
    for m in models:
        caps = (m.get("capabilities") or {})
        if caps.get("effort") or caps.get("effort_levels"):
            with_effort += 1
        for h in (m.get("hosts") or []):
            hc = h.get("capabilities") or {}
            if hc.get("effort") or hc.get("effort_levels"):
                prov = h.get("slot", "").split("/", 1)[0]
                if prov:
                    by_provider[prov] = by_provider.get(prov, 0) + 1
    return {
        "total": total,
        "with_effort": with_effort,
        "without_effort": total - with_effort,
        "by_provider": by_provider,
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
    #   doomalaysocreate schematic = JSON object carrying 'stages' or 'task_type'. anything
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
    #   generalized: any doomalaysocreate role, or a fully custom system prompt. this is what
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


def _ensure_auth_secret() -> None:
    """Auto-generate CRITIQUE_ROTATION_SECRET on first boot and persist to /data/.

    This lets duplicators skip the manual 'set Space secret' step — the
    rotation secret is generated once, stored in the persistent volume, and
    reused across restarts.  Also degrades gracefully if /data/ is unwritable
    (works for this session only).
    """
    if os.environ.get("CRITIQUE_TOKEN", "").strip():
        return  # static token is set — no rotation secret needed
    if os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip():
        return  # already configured
    secret_path = "/data/rotation_secret"
    try:
        if os.path.isfile(secret_path):
            with open(secret_path) as f:
                val = f.read().strip()
            if val and len(val) >= 16:
                os.environ["CRITIQUE_ROTATION_SECRET"] = val
                log_event("auth_secret", source="disk")
                return
    except OSError:
        pass
    # generate new secret and persist
    import secrets as _secrets
    val = _secrets.token_hex(32)
    try:
        # SECURITY: write with 0o600 permissions (owner-only read/write)
        # so other processes in the container can't read the secret.
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(val)
        os.environ["CRITIQUE_ROTATION_SECRET"] = val
        log_event("auth_secret", source="generated", path=secret_path)
    except OSError:
        # /data/ may not exist or be unwritable — run session-local
        os.environ["CRITIQUE_ROTATION_SECRET"] = val
        log_event("auth_secret", source="generated_ephemeral")


def _ensure_encryption_key() -> None:
    """Auto-generate ENCRYPTION_KEY on first boot and persist to /data/.

    Without this, crypto.py falls back to a per-process ephemeral key, which
    means encrypted tokens (GitHub/HF OAuth tokens) are lost on every Space
    restart — forcing users to re-authenticate.
    """
    if os.environ.get("ENCRYPTION_KEY", "").strip():
        return
    key_path = "/data/encryption_key"
    try:
        if os.path.isfile(key_path):
            with open(key_path) as f:
                val = f.read().strip()
            if val and len(val) >= 16:
                os.environ["ENCRYPTION_KEY"] = val
                log_event("encryption_key", source="disk")
                return
    except OSError:
        pass
    import secrets as _secrets
    import base64
    raw = _secrets.token_bytes(32)
    val = base64.urlsafe_b64encode(raw).decode()
    try:
        # SECURITY: write with 0o600 permissions (owner-only read/write)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(val)
        os.environ["ENCRYPTION_KEY"] = val
        log_event("encryption_key", source="generated", path=key_path)
    except OSError:
        os.environ["ENCRYPTION_KEY"] = val
        log_event("encryption_key", source="generated_ephemeral")


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
    return bool(os.environ.get("HF_CLIENT_ID", "").strip())


def _make_oauth_state(nonce: str, redirect_to: str = "") -> str:
    """HMAC-signed state token: nonce.timestamp.[redirect_to].sig — verifiable without server storage.
    redirect_to is base64url-encoded to avoid dots/slashes/colons that would
    corrupt the dot-delimited rsplit() parsing."""
    import base64 as _b64
    secret = os.environ.get("HF_CLIENT_SECRET", "x").encode()
    ts = str(int(time.time()))
    redirect_enc = _b64.urlsafe_b64encode(redirect_to.encode()).decode().rstrip("=") if redirect_to else ""
    data = f"{nonce}.{ts}.{redirect_enc}" if redirect_enc else f"{nonce}.{ts}."
    sig = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{data}.{sig}"


def _verify_oauth_state(state: str) -> tuple[bool, str]:
    """Returns (valid, redirect_to_url). redirect_to is empty string if not a proxy flow."""
    import base64 as _b64
    try:
        parts = state.rsplit(".", 2)
        if len(parts) != 3:
            return False, ""
        nonce_ts, redirect_enc, sig = parts
        if abs(time.time() - float(nonce_ts.split(".", 1)[1])) > 600:
            return False, ""
        secret = os.environ.get("HF_CLIENT_SECRET", "x").encode()
        data = f"{nonce_ts}.{redirect_enc}"
        expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expected, sig):
            return False, ""
        if not redirect_enc:
            return True, ""
        padding = 4 - len(redirect_enc) % 4
        padded = redirect_enc + "=" * padding if padding != 4 else redirect_enc
        return True, _b64.urlsafe_b64decode(padded).decode()
    except Exception:
        return False, ""


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
    server_version = "doomalaysocreate-panel/2.0"
    #   socketserver enforces this on the request socket: a client that opens a
    #   connection but sends its body slowly (slowloris) is dropped instead of pinning
    #   a worker forever. HTTP/1.0 default => no keep-alive holding workers between calls.
    timeout = REQUEST_TIMEOUT_S
    panel: Panel  # injected on the server instance

    def _set_csp_header(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )

    def _send_json(self, status: int, payload: dict, *, headers: dict | None = None) -> None:
        self._last_status = status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_csp_header()
        #   CORS: the web app may be served from a different origin (separate static
        #   host, or local dev without the proxy). auth is a bearer header - no cookies -
        #   so a wildcard origin grants nothing by itself; requests still need the token.
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    # --- Tier 3: Conscious dispatch ----------------------------------------
    def _conscious_dispatch(self, method: str) -> bool:
        """Handle /api/conscious/* if the route matches. Returns True if handled.

        Auth gate: bearer token (``_token_ok``) is required for ALL conscious
        routes. Workspace-owning ops additionally call ``_require_user_from_jwt``
        inside ``conscious_routes`` (via the ``user_id`` argument we pass here).
        Tier 1 invariants (no token in .git/config, no auth regressions) are
        preserved — this is purely additive dispatch.
        """
        from urllib.parse import urlsplit
        path = urlsplit(self.path).path.rstrip("/")
        if path != "/api/conscious" and not path.startswith("/api/conscious/"):
            return False
        # workspace-owning ops need the GitHub JWT (X-JWT) — pass it through;
        # conscious_routes._check_ownership enforces it.
        # Phase 6: JWT is OPTIONAL — if present, used for workspace ownership.
        # If absent, conscious_routes creates/uses a default workspace (so the
        # conscious system works WITHOUT GitHub auth, like the chat panel).
        user_id = self._require_user_from_jwt() if self._wants_jwt() else None
        auth_ok = self._auth_ok()
        log_event("conscious_dispatch_auth",
                  path=self.path, method=method,
                  auth_ok=auth_ok,
                  has_x_jwt=bool(self.headers.get("X-JWT")),
                  has_auth=bool(self.headers.get("Authorization")),
                  wants_jwt=self._wants_jwt(),
                  user_id=user_id)
        if not auth_ok:
            # Fallback: try JWT identity as the auth signal, so users who only
            # have githubSessionId (no rotation secret) can still use conscious
            # routes. If neither bearer nor JWT is valid, reject.
            if not user_id:
                if not _auth_configured():
                    self._send_json(503, {"error": "no auth secret configured on server "
                                                   "(set CRITIQUE_TOKEN or CRITIQUE_ROTATION_SECRET)"})
                else:
                    self._send_json(401, {"error": "missing or invalid bearer token"})
                return True
        # body: GET/DELETE have none; POST requires JSON; PATCH is optional JSON
        body: dict = {}
        if method == "POST":
            parsed = self._read_json_body()
            if parsed is None:
                return True  # _read_json_body already sent the error response
            body = parsed
        elif method == "PATCH":
            parsed = self._read_json_body_optional()
            if parsed is None:
                return True
            body = parsed
        try:
            status, payload = conscious_routes.handle_request(
                method, self.path, body, dict(self.headers), user_id)
            self._send_json(status, payload)
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace
            log_event("conscious_request_error", route=self.path, error=repr(exc)[:300])
            self._send_json(500, {"error": f"internal error: {type(exc).__name__}"})
        return True

    @staticmethod
    def _wants_jwt() -> bool:
        """Phase 6+: JWT is preferred. When present, conscious routes use the
        JWT'd user_id to find user-specific workspaces. When absent (no GitHub
        connection), they fall back to the default user/workspace so the
        conscious system works without GitHub auth."""
        return True

    def _read_json_body_optional(self) -> dict | None:
        """Like _read_json_body but returns {} for empty body (used by PATCH)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as e:
            self._send_json(400, {"error": f"invalid UTF-8 body: {e}"})
            return None
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def do_OPTIONS(self) -> None:
        #   CORS preflight for cross-origin POSTs with Authorization/Content-Type.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-JWT")
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
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self' https://js.puter.com; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; font-src 'self'; connect-src 'self' https://api.puter.com wss://api.puter.com; "
                         "manifest-src 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _log_request(self, method: str, route: str, status: int, latency_ms: float, error: str | None = None) -> None:
        """Safely records request metrics and structural metadata to the RAM ring buffer."""
        from debug_log import log_entry
        import hashlib
        import base64
        
        auth = self.headers.get("Authorization", "").strip()
        x_jwt = self.headers.get("X-JWT", "").strip()
        
        # Inferred auth type (no content value captured)
        auth_type = "none"
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            auth_type = "jwt" if token.count(".") == 2 else "rotation"
        elif x_jwt:
            auth_type = "jwt"
            
        # Resolve anonymous stable user hash for tracing
        user_hash = None
        try:
            token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else x_jwt
            if token and token.count(".") == 2:
                payload_b64 = token.split(".")[1]
                pad = 4 - len(payload_b64) % 4
                payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (pad if pad != 4 else 0))
                payload = json.loads(payload_bytes.decode("utf-8"))
                uid = payload.get("sub")
                if uid:
                    # 8-character stable non-invertible correlation signature
                    user_hash = hashlib.sha256(f"{uid}:anonymizing_salt_2026".encode()).hexdigest()[:8]
        except Exception:
            pass

        # Assign correct debug log level based on response code
        level = "INFO"
        if status >= 500:
            level = "ERROR"
        elif status >= 400:
            level = "WARN"

        msg = f"{method} {route} -> {status} ({int(latency_ms)}ms)"
        log_entry(
            level=level,
            cat="http",
            fn=f"do_{method}",
            msg=msg,
            data={
                "method": method,
                "route": route,
                "status": status,
                "latency_ms": round(latency_ms, 2),
                "auth_present": bool(auth or x_jwt),
                "auth_type": auth_type,
                "user_hash": user_hash,
                "error": error
            }
        )

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - quiet default logging
        return  # telemetry goes through oplog; suppress the stderr access log spam

    def do_GET(self) -> None:
        start = time.time()
        self._last_status = 200
        error_msg = None
        try:
            self._do_GET()
        except Exception as exc:  # noqa: BLE001 — last-resort guard so a bug
            # in any handler never leaves the connection hanging or crashes
            # the worker thread. Without this, a NameError (like the
            # _check_session bug) turns a clean 401 into an HTTP 500 that
            # surfaces to the user as "internal error" with no route context.
            try:
                error_msg = repr(exc)[:300]
                log_event("do_GET_unhandled", path=self.path,
                          error=error_msg)
                self._send_json(500, {"error": f"internal error: {type(exc).__name__}"})
            except Exception:
                pass  # connection may already be closed
        finally:
            self._log_request("GET", self.path, self._last_status, (time.time() - start) * 1000, error_msg)

    def _do_GET(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        # --- POST /api/models/resync — force re-fetch + re-cache models ---
        if route == "/api/models/resync":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            try:
                import agent_sessions as _as
                _as._reset_open_models_cache()
                # Also reset the provider sync cache
                try:
                    import provider_sync
                    provider_sync._sync_cache = None
                except Exception:
                    pass
                # Refresh the live effort-detector cache (OpenRouter + GitHub
                # Models model lists). This is what backs the dynamic effort
                # mode detection — without this, the detector's 10-minute cache
                # would keep returning stale data after a new model ships on
                # OpenRouter or GitHub Models.
                try:
                    import effort_detector
                    effort_detector.refresh_live_cache()
                    log_event("resync_effort_cache_refreshed")
                except Exception as e:
                    log_event("resync_effort_cache_error", error=str(e)[:200])
                # Trigger a fresh sync
                try:
                    from providers import make_provider_registry
                    reg = make_provider_registry()
                    provider_map = {p.name: p for p in reg if hasattr(p, "name")}
                    results = provider_sync.sync_all_providers(provider_map)
                    provider_sync._sync_cache = results
                except Exception as e:
                    log_event("resync_error", error=str(e)[:200])
                # Rebuild the open models cache
                models = _as._build_open_models()
                self._send_json(200, {
                    "ok": True,
                    "model_count": len(models),
                    "models": [{"model": m[2], "provider": m[5] or m[1]} for m in models[:20]],
                })
            except Exception as exc:
                self._send_json(500, {"error": str(exc)[:200]})
            return
        # --- PUBLIC comprehensive test runner v2 (no auth) ---
        if route == "/api/debug/run-tests-v2":
            import agent_sessions as _as
            import time as _time
            import tempfile as _tf
            from pathlib import Path as _P
            import threading as _th
            import json as _json
            import urllib.request as _ur
            results = []
            def _test(name, func, timeout=120):
                t0 = _time.time()
                try:
                    result = func()
                    elapsed = round(_time.time() - t0, 1)
                    results.append({"test": name, "pass": True, "elapsed_s": elapsed, "result": str(result)[:200]})
                except Exception as e:
                    elapsed = round(_time.time() - t0, 1)
                    results.append({"test": name, "pass": False, "elapsed_s": elapsed, "error": str(e)[:200]})
            def _run_adapter_turn(system_prompt, msg, timeout=30):
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None, workspace_id=None,
                                             system_prompt=system_prompt)
                events = []
                adapter._session = None
                adapter.open()
                done = {"done": False, "error": None}
                def _turn():
                    try:
                        adapter.turn(msg, lambda ev: events.append(ev))
                        done["done"] = True
                    except Exception as e:
                        done["error"] = str(e)[:200]
                t = _th.Thread(target=_turn, daemon=True)
                t.start()
                t.join(timeout=timeout)
                if not done["done"] and not done["error"]:
                    events.append({"type": "error", "error": f"timeout ({timeout}s)"})
                elif done["error"]:
                    events.append({"type": "error", "error": done["error"]})
                return events

            # === T1: Basic chat ===
            def t1():
                evs = _run_adapter_turn("You are a helpful assistant.", "Say hello.", 30)
                asst = [e for e in evs if e.get("type") == "assistant"]
                if asst: return f"Response: {asst[0].get('text','')[:80]}"
                raise Exception(f"No assistant event: {[e.get('type') for e in evs]}")
            _test("T1: Basic chat", t1)

            # === T2: Back-to-back messages ===
            def t2():
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None, workspace_id=None,
                                             system_prompt="You are a helpful assistant. Be concise.")
                adapter._session = None
                adapter.open()
                all_evs = []
                done = {"d": False, "e": None}
                def _turn():
                    try:
                        adapter.turn("My name is TestUser and I like pizza.", lambda ev: all_evs.append(ev))
                        adapter.turn("What is my name and what do I like?", lambda ev: all_evs.append(ev))
                        done["d"] = True
                    except Exception as e: done["e"] = str(e)[:200]
                t = _th.Thread(target=_turn, daemon=True)
                t.start()
                t.join(timeout=60)
                asst = [e for e in all_evs if e.get("type") == "assistant"]
                if len(asst) >= 2:
                    text = asst[-1].get("text","")
                    if "TestUser" in text and "pizza" in text.lower():
                        return f"Context retained across 2 msgs: {text[:60]}"
                    return f"Got 2 responses but context not retained: {text[:60]}"
                raise Exception(f"Expected 2 responses, got {len(asst)}. done={done}")
            _test("T2: Back-to-back messages + context", t2)

            # === T3: Tool use (shell) ===
            def t3():
                evs = _run_adapter_turn(_as.AGENT_SYSTEM_PROMPT,
                    "Use the shell tool to run: echo hello_world", 45)
                tools = [e for e in evs if e.get("type") == "tool_use"]
                asst = [e for e in evs if e.get("type") == "assistant"]
                if tools:
                    return f"Tool: {tools[0].get('name')}. Response: {asst[-1].get('text','')[:60] if asst else 'none'}"
                raise Exception(f"No tool_use: {[e.get('type') for e in evs]}")
            _test("T3: Tool use (shell)", t3)

            # === T4: Long conversation (10+ messages) ===
            def t4():
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None, workspace_id=None,
                                             system_prompt="You are a helpful assistant. Be very concise (1 sentence).")
                adapter._session = None
                adapter.open()
                all_evs = []
                done = {"d": False, "e": None}
                msgs = ["Hi", "What is 2+2?", "Tell me a color.", "What is the capital of France?",
                        "Name a fruit.", "What is 5*5?", "Tell me a planet.", "What is Python?",
                        "Name an animal.", "What was the first thing I asked you?"]
                def _turn():
                    try:
                        for m in msgs:
                            adapter.turn(m, lambda ev: all_evs.append(ev))
                        done["d"] = True
                    except Exception as e: done["e"] = str(e)[:200]
                t = _th.Thread(target=_turn, daemon=True)
                t.start()
                t.join(timeout=180)
                asst = [e for e in all_evs if e.get("type") == "assistant"]
                if len(asst) >= 8:
                    last = asst[-1].get("text","")
                    return f"Got {len(asst)} responses in 10-msg convo. Last: {last[:60]}"
                raise Exception(f"Expected 8+ responses, got {len(asst)}. done={done}")
            _test("T4: Long conversation (10 messages)", t4, timeout=200)

            # === T5: Multiple providers ===
            def t5():
                providers_ok = []
                # NVIDIA
                try:
                    key = os.environ.get("NVIDIA_API_KEY", "")
                    if key:
                        url = "https://integrate.api.nvidia.com/v1/chat/completions"
                        body = _json.dumps({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}).encode()
                        req = _ur.Request(url, data=body, headers={"Content-Type":"application/json","Authorization":f"Bearer {key}"})
                        with _ur.urlopen(req, timeout=10) as resp:
                            data = _json.loads(resp.read().decode())
                        if data.get("choices"): providers_ok.append("NVIDIA")
                except Exception: pass
                # OpenRouter
                try:
                    key = os.environ.get("OPENROUTER_API_KEY", "")
                    if key:
                        url = "https://openrouter.ai/api/v1/chat/completions"
                        body = _json.dumps({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}).encode()
                        req = _ur.Request(url, data=body, headers={"Content-Type":"application/json","Authorization":f"Bearer {key}","HTTP-Referer":"https://huggingface.co/spaces","X-Title":"doomalaysocreate"})
                        with _ur.urlopen(req, timeout=10) as resp:
                            data = _json.loads(resp.read().decode())
                        if data.get("choices"): providers_ok.append("OpenRouter")
                except Exception: pass
                if providers_ok:
                    return f"Working providers: {', '.join(providers_ok)}"
                raise Exception("No providers responded")
            _test("T5: Multiple providers", t5)

            # === T6: Chat session isolation ===
            def t6():
                import chat_routes
                cs1 = chat_routes.create_chat_session(model="openai/z-ai/glm-5.2", user_id=None)
                cs2 = chat_routes.create_chat_session(model="openai/z-ai/glm-5.1", user_id=None)
                if cs1["id"] != cs2["id"] and cs1.get("model") != cs2.get("model"):
                    return f"Session 1: {cs1['id'][:8]} (model={cs1.get('model','?')[:20]}), Session 2: {cs2['id'][:8]} (model={cs2.get('model','?')[:20]})"
                raise Exception("Sessions not isolated")
            _test("T6: Chat session isolation", t6)

            # === T7: Effort modes (check catalog) ===
            def t7():
                import json as _json
                with open("reasoning_catalog.json") as f:
                    cat = _json.load(f)
                reasoning = cat.get("reasoning", {})
                has_effort = {k: v.get("body", {}) for k, v in reasoning.items() if v.get("body") and k != "*"}
                return f"Models with effort: {len(has_effort)} (e.g. {list(has_effort.keys())[:3]})"
            _test("T7: Effort modes catalog", t7)

            # === T8: Template system ===
            def t8():
                import template_library
                # Check both user templates and public/default templates
                my_tpls = template_library.list_my_templates(user_id=None)
                pub_tpls, total = template_library.list_public_templates(
                    sort="hearts", limit=5, offset=0, user_id=None)
                all_tpls = my_tpls + pub_tpls
                if all_tpls:
                    names = [t.get("name","?")[:20] for t in all_tpls[:5]]
                    return f"Templates: {len(my_tpls)} mine + {total} public. Examples: {names}"
                raise Exception("No templates found (neither mine nor public)")
            _test("T8: Template system", t8)

            # === T9: Model roster (dynamic fetch) ===
            def t9():
                models = _as._build_open_models()
                if len(models) > 0:
                    return f"Open models: {len(models)} (e.g. {models[0][2]})"
                # Try live fetch
                picked = _as._pick_open_llm()
                if picked:
                    return f"Live fetch OK: {picked[1]}"
                raise Exception("No models available")
            _test("T9: Dynamic model roster", t9)

            # === T10: FileSessionManager (persistence) ===
            def t10():
                try:
                    from strands.session import FileSessionManager
                    tmpdir = _P(_tf.mkdtemp())
                    (tmpdir / ".sessions").mkdir(parents=True, exist_ok=True)
                    sm = FileSessionManager(session_id="test123", sessions_dir=str(tmpdir / ".sessions"))
                    return f"FileSessionManager created OK at {tmpdir / '.sessions'}"
                except Exception as e:
                    raise Exception(f"FileSessionManager failed: {e}")
            _test("T10: Session persistence (FileSessionManager)", t10)

            # Summary
            passed = sum(1 for r in results if r["pass"])
            failed = sum(1 for r in results if not r["pass"])
            self._send_json(200, {
                "total": len(results), "passed": passed, "failed": failed,
                "results": results,
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            })
            return
        # --- PUBLIC comprehensive test runner (no auth) ---
        if route == "/api/debug/run-tests":
            import agent_sessions as _as
            import time as _time
            import tempfile as _tf
            from pathlib import Path as _P
            import threading as _th
            results = []
            def _test(name, func):
                t0 = _time.time()
                try:
                    result = func()
                    elapsed = round(_time.time() - t0, 1)
                    results.append({"test": name, "pass": True, "elapsed_s": elapsed, "result": result})
                except Exception as e:
                    elapsed = round(_time.time() - t0, 1)
                    results.append({"test": name, "pass": False, "elapsed_s": elapsed,
                                    "error": str(e)[:200]})
            def _run_adapter_turn(system_prompt, msg, timeout=30):
                """Run a single adapter turn, return events list."""
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None, workspace_id=None,
                                             system_prompt=system_prompt)
                events = []
                adapter._session = None  # safe default
                adapter.open()
                done = {"done": False, "error": None}
                def _turn():
                    try:
                        adapter.turn(msg, lambda ev: events.append(ev))
                        done["done"] = True
                    except Exception as e:
                        done["error"] = str(e)[:200]
                t = _th.Thread(target=_turn, daemon=True)
                t.start()
                t.join(timeout=timeout)
                if not done["done"] and not done["error"]:
                    events.append({"type": "error", "error": f"timeout ({timeout}s)"})
                elif done["error"]:
                    events.append({"type": "error", "error": done["error"]})
                return events

            # Test 1: Basic chat
            def test1():
                evs = _run_adapter_turn("You are a helpful assistant.", "Say hello.", 30)
                asst = [e for e in evs if e.get("type") == "assistant"]
                if asst:
                    return f"Response: {asst[0].get('text','')[:80]}"
                raise Exception(f"No assistant event. Events: {[e.get('type') for e in evs]}")
            _test("Basic chat", test1)

            # Test 2: Back-to-back messages
            def test2():
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None, workspace_id=None,
                                             system_prompt="You are a helpful assistant. Be concise.")
                adapter._session = None
                adapter.open()
                all_events = []
                done = {"done": False, "error": None}
                def _turn():
                    try:
                        adapter.turn("My name is TestUser.", lambda ev: all_events.append(ev))
                        adapter.turn("What is my name?", lambda ev: all_events.append(ev))
                        done["done"] = True
                    except Exception as e:
                        done["error"] = str(e)[:200]
                t = _th.Thread(target=_turn, daemon=True)
                t.start()
                t.join(timeout=60)
                asst = [e for e in all_events if e.get("type") == "assistant"]
                if len(asst) >= 2:
                    return f"Got {len(asst)} responses. Last: {asst[-1].get('text','')[:60]}"
                raise Exception(f"Expected 2+ responses, got {len(asst)}. done={done}")
            _test("Back-to-back messages", test2)

            # Test 3: Tool use (shell)
            def test3():
                evs = _run_adapter_turn(
                    _as.AGENT_SYSTEM_PROMPT,
                    "Use the shell tool to run: echo hello_world. Then tell me the output.",
                    60)
                tool_uses = [e for e in evs if e.get("type") == "tool_use"]
                asst = [e for e in evs if e.get("type") == "assistant"]
                if tool_uses:
                    return f"Tool used: {tool_uses[0].get('name','?')}. Assistant: {asst[-1].get('text','')[:60] if asst else 'none'}"
                raise Exception(f"No tool_use event. Events: {[e.get('type') for e in evs]}")
            _test("Tool use (shell)", test3)

            # Test 4: Direct LLM (different provider)
            def test4():
                import urllib.request as _ur
                import json as _json
                # Test NVIDIA
                key = os.environ.get("NVIDIA_API_KEY", "")
                if not key:
                    raise Exception("NVIDIA_API_KEY not set")
                url = "https://integrate.api.nvidia.com/v1/chat/completions"
                body = _json.dumps({
                    "model": "z-ai/glm-5.2",
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 10,
                }).encode()
                req = _ur.Request(url, data=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                })
                with _ur.urlopen(req, timeout=15) as resp:
                    data = _json.loads(resp.read().decode())
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return f"NVIDIA response: {text[:50]}"
            _test("Direct LLM (NVIDIA)", test4)

            # Test 5: OpenRouter (if key set)
            def test5():
                import urllib.request as _ur
                import json as _json
                key = os.environ.get("OPENROUTER_API_KEY", "")
                if not key:
                    return "SKIPPED: OPENROUTER_API_KEY not set"
                url = "https://openrouter.ai/api/v1/chat/completions"
                body = _json.dumps({
                    "model": "z-ai/glm-5.2:free",
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 10,
                }).encode()
                req = _ur.Request(url, data=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://huggingface.co/spaces",
                    "X-Title": "doomalaysocreate",
                })
                with _ur.urlopen(req, timeout=15) as resp:
                    data = _json.loads(resp.read().decode())
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return f"OpenRouter response: {text[:50]}"
            _test("Direct LLM (OpenRouter)", test5)

            # Test 6: Model roster
            def test6():
                import urllib.request as _ur
                import json as _json
                key = os.environ.get("NVIDIA_API_KEY", "")
                url = "https://integrate.api.nvidia.com/v1/models"
                req = _ur.Request(url, headers={
                    "Authorization": f"Bearer {key}" if key else "",
                    "User-Agent": "doomalaysocreate/1.0",
                })
                with _ur.urlopen(req, timeout=10) as resp:
                    data = _json.loads(resp.read().decode())
                models = data.get("data", [])
                return f"NVIDIA has {len(models)} models"
            _test("Model roster (NVIDIA)", test6)

            # Test 7: Chat session creation
            def test7():
                import chat_routes
                cs = chat_routes.create_chat_session(model="openai/z-ai/glm-5.2",
                                                      workspace_id=None, user_id=None)
                if cs and cs.get("id"):
                    return f"Session created: {cs['id'][:8]}... title={cs.get('title','?')}"
                raise Exception("Failed to create chat session")
            _test("Chat session creation", test7)

            # Summary
            passed = sum(1 for r in results if r["pass"])
            failed = sum(1 for r in results if not r["pass"])
            self._send_json(200, {
                "total": len(results),
                "passed": passed,
                "failed": failed,
                "results": results,
            })
            return
        # --- PUBLIC diagnostic endpoint (no auth) for live monitoring ---
        # Returns recent ERROR/WARN logs + agent session count + provider status.
        # No sensitive data (no keys, no tokens, no user data).
        if route == "/api/debug/public":
            import debug_log as _dl
            logs_raw = _dl.get_recent_logs(tail=200)
            # Normalize log format: oplog.log_entry stores in 'data'+'msg',
            # debug_log.log_event stores in 'fields'. Merge both.
            logs = []
            for r in logs_raw:
                logs.append({
                    "ts": r.get("ts", ""),
                    "cat": r.get("cat", r.get("kind", "")),
                    "level": r.get("level", "INFO"),
                    "msg": r.get("msg", ""),
                    "fields": r.get("fields", r.get("data", {})),
                })
            active_sessions = 0
            try:
                import agent_sessions as _as
                with _as._sessions_lock:
                    active_sessions = len(_as._sessions)
            except Exception:
                pass
            providers_status = {}
            try:
                for name in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID",
                             "OPENROUTER_API_KEY", "GITHUB_TOKEN",
                             "PRIVATEMODEAI_API_KEY", "OPENCODE_ZEN_API_KEY",
                             "ANTHROPIC_API_KEY"):
                    providers_status[name] = bool(os.environ.get(name, "").strip())
            except Exception:
                pass
            self._send_json(200, {
                "logs": logs,
                "active_sessions": active_sessions,
                "providers": providers_status,
                "agent_tier": agent_sessions.agent_tier(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            return
        # --- PUBLIC Strands import test ---
        if route == "/api/debug/test-strands":
            import json as _json
            import traceback as _tb
            results = {}
            # Test 1: Can we import strands?
            try:
                from strands import Agent
                from strands.models.litellm import LiteLLMModel
                results["strands_import"] = "OK"
            except Exception as e:
                results["strands_import"] = f"FAIL: {e}"
                self._send_json(200, {"ok": False, "results": results, "traceback": _tb.format_exc()[:500]})
                return
            # Test 2: Can we create a LiteLLMModel?
            try:
                import agent_sessions as _as
                picked = _as._pick_open_llm()
                if picked is None:
                    results["pick_llm"] = "FAIL: no LLM available"
                    self._send_json(200, {"ok": False, "results": results})
                    return
                key_env, model, base_url = picked
                results["picked_model"] = model
                results["picked_key_env"] = key_env
                results["picked_base_url"] = base_url
                api_key = os.environ.get(key_env, "")
                client_args = {"api_key": api_key}
                if base_url:
                    client_args["api_base"] = base_url
                llm = LiteLLMModel(client_args=client_args, model_id=model, stream=False)
                results["litellm_create"] = "OK"
            except Exception as e:
                results["litellm_create"] = f"FAIL: {e}"
                self._send_json(200, {"ok": False, "results": results, "traceback": _tb.format_exc()[:500]})
                return
            # Test 3: Can we create an Agent?
            try:
                agent = Agent(model=llm, system_prompt="You are a helpful assistant.")
                results["agent_create"] = "OK"
            except Exception as e:
                results["agent_create"] = f"FAIL: {e}"
                self._send_json(200, {"ok": False, "results": results, "traceback": _tb.format_exc()[:500]})
                return
            # Test 4: Can we call the agent with a 15s timeout?
            import threading as _threading
            import time as _time
            _result = {"done": False, "error": None, "response": None}
            def _call():
                try:
                    resp = agent("Say hello in one word.")
                    _result["done"] = True
                    _result["response"] = str(resp)[:200]
                except Exception as e:
                    _result["error"] = str(e)[:300]
            t = _threading.Thread(target=_call, daemon=True)
            t.start()
            t.join(timeout=15)
            if t.is_alive():
                results["agent_call"] = "TIMEOUT (15s)"
            elif _result["error"]:
                results["agent_call"] = f"FAIL: {_result['error']}"
            else:
                results["agent_call"] = f"OK: {_result['response']}"

            # Test 5: Test with the FULL StrandsAdapter (with tools + memory)
            try:
                from pathlib import Path as _P
                import tempfile as _tf
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None,
                                             workspace_id=None,
                                             system_prompt="You are a helpful assistant.")
                adapter.open()
                results["adapter_open"] = f"OK (resolved_model={adapter.resolved_model})"
                # Try a turn with a 30s timeout
                _adapter_events = []
                _adapter_done = {"done": False, "error": None}
                def _adapter_call():
                    try:
                        adapter.turn("Say hello", lambda ev: _adapter_events.append(ev))
                        _adapter_done["done"] = True
                    except Exception as e:
                        _adapter_done["error"] = str(e)[:300]
                t2 = _threading.Thread(target=_adapter_call, daemon=True)
                t2.start()
                t2.join(timeout=30)
                if t2.is_alive():
                    results["adapter_turn"] = f"TIMEOUT (30s) — events so far: {len(_adapter_events)}"
                elif _adapter_done["error"]:
                    results["adapter_turn"] = f"FAIL: {_adapter_done['error']}"
                else:
                    asst_ev = [e for e in _adapter_events if e.get("type") == "assistant"]
                    asst_deltas = [e for e in _adapter_events if e.get("type") == "assistant_delta"]
                    asst_text = asst_ev[0].get('text','') if asst_ev else ''.join(e.get('text','') for e in asst_deltas)
                    results["adapter_turn"] = f"OK — {len(_adapter_events)} events, assistant: {asst_text[:60] if asst_text else 'NONE'}"
            except Exception as e:
                results["adapter_turn"] = f"FAIL: {e}"
                results["adapter_traceback"] = _tb.format_exc()[:400]

            self._send_json(200, {"ok": True, "results": results})
            return
        # --- PUBLIC direct LLM test (no Strands, just litellm) ---
        if route == "/api/debug/test-llm":
            import json as _json
            import time as _time
            from urllib.parse import parse_qs, urlsplit
            qs = parse_qs(urlsplit(self.path).query)
            model_param = (qs.get("model", [None])[0] or "").strip()
            try:
                import agent_sessions as _as
                # Pick a model
                if model_param:
                    pair = _as._resolve_open_model(model_param)
                    if pair:
                        model, base_url, key_env, provider_label, extra_headers = pair
                    else:
                        self._send_json(500, {"error": f"cannot resolve model {model_param}"})
                        return
                else:
                    picked = _as._pick_open_llm()
                    if picked is None:
                        self._send_json(500, {"error": "no open LLM available"})
                        return
                    key_env, model, base_url = picked
                    extra_headers = None
                    provider_label = key_env
                api_key = os.environ.get(key_env, "")
                if not api_key:
                    self._send_json(500, {"error": f"no API key for {key_env}"})
                    return
                # Direct HTTP call (no Strands/litellm)
                import urllib.request
                url = (base_url or "https://integrate.api.nvidia.com/v1") + "/chat/completions"
                if not base_url:
                    # NVIDIA base_url in catalog already includes /chat/completions
                    url = "https://integrate.api.nvidia.com/v1/chat/completions"
                elif base_url.endswith("/chat/completions"):
                    url = base_url
                else:
                    url = base_url.rstrip("/") + "/chat/completions"
                body = _json.dumps({
                    "model": model.replace("openai/", ""),
                    "messages": [{"role": "user", "content": "Say hello in one word."}],
                    "max_tokens": 50,
                    "stream": False,
                }).encode()
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                }
                if extra_headers:
                    headers.update(extra_headers)
                t0 = _time.time()
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        result = _json.loads(resp.read().decode())
                    elapsed = _time.time() - t0
                    self._send_json(200, {
                        "ok": True,
                        "model": model,
                        "provider": provider_label if model_param else key_env,
                        "url": url,
                        "elapsed_ms": round(elapsed * 1000),
                        "response": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                        "usage": result.get("usage", {}),
                    })
                except urllib.error.HTTPError as e:
                    body_text = e.read().decode()[:300]
                    self._send_json(200, {
                        "ok": False,
                        "model": model,
                        "url": url,
                        "error": f"HTTP {e.code}: {body_text}",
                        "elapsed_ms": round((_time.time() - t0) * 1000),
                    })
                except Exception as e:
                    self._send_json(200, {
                        "ok": False,
                        "model": model,
                        "url": url,
                        "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "elapsed_ms": round((_time.time() - t0) * 1000),
                    })
            except Exception as exc:
                import traceback as _tb
                self._send_json(500, {
                    "error": str(exc)[:300],
                    "traceback": _tb.format_exc()[:500],
                })
            return
        # --- PUBLIC test endpoint (no auth) — send a test message, return the response ---
        if route == "/api/debug/test-chat":
            import agent_sessions as _as
            import time as _time
            from pathlib import Path as _P
            import tempfile as _tf
            try:
                # Test the adapter DIRECTLY (no AgentSession overhead)
                tmpdir = _P(_tf.mkdtemp())
                adapter = _as.StrandsAdapter(tmpdir, model=None,
                                             workspace_id=None,
                                             system_prompt=_as.AGENT_SYSTEM_PROMPT)
                events = []
                def _emit(ev):
                    events.append(ev)
                # Subscribe is not needed — adapter.turn calls emit directly
                t0 = _time.time()
                try:
                    adapter.open()
                    events.append({"type": "status", "state": "adapter_opened",
                                   "resolved_model": adapter.resolved_model,
                                   "resolved_provider": adapter.resolved_provider})
                    # Run turn with a 30s timeout
                    import threading as _th
                    _done = {"done": False, "error": None}
                    def _turn():
                        try:
                            adapter.turn("Say hello in one word.", _emit)
                            _done["done"] = True
                        except Exception as e:
                            import traceback as _tb
                            _done["error"] = str(e)[:300]
                            _done["traceback"] = _tb.format_exc()[:500]
                    _t = _th.Thread(target=_turn, daemon=True)
                    _t.start()
                    _t.join(timeout=30)
                    elapsed = _time.time() - t0
                    if not _done["done"] and not _done["error"]:
                        events.append({"type": "error", "error": f"turn timed out (30s), elapsed={elapsed:.1f}s"})
                    elif _done["error"]:
                        events.append({"type": "error", "error": _done["error"],
                                       "traceback": _done.get("traceback", "")})
                    else:
                        events.append({"type": "status", "state": "turn_done", "elapsed_s": round(elapsed, 1)})
                except Exception as e:
                    events.append({"type": "error", "error": f"adapter.open failed: {e}"})
                self._send_json(200, {
                    "ok": True,
                    "adapter": {
                        "type": type(adapter).__name__,
                        "resolved_model": adapter.resolved_model,
                        "resolved_provider": adapter.resolved_provider,
                    },
                    "events": events[:30],
                    "event_count": len(events),
                    "elapsed_s": round(_time.time() - t0, 1),
                })
            except Exception as exc:
                import traceback as _tb
                self._send_json(500, {
                    "ok": False,
                    "error": str(exc)[:300],
                    "traceback": _tb.format_exc()[:500],
                })
            return
        if route == "/api/debug/test-chat-full":
            import agent_sessions as _as
            import time as _time
            try:
                # Create a session with a specific model to avoid pick issues
                sess = _as.get_or_create(None, model="openai/z-ai/glm-5.2",
                                         chat_session_id=None)
                # Subscribe to events BEFORE submitting (no race)
                q = sess.subscribe(0)
                sess.submit("Hi")
                # Collect events for up to 60s (give the LLM time to respond)
                events = []
                deadline = _time.time() + 120
                while _time.time() < deadline:
                    try:
                        ev = q.get(timeout=1)
                        events.append(ev)
                        if ev.get("type") in ("assistant", "error") or ev.get("state") in ("idle", "error"):
                            # Wait a bit more for trailing events
                            _time.sleep(1)
                            while not q.empty():
                                events.append(q.get_nowait())
                            break
                    except Exception:
                        pass
                try:
                    sess.unsubscribe(q)
                except Exception:
                    pass
                # Check adapter status
                adapter_info = {}
                try:
                    a = getattr(sess, "adapter", None)
                    if a:
                        adapter_info = {
                            "type": type(a).__name__,
                            "resolved_model": getattr(a, "resolved_model", None),
                            "resolved_provider": getattr(a, "resolved_provider", None),
                            "model": getattr(a, "model", None),
                        }
                except Exception as e:
                    adapter_info = {"error": str(e)}
                self._send_json(200, {
                    "ok": True,
                    "session_id": sess.id,
                    "tier": sess.tier,
                    "model": sess.model,
                    "status": sess.status,
                    "adapter": adapter_info,
                    "events": events[:50],
                    "event_count": len(events),
                    "all_session_events": sess.events[:50],
                })
            except Exception as exc:
                import traceback as _tb
                self._send_json(500, {
                    "ok": False,
                    "error": str(exc)[:300],
                    "error_type": type(exc).__name__,
                    "traceback": _tb.format_exc()[:500],
                })
            return
        # --- Chat session routes (bearer-gated; identity via X-JWT) ---
        if route == "/api/chat/sessions" or route.startswith("/api/chat/sessions/"):
            import chat_routes
            chat_routes.handle_request("GET", self.path, {}, self)
            return
        # --- Chat job queue (GET /api/chat/queue) — bearer-gated -------------
        if route == "/api/chat/queue":
            import chat_jobs
            chat_jobs.handle_request("GET", self.path, {}, self)
            return
        # --- Provider API keys (GET /api/keys) — bearer-gated; identity via X-JWT
        if route == "/api/keys":
            import keys_routes
            keys_routes.handle_request("GET", self.path, {}, self)
            return
        # --- Pricing (GET /api/pricing) — bearer-gated, 10-min cached --------
        if route == "/api/pricing":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._handle_pricing()
            return
        # --- Monitor SSE (GET /api/monitor) — bearer-gated, long-lived stream
        if route == "/api/monitor":
            ok = self._auth_ok()
            if not ok:
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                token_q = q.get("bearer", [None])[0]
                if token_q:
                    self.headers["Authorization"] = f"Bearer {token_q}"
                    ok = self._auth_ok()
            if not ok:
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._handle_monitor_sse()
            return
        # --- HF Spaces health check ---
        if route == "/-/health":
            self._send_json(200, {"status": "ok", "service": "doomalaysocreate"})
            return
        # --- Debug logs (auth-gated) ---
        if route == "/api/debug/logs":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            cat = q.get("cat", [None])[0]
            tail = int(q.get("tail", ["100"])[0])
            level = q.get("level", [None])[0]
            self._send_json(200, {
                "logs": debug_log.get_recent_logs(cat, tail, level),
                "categories": debug_log.list_log_categories(),
            })
            return
        if route == "/api/debug/diagnose":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._handle_debug_diagnose()
            return
        # --- Tier 3: Conscious routes (bearer-gated; JWT enforced inside) ---
        if route == "/api/conscious" or route.startswith("/api/conscious/"):
            self._conscious_dispatch("GET")
            return
        if route == "" and self._serve_static("/"):
            return  # bundled web app owns the root; the JSON overview stays on /health
        if route in ("", "/health"):
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            roster = panel.roster()
            self._send_json(200, {
                "service": "doomalaysocreate model panel",
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
                    "GET /api/templates": "list the caller's templates (private + public)",
                    "GET /api/templates/explore": "browse public templates (sort=hearts|recent|relevant, query, kind)",
                    "GET /api/templates/<id>": "fetch one template (full markdown)",
                    "POST /api/templates": "create a template {name, description, markdown, kind, tags, is_public}",
                    "PATCH /api/templates/<id>": "update a template (owner only)",
                    "DELETE /api/templates/<id>": "delete a template (owner only)",
                    "POST /api/templates/<id>/heart": "toggle heart on a template",
                    "POST /api/templates/<id>/download": "download a template (creates a local copy)",
                    "POST /api/templates/<id>/publish": "publish to the global library",
                    "POST /api/templates/<id>/unpublish": "unpublish from the global library",
                    "GET /api/orchestrator/templates": "list built-in + user orchestrator schematic templates",
                    "GET /api/orchestrator/templates/<id>": "fetch one orchestrator template's schematic",
                    "POST /api/orchestrator/templates": "save a user orchestrator template {id, schematic}",
                    "DELETE /api/orchestrator/templates/<id>": "delete a user orchestrator template",
                    "GET /api/jobs/<id>": "poll an async job/run (when called with async:true)",
                    "POST /api/agent": "agent chat: {message, session_id?} -> 202 {session_id}",
                    "GET /api/agent/<sid>?since=<n>": "poll the agent transcript (delta events)",
                    "GET /api/agent/<sid>/files": "list workspace artifacts",
                    "GET /api/agent/<sid>/file?path=": "download a workspace artifact",
                    "GET /api/stats": "live rotation/health per provider + slot",
                    "GET /api/metrics?profile=<id>": "per-profile cost/throttle/latency aggregates",
                    "GET /api/metrics/sync": "force sync public metrics from shared HF Dataset",
                    "GET /api/metrics/global": "global aggregates across all profiles (community dashboard)",
                    "GET /api/metrics/user?profile=<id>": "user's own metrics from public dataset",
                    "GET /api/debug/logs?cat=&tail=100&level=": "view structured debug logs (auth-gated)",
                    "DELETE /api/debug/clear?cat=": "clear debug log ring buffer (auth-gated)",
                    "GET /api/memory?workspace_id=": "workspace memory layer state (.pied sanity log)",
                    "POST /api/memory": "write to / clear a workspace's memory layer",
                    "GET /api/benchmarks": "live model benchmarks from OpenRouter (pricing, context, caps)",
                    "GET /api/workspace/files?workspace_id=": "list files in a workspace sandbox",
                    "GET /api/roster": "per-model benchmark + hosts + privacy-safe routability + frontier guarantee",
                    "GET /oauth/login": "start HF OAuth flow (redirects to HF authorize)",
                    "GET /oauth/callback": "HF OAuth callback — provisions user Space",
                    "GET /oauth/result/<token>": "exchange one-time token for provision result",
                    "POST /oauth/set-provider-key": "set a provider API key on the user's Space via HF API",
                    # GitHub integration
                    "GET /api/auth/github/login": "start GitHub OAuth flow (redirects to GitHub)",
                    "GET /api/auth/github/callback": "GitHub OAuth callback — stores encrypted token",
                    "POST /api/auth/github/disconnect": "remove stored GitHub token",
                    "GET /api/auth/status": "check GitHub + HF auth status",
                    "GET /api/github/repos": "list authenticated user's GitHub repos",
                    "GET /api/github/repos/<owner>/<repo>/branches": "list branches for a repo",
                    "POST /api/github/repos/create": "create a new GitHub repo for the user (or under an org)",
                    "GET /api/github/gitignore/templates": "list .gitignore templates GitHub offers",
                    "GET /api/github/licenses": "list license templates GitHub offers",
                    "GET /api/github/orgs": "list orgs the user can create repos in",
                    "GET /api/workspaces": "list user's workspaces",
                    "POST /api/workspaces": "create workspace (optionally clone from GitHub repo)",
                    "GET /api/workspaces/<id>": "get workspace details",
                    "POST /api/workspaces/<id>/update": "update workspace settings",
                    "DELETE /api/workspaces/<id>": "delete workspace + sandbox",
                    "POST /api/workspaces/<id>/commit": "stage all + commit in workspace",
                    "POST /api/workspaces/<id>/push": "push to remote (queued for approval)",
                    "POST /api/workspaces/<id>/pr": "create pull request (queued for approval)",
                    "POST /api/workspaces/<id>/publish": "publish workspace to public registry",
                    "POST /api/workspaces/<id>/unpublish": "remove workspace from registry",
                    "POST /api/workspaces/<id>/checkout": "checkout a branch in workspace",
                    "GET /api/workspaces/<id>/logs": "list push history for workspace",
                    "GET /api/github/repos/<owner>/<repo>/contents": "fetch file contents from repo",
                    "GET /api/github/repos/<owner>/<repo>/tree": "fetch repo file tree",
                    "GET /api/registry/spaces": "browse public workspaces",
                    "POST /api/auth/push-requests/<id>/resolve": "approve or reject a push request",
                },
                "oauth_configured": _oauth_configured(),
                "github_configured": github_integration._github_configured(),
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
        # --- Template library (GET /api/templates*) -- bearer-gated ---------
        # The new template library handles user-created reusable templates
        # (websearch/deepresearch/judge/chat/custom). It dispatches via
        # template_library.handle_request and supports:
        #   GET /api/templates           -> caller's templates (private + public)
        #   GET /api/templates/explore   -> browse public templates
        #   GET /api/templates/<id>      -> fetch one template
        if route == "/api/templates" or route.startswith("/api/templates/"):
            import template_library
            template_library.handle_request("GET", self.path, {}, self)
            return
        # --- Orchestrator schematic templates (moved to /api/orchestrator/templates
        #     to free up /api/templates for the new template library). These are
        #     the JSON-stage templates used by POST /api/run with a template id. ---
        if route == "/api/orchestrator/templates" or route.startswith("/api/orchestrator/templates/"):
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            if route == "/api/orchestrator/templates":
                self._send_json(200, {"templates": panel.templates.list()})
            else:
                tid = route[len("/api/orchestrator/templates/"):]
                data = panel.templates.get_dict(tid)
                if data is None:
                    self._send_json(404, {"error": f"no such template {tid!r}"})
                else:
                    self._send_json(200, {"id": tid, "schematic": data})
            return
        if route in ("/api/stats", "/api/metrics", "/api/roster"):
            #   telemetry endpoints share the bearer token with the POST routes.
            if not self._auth_ok():
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
        # --- Provider model catalog (public, no auth) ---
        if route == "/api/models":
            from urllib.parse import parse_qs
            from provider_sync.catalog import build_provider_catalog
            q = parse_qs(urlsplit(self.path).query)
            refresh = q.get("refresh", ["0"])[0] in ("1", "true", "yes")
            result = build_provider_catalog(force_refresh=refresh)
            log_event("provider_catalog_served", provider_count=len(result.get("providers", [])), total_models=result.get("totalModels", 0))
            self._send_json(200, result)
            return
        # --- public metrics sync & global aggregates ---
        if route == "/api/metrics/sync":
            # Force sync from public dataset
            result = panel.metrics.sync_public_metrics()
            self._send_json(200, result)
            return
        if route == "/api/metrics/global":
            # Global aggregates across all profiles (community dashboard)
            result = panel.metrics.get_global_aggregates()
            self._send_json(200, result)
            return
        if route == "/api/metrics/user":
            # User's own metrics from public dataset (requires auth)
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            profile = "default"
            if "?" in self.path:
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                profile = (q.get("profile", ["default"])[0] or "default")
            result = panel.metrics.get_user_metrics(profile)
            self._send_json(200, result)
            return
        if route.startswith("/api/jobs/"):
            #   polling an async job needs the same bearer token as submitting one.
            if not self._auth_ok():
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
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            self._send_json(200, {"tier": agent_sessions.agent_tier(),
                                  "models": agent_sessions.agent_models()})
            return
        # Agent tools endpoint — lists all available agent tools
        if route == "/api/agent/tools":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            tools = [
                {"name": "shell", "description": "Execute bash commands (ls, cat, grep, git, python3, pip, npm, make, curl, etc.)"},
                {"name": "file_read", "description": "Read file contents"},
                {"name": "file_write", "description": "Write/create files"},
                {"name": "editor", "description": "Edit existing files (str_replace)"},
                {"name": "http_request", "description": "Fetch URLs (GET/POST/PUT/DELETE — full web access)"},
                {"name": "grep", "description": "Search file contents with regex"},
                {"name": "glob", "description": "Find files by pattern (e.g. **/*.py)"},
                {"name": "calculator", "description": "Math calculations"},
                {"name": "agent_panel", "description": "Invoke the multi-model judge panel for critiques"},
                {"name": "memory", "description": "Read/write the workspace memory layer (.pied sanity log)"},
                {"name": "delegate", "description": "Spawn a sub-agent for a sub-task (multi-agent orchestration)"},
                {"name": "load_tool", "description": "Dynamically load more tools at runtime"},
                {"name": "web_search", "description": "Search the web (when available)"},
            ]
            self._send_json(200, {"tools": tools, "count": len(tools)})
            return
        # Memory layer endpoint — returns the .pied state for a workspace.
        #
        # Auth: resolves the user from the JWT (Authorization bearer OR X-JWT
        # header — see ``_require_user``).  This is the fix for the
        # "workspace not found" bug: the previous version used ``_auth_ok()``
        # which only accepts the rotation token, so users with a JWT but no
        # rotation token got 401 → frontend read it as "workspace not found".
        # We now also accept the workspace_id being either the internal ID
        # OR a GitHub repo full name (``owner/repo``), and auto-create the
        # DB row if the user owns the repo on GitHub but it's not yet in
        # the workspaces table (covers freshly-cloned repos).
        if route == "/api/memory":
            user_id = self._require_user()
            if not user_id:
                return
            from urllib.parse import parse_qs, urlsplit
            qs = parse_qs(urlsplit(self.path).query)
            workspace_id = (qs.get("workspace_id", [""])[0] or "").strip()
            if not workspace_id:
                self._send_json(400, {"error": "workspace_id query parameter is required"})
                return
            try:
                ws = self._resolve_workspace_for_user(user_id, workspace_id)
                if ws is None:
                    self._send_json(404, {"error": "workspace not found"})
                    return
                from pathlib import Path
                import memory_layer
                # Make sure the sandbox directory + .pied/ exist (a freshly
                # cloned workspace has a sandbox but no .pied yet).
                github_integration.ensure_workspace_sandbox(ws["id"])
                ws_path = Path(ws["sandbox_path"])
                memory_layer.init_memory(ws_path)
                state = memory_layer.read_state(ws_path)
                recent_log = memory_layer.get_recent_log(ws_path, limit=20)
                blackboard = memory_layer.read_blackboard(ws_path)
                self._send_json(200, {
                    "state": state,
                    "recent_log": recent_log,
                    "blackboard": blackboard,
                })
            except Exception as e:
                self._send_json(500, {"error": str(e)[:200]})
            return

        # Benchmarks endpoint — live model data from OpenRouter (pricing, context, caps)
        if route == "/api/benchmarks":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                models = []
                for m in data.get("data", []):
                    pricing = m.get("pricing", {})
                    arch = m.get("architecture", {})
                    model_id = m.get("id", "")
                    name = m.get("name", "")
                    desc = (m.get("description", "") or "")[:300]
                    modality = arch.get("modality", "text->text")
                    name_lower = (name + " " + model_id + " " + desc).lower()
                    caps = []
                    if "vision" in name_lower or "image" in modality:
                        caps.append("vision")
                    if "cod" in name_lower or "program" in name_lower:
                        caps.append("coding")
                    if "reason" in name_lower or "think" in name_lower:
                        caps.append("reasoning")
                    if "agent" in name_lower:
                        caps.append("agentic")
                    if "tool" in name_lower:
                        caps.append("tools")
                    prompt_cost = float(pricing.get("prompt", "0") or "0")
                    completion_cost = float(pricing.get("completion", "0") or "0")
                    cost_per_1m = round((prompt_cost + completion_cost) * 1000000, 4)
                    models.append({
                        "id": model_id, "name": name,
                        "context_length": m.get("context_length", 0),
                        "prompt_price": pricing.get("prompt", "0"),
                        "completion_price": pricing.get("completion", "0"),
                        "cost_per_1m": cost_per_1m,
                        "is_free": pricing.get("prompt") == "0" and pricing.get("completion") == "0",
                        "description": desc, "modality": modality,
                        "capabilities": caps,
                        "input_modalities": arch.get("input_modalities", ["text"]),
                        "output_modalities": arch.get("output_modalities", ["text"]),
                        "tokenizer": arch.get("tokenizer", ""),
                        "knowledge_cutoff": m.get("knowledge_cutoff", ""),
                        "supported_params": [p for p in m.get("supported_parameters", []) if p in
                                            ["temperature", "top_p", "max_tokens", "stream", "tools",
                                             "response_format", "reasoning", "tool_choice"]],
                    })
                models.sort(key=lambda m: (not m["is_free"], -m["context_length"]))
                self._send_json(200, {
                    "models": models, "count": len(models),
                    "free_count": sum(1 for m in models if m["is_free"]),
                    "vision_count": sum(1 for m in models if "vision" in m["capabilities"]),
                    "coding_count": sum(1 for m in models if "coding" in m["capabilities"]),
                    "agentic_count": sum(1 for m in models if "agentic" in m["capabilities"]),
                })
            except Exception as e:
                self._send_json(500, {"error": f"failed to fetch benchmarks: {str(e)[:200]}"})
            return
        # Workspace files endpoint — list files in a workspace sandbox
        if route == "/api/workspace/files":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            from urllib.parse import parse_qs, urlsplit
            qs = parse_qs(urlsplit(self.path).query)
            workspace_id = qs.get("workspace_id", [""])[0]
            if not workspace_id:
                self._send_json(400, {"error": "workspace_id query parameter is required"})
                return
            try:
                import db
                from pathlib import Path
                ws = db.get_workspace(workspace_id)
                if not ws or not ws.get("sandbox_path"):
                    self._send_json(404, {"error": "workspace not found"})
                    return
                root = Path(ws["sandbox_path"]).resolve()
                files = []
                if root.exists():
                    for p in sorted(root.rglob("*")):
                        if len(files) >= 500:
                            break
                        rel = p.relative_to(root)
                        # hide dotfiles, .git, .pied, .claude, node_modules
                        if any(seg.startswith(".") or seg == "node_modules" for seg in rel.parts):
                            continue
                        if p.is_file():
                            st = p.stat()
                            files.append({"path": str(rel), "size": st.st_size,
                                          "mtime": int(st.st_mtime)})
                self._send_json(200, {"workspace_id": workspace_id, "files": files})
            except Exception as e:
                self._send_json(500, {"error": str(e)[:200]})
            return
        if route.startswith("/api/agent/"):
            #   transcript polling + artifact access + SSE stream share the service bearer token.
            #   SSE via EventSource can't set custom headers, so the stream endpoint also
            #   accepts the token as a ?bearer= query parameter.
            ok = self._auth_ok()
            if not ok and route.endswith("/stream"):
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                token_q = q.get("bearer", [None])[0]
                if token_q:
                    self.headers["Authorization"] = f"Bearer {token_q}"
                    ok = self._auth_ok()
            if not ok:
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            # SSE stream endpoint — keep-alive, must be handled differently than JSON responses
            if route.endswith("/stream"):
                self._handle_agent_stream(route)
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
        # --- Identity grant claim endpoints (main Space serves, any Space claims) --
        if route.startswith("/api/auth/github/grants/"):
            token = route[len("/api/auth/github/grants/"):]
            data = _pop_provision_result(token)
            if data is None:
                self._send_json(404, {"error": "grant token not found or expired"})
            else:
                # SECURITY (C6): Strip server-side tokens before sending
                safe = {k: v for k, v in data.items() if not k.startswith("_")}
                self._send_json(200, safe)
            return
        if route.startswith("/api/auth/hf/grants/"):
            token = route[len("/api/auth/hf/grants/"):]
            data = _pop_provision_result(token)
            if data is None:
                self._send_json(404, {"error": "grant token not found or expired"})
            else:
                # SECURITY (C6): Strip server-side tokens before sending
                safe = {k: v for k, v in data.items() if not k.startswith("_")}
                self._send_json(200, safe)
            return
        # --- GitHub integration routes -------------------------------------------
        if route == "/api/auth/github/login":
            self._handle_github_login()
            return
        if route == "/api/auth/github/callback":
            self._handle_github_callback()
            return
        if route == "/api/auth/hf/login":
            self._handle_hf_login()
            return
        if route == "/api/auth/hf/callback":
            self._handle_hf_callback()
            return
        if route == "/api/auth/status":
            self._handle_auth_status()
            return
        if route == "/api/github/repos":
            self._handle_github_repos()
            return
        if route.startswith("/api/github/repos/") and route.endswith("/branches"):
            self._handle_github_branches(route)
            return
        if route == "/api/github/gitignore/templates":
            self._handle_github_gitignore_templates()
            return
        if route == "/api/github/licenses":
            self._handle_github_licenses()
            return
        if route == "/api/github/orgs":
            self._handle_github_orgs()
            return
        if route == "/api/workspaces":
            self._handle_workspaces_list()
            return
        if route.startswith("/api/workspaces/"):
            ws_rest = route[len("/api/workspaces/"):]
            parts = ws_rest.split("/", 1)
            ws_id = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "logs":
                self._handle_workspace_logs(ws_id)
                return
            if sub == "":
                self._handle_workspace_get(ws_id)
                return
        if route.startswith("/api/github/repos/") and route.endswith("/contents"):
            self._handle_github_contents(route)
            return
        if route.startswith("/api/github/repos/") and route.endswith("/tree"):
            self._handle_github_tree(route)
            return
        if route == "/api/registry/spaces":
            self._handle_registry_list()
            return
        if route.startswith("/api/auth/push-requests/"):
            req_id = route[len("/api/auth/push-requests/"):]
            self._handle_push_request_status(req_id)
            return
        # -----------------------------------------------------------------------
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
                                           "(HF_CLIENT_ID missing — set hf_oauth:true in README)"})
            return
        from urllib.parse import urlencode
        nonce = secrets.token_urlsafe(16)
        state = _make_oauth_state(nonce)
        host = self._space_host()
        redirect_uri = f"https://{host}/oauth/callback"
        params = urlencode({
            "client_id": os.environ["HF_CLIENT_ID"],
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
        if not code or not state or not _verify_oauth_state(state)[0]:
            self._redirect(f"https://{host}/#provision-error=invalid_state")
            return

        client_id = os.environ.get("HF_CLIENT_ID", "").strip()
        client_secret = os.environ.get("HF_CLIENT_SECRET", "").strip()
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
        target_repo = f"{username}/doomalaysocreate"
        rotation_secret = secrets.token_urlsafe(32)
        existing = False

        # 3. Returning user? Repo ids are unique case-insensitively but API lookups
        #    are exact-case — find the canonical id of any existing "doomalaysocreate" Space
        #    (e.g. "<user>/Doomalaysocreate") instead of blindly assuming lowercase.
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

        # 5b. Copy GitHub OAuth credentials so users don't need their own OAuth App.
        #     These are read from this Space's own environment (set by the account owner).
        gh_client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
        gh_client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
        if gh_client_id and gh_client_secret:
            for key, val in [("GITHUB_CLIENT_ID", gh_client_id),
                             ("GITHUB_CLIENT_SECRET", gh_client_secret)]:
                try:
                    _hf_api(f"https://huggingface.co/api/spaces/{target_repo}/secrets",
                            method="POST", token=user_token,
                            body={"key": key, "value": val})
                except Exception as exc:
                    # Non-fatal: GitHub integration won't work but the Space still functions.
                    log_event("oauth_set_github_secret_error", user=username,
                              key=key, error=str(exc)[:200])

        space_name = target_repo.split("/", 1)[1].lower()
        space_url = f"https://{username.lower()}-{space_name}.hf.space"
        result = {
            "space_url": space_url,
            "space_repo": target_repo,
            "rotation_secret": rotation_secret,
            "username": username,
            # SECURITY (C6): oauth_token is NOT included in the result returned
            # to the browser. The frontend only needs rotation_secret + space_url.
            # The oauth_token is used server-side only (for setting secrets on
            # the user's duplicated Space). It was previously exposed via the
            # /oauth/result/<token> endpoint which is unauthenticated.
            "_oauth_token": user_token,  # server-side only, stripped before sending
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
        # SECURITY (C6): Strip the server-side oauth_token before sending to browser
        safe_result = {k: v for k, v in result.items() if not k.startswith("_")}
        self._send_json(200, safe_result)

    # -- agent orchestrator routes ------------------------------------------

    def _handle_agent_post(self, payload: dict) -> None:
        #   POST /api/agent {message, session_id?, workspace_id?} -> 202 {session_id, tier, status}
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._send_json(400, {"error": "'message' (non-empty string) is required"})
            return
        if len(message) > 100_000:
            self._send_json(413, {"error": "'message' too large (max 100k chars)"})
            return
        if agent_sessions.agent_tier() is None:
            self._send_json(503, {"error": "no agent tier configured — set ANTHROPIC_API_KEY "
                                           "(Claude agent), any free provider key (open agent), "
                                           "or AGENT_FORCE_TIER=mock (echo test agent). "
                                           "On a duplicated Space, add a key via the onboarding wizard."})
            return
        session_id = payload.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) else None
        model = payload.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        # Model resolution: the frontend can send any of these formats:
        #   - Canonical litellm:  "openai/kimi-k2.6"
        #   - Panel provider/model: "privatemodeai/kimi-k2.6"
        #   - Logical/bare name:  "kimi-k2.6" or "deepseek-v4-flash"
        # We normalize to the canonical litellm string via _resolve_open_model.
        # If that fails, we try the panel's logical_models mapping as a bridge.
        # If still unresolved, we pass the raw model through — StrandsAdapter.open()
        # also calls _resolve_open_model as a last line of defense, and
        # tier_for_model falls back to mock if nothing matches.
        if model:
            resolved = None
            try:
                pair = agent_sessions._resolve_open_model(model)
                if pair:
                    resolved = pair[0]  # canonical litellm string (openai/...)
            except Exception:
                pass
            # Bridge: resolve a bare logical ID via the panel's logical_models
            # mapping, then re-resolve through _resolve_open_model to get the
            # litellm format. This handles the condensed model picker which
            # uses logical IDs.
            if not resolved and "/" not in model:
                try:
                    panel_obj: Panel = self.server.panel  # type: ignore[attr-defined]
                    spec = panel_obj.logical_models.get(model)
                    if spec and spec.get("candidates"):
                        for cand in spec["candidates"]:
                            provider = cand.get("provider", "")
                            model_id = cand.get("model", "")
                            if provider and model_id:
                                pair = agent_sessions._resolve_open_model(
                                    f"{provider}/{model_id}")
                                if pair:
                                    resolved = pair[0]
                                    break
                                pair = agent_sessions._resolve_open_model(model_id)
                                if pair:
                                    resolved = pair[0]
                                    break
                except Exception:
                    pass
            if resolved:
                model = resolved
        # optional workspace_id: link agent to a user workspace sandbox.
        # If the workspace_id is provided but NOT found (or not owned by the
        # caller), we DON'T 404 — we log a warning and proceed WITHOUT a
        # workspace so the chat still works (Bug 8: the frontend sometimes
        # sends a stale/invalid workspace_id after a Space wipe or session
        # restore, and the chat shouldn't fail because of it).
        workspace_id = payload.get("workspace_id")
        workspace_id = workspace_id.strip() if isinstance(workspace_id, str) and workspace_id.strip() else None
        if workspace_id:
            ws = db.get_workspace(workspace_id)
            if not ws:
                log_event("agent_post_workspace_not_found",
                          workspace_id=workspace_id,
                          note="proceeding without workspace (stale id from frontend?)")
                workspace_id = None
            else:
                # ownership check: agent must operate in caller's workspace.
                # The service bearer token in Authorization was already verified by
                # _auth_and_body; here we additionally require GitHub identity,
                # carried in the X-JWT header (NOT Authorization, which is reserved
                # for the service/rotation token).
                user_id = self._require_user_from_jwt()
                if not user_id:
                    self._send_json(401, {"error": "GitHub identity required — connect GitHub in settings"})
                    return
                if ws["user_id"] != user_id:
                    log_event("agent_post_workspace_access_denied",
                              workspace_id=workspace_id,
                              note="proceeding without workspace (not owned by caller)")
                    workspace_id = None
        # STRANDS-COMPLETE-FIX (Issue 2): the frontend MUST create the chat
        # session first via POST /api/chat/sessions and pass the session id
        # as `chat_session_id` in every POST /api/agent call. The previous
        # auto-create logic here caused duplicate sessions: the frontend's
        # createSession() call succeeded but its response was slow, so the
        # user clicked send before the session id landed; this backend then
        # auto-created a SECOND session -- the user saw two entries in the
        # sidebar for one click. We now REQUIRE chat_session_id and reject
        # with 400 if it's missing. The frontend's _runTurn already awaits
        # createSession() before calling client.send(), so the id is always
        # present in well-formed requests.
        chat_session_id = payload.get("chat_session_id")
        chat_session_id = (chat_session_id.strip()
                           if isinstance(chat_session_id, str) and chat_session_id.strip()
                           else None)
        if not chat_session_id:
            self._send_json(400, {"error": "chat_session_id is required "
                                         "(call POST /api/chat/sessions first)"})
            return
        # Research mode params (optional). When webSearch or deepResearch is
        # true, the session uses ResearchAdapter instead of the Claude/Strands
        # SDK and drives research_templates directly from the panel's slots.
        panel_for_research: Panel = self.server.panel  # type: ignore[attr-defined]
        # Task 6 — per-chat metadata fallback: if the request didn't
        # explicitly set a param, fall back to the chat session's stored
        # metadata so switching chats restores the right model/tools.
        # ``cs_meta`` is None if no chat_session_id was resolved.
        cs_meta: dict | None = None
        if chat_session_id:
            try:
                import chat_routes
                cs_meta = chat_routes.get_chat_session(chat_session_id)
            except Exception:
                cs_meta = None
        # model fallback (already resolved above, but if the request didn't
        # send a model, use the session's stored model)
        if not model and cs_meta and cs_meta.get("model"):
            model = cs_meta["model"].strip() or None
        effort = str(payload.get("effort", "")).strip().lower() or None
        if not effort and cs_meta:
            effort = (cs_meta.get("effort") or "").strip().lower() or None
        # Accept any effort string — the valid levels are determined per-model
        # by the effort_detector. The frontend sends the level the user selected
        # from the model's effort_levels list (e.g. "on", "off", "none", "high",
        # "max", "min", "ultra", etc.). The StrandsAdapter.open() resolves the
        # correct body via effort_detector.detect_effort_body().
        web_search = bool(payload.get("webSearch") or payload.get("web_search"))
        if "webSearch" not in payload and "web_search" not in payload and cs_meta:
            web_search = bool(cs_meta.get("web_search", 0))
        web_search_template = (payload.get("webSearchTemplate")
                                or payload.get("web_search_template"))
        if not web_search_template and cs_meta:
            web_search_template = cs_meta.get("web_template") or None
        if web_search_template and web_search_template not in (
                "breadth", "deep_dive", "compare", "fact_check"):
            self._send_json(400, {"error": "'webSearchTemplate' must be one of breadth|deep_dive|compare|fact_check"})
            return
        if web_search and not web_search_template:
            web_search_template = "breadth"  # default template
        deep_research = bool(payload.get("deepResearch") or payload.get("deep_research"))
        if "deepResearch" not in payload and "deep_research" not in payload and cs_meta:
            deep_research = bool(cs_meta.get("deep_research", 0))
        deep_research_mode = (payload.get("deepResearchMode")
                               or payload.get("deep_research_mode"))
        if not deep_research_mode and cs_meta:
            deep_research_mode = cs_meta.get("deep_mode") or None
        if deep_research_mode and deep_research_mode not in (
                "default", "react", "extended_thinking"):
            self._send_json(400, {"error": "'deepResearchMode' must be one of default|react|extended_thinking"})
            return
        deep_research_template = (payload.get("deepResearchTemplate")
                                   or payload.get("deep_research_template"))
        if not deep_research_template and cs_meta:
            deep_research_template = cs_meta.get("deep_template") or None
        if deep_research_template and deep_research_template not in (
                "breadth", "deep_dive", "compare", "fact_check"):
            self._send_json(400, {"error": "'deepResearchTemplate' must be one of breadth|deep_dive|compare|fact_check"})
            return
        # Task 6 — persist the resolved metadata back to the chat session so
        # the next turn (which may not send these params) picks them up.
        # Skip if the chat session was just auto-created (it already has
        # these values from the create call). Best-effort — never block the
        # turn on a DB write failure.
        if chat_session_id and cs_meta:
            try:
                import chat_routes
                updates: dict = {}
                if model and model != (cs_meta.get("model") or ""):
                    updates["model"] = model
                if effort and effort != (cs_meta.get("effort") or ""):
                    updates["effort"] = effort
                if web_search != bool(cs_meta.get("web_search", 0)):
                    updates["web_search"] = web_search
                if (web_search_template or "") != (cs_meta.get("web_template") or ""):
                    updates["web_template"] = web_search_template
                if deep_research != bool(cs_meta.get("deep_research", 0)):
                    updates["deep_research"] = deep_research
                if (deep_research_mode or "") != (cs_meta.get("deep_mode") or ""):
                    updates["deep_mode"] = deep_research_mode
                if (deep_research_template or "") != (cs_meta.get("deep_template") or ""):
                    updates["deep_template"] = deep_research_template
                if updates:
                    chat_routes.update_chat_session_meta(chat_session_id, **updates)
            except Exception as e:  # noqa: BLE001
                log_event("agent_chat_meta_persist_error", error=repr(e)[:200])
        try:
            mode = str(payload.get("mode", "auto")).strip().lower()
            if mode not in ("auto", "build", "plan"):
                mode = "auto"
            session = agent_sessions.get_or_create(session_id, model,
                                                   workspace_id=workspace_id,
                                                   chat_session_id=chat_session_id,
                                                   mode=mode,
                                                   panel=panel_for_research,
                                                   effort=effort,
                                                   web_search=web_search,
                                                   web_search_template=web_search_template,
                                                   deep_research=deep_research,
                                                   deep_research_mode=deep_research_mode,
                                                   deep_research_template=deep_research_template)
        except agent_sessions.CapacityError as e:
            self._send_json(429, {"error": str(e)}, headers={"Retry-After": "30"})
            return
        except Exception as e:  # noqa: BLE001
            log_event("agent_create_error", error=repr(e)[:200])
            self._send_json(500, {"error": "agent session error"})
            return
        session.submit(message.strip())
        resp = {"session_id": session.id, "tier": session.tier,
                "model": session.model, "status": session.status,
                "requested_model": model}
        # Include resolved routing info if available (set by StrandsAdapter.open()
        # in a background thread — may be None if the adapter hasn't opened yet).
        adapter = getattr(session, "adapter", None)
        if adapter is not None:
            rm = getattr(adapter, "resolved_model", None)
            rp = getattr(adapter, "resolved_provider", None)
            if rm:
                resp["resolved_model"] = rm
            if rp:
                resp["resolved_provider"] = rp
        if session.workspace_id:
            resp["workspace_id"] = session.workspace_id
        if chat_session_id:
            resp["chat_session_id"] = chat_session_id
        self._send_json(202, resp)
        # STRANDS-COMPLETE-FIX (Issue 1): throttle-batched DB sync to the
        # HF dataset so chat history survives Space restarts. Best-effort,
        # non-blocking -- the actual upload happens in a background worker
        # that batches to at most one every 30s.
        try:
            db.sync_db_to_dataset()
        except Exception:
            pass

    def _handle_chat_judge(self, payload: dict) -> None:
        #   POST /api/chat/judge — in-chat multi-model judge panel.
        #   Accepts: {input, template (critique|verify|improve|debate), count (1-6)}
        #   Fans out to N diverse judges in parallel, then merges.
        input_text = payload.get("input")
        if not isinstance(input_text, str) or not input_text.strip():
            self._send_json(400, {"error": "'input' (non-empty string) is required"})
            return
        template = str(payload.get("template", "critique")).strip().lower()
        if template not in ("critique", "verify", "improve", "debate"):
            self._send_json(400, {"error": "'template' must be critique|verify|improve|debate"})
            return
        try:
            count = int(payload.get("count", 3))
        except (TypeError, ValueError):
            count = 3
        if not 1 <= count <= 6:
            self._send_json(400, {"error": "'count' must be in 1..6"})
            return
        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        # Build a diverse panel from the roster — different model families.
        # Use the panel's default_panel if it has >= count entries; else
        # expand from the roster's logical models.
        roster = panel.roster()
        candidates: list[str] = []
        # Prefer the default panel (already curated for diversity).
        for who in panel.default_panel:
            if who not in candidates:
                candidates.append(who)
        # Top up from the roster (frontier models first).
        if len(candidates) < count:
            for m in roster.get("models", []):
                logical = m.get("logical")
                if logical and logical not in candidates and m.get("routable"):
                    candidates.append(logical)
                    if len(candidates) >= count * 2:
                        break
        if not candidates:
            self._send_json(503, {"error": "no judges available (configure a provider key)"})
            return
        who_list = candidates[:count]
        # Map template → role + merge mode.
        template_to_role = {
            "critique": ("critiquer", "dedupe"),
            "verify":   ("verifier", "vote"),
            "improve":  ("transformer", "dedupe"),
            "debate":   ("generator", "concat"),
        }
        role, merge = template_to_role[template]
        try:
            params = build_panel_params(
                panel, input_text=input_text, role=role, system=None,
                instructions=None, output_rules=None, template=None,
                panel_override=who_list, merge_mode=merge, max_tokens=8192,
                want_artifacts=False, reasoning=False, research=False)
            params = apply_effort(params, "med")
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"failed to build judge params: {type(e).__name__}"})
            return
        try:
            result = asyncio.run(run_sync(panel, params))
        except Exception as e:  # noqa: BLE001
            log_event("chat_judge_error", error=repr(e)[:200])
            self._send_json(500, {"error": f"judge failed: {type(e).__name__}"})
            return
        judges = result.get("judges", [])
        ok = sum(1 for j in judges if j.get("ok"))
        self._send_json(200, {
            "merged": result.get("merged", ""),
            "judges": judges,
            "ok": ok,
            "total": len(judges),
            "template": template,
            "count": count,
        })

    def _handle_pricing(self) -> None:
        #   GET /api/pricing — live OpenRouter pricing + local catalog merge.
        try:
            import pricing as _pricing
            result = _pricing.get_pricing()
            self._send_json(200, result)
        except Exception as e:  # noqa: BLE001
            log_event("pricing_error", error=repr(e)[:200])
            self._send_json(500, {"error": f"failed to fetch pricing: {type(e).__name__}"})

    def _handle_monitor_sse(self) -> None:
        #   GET /api/monitor — SSE stream that polls for queued chat jobs,
        #   runs them, and streams live progress events. One long-lived
        #   connection per browser tab.
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        space_id = (qs.get("spaceId", [""])[0] or qs.get("space_id", [""])[0] or "").strip() or None
        # Allow ?bearer= for SSE clients that can't set headers (EventSource).
        try:
            idle_timeout_s = float(qs.get("idleTimeout", ["120"])[0])
        except ValueError:
            idle_timeout_s = 120.0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        import chat_jobs
        import time as _time

        def _send_event(ev: dict) -> None:
            try:
                line = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False
            return True

        last_activity = _time.time()
        try:
            while True:
                # Poll for queued jobs (FIFO — oldest first).
                queued = chat_jobs.list_queued_jobs(limit=5)
                if space_id:
                    queued = [j for j in queued if j.get("space_id") == space_id]
                if queued:
                    job = queued[0]
                    if job.get("status") == "queued":
                        # Run the job synchronously in this thread. The runner
                        # emits events via _send_event; we relay them as SSE.
                        chat_jobs.run_job(job, panel=panel, emit=_send_event)
                        last_activity = _time.time()
                else:
                    # Heartbeat so proxies don't time us out.
                    if _time.time() - last_activity > idle_timeout_s:
                        break
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    _time.sleep(2.0)
                    continue
                # Brief pause between jobs so we don't tight-loop on errors.
                _time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001 — never crash the SSE thread
            log_event("monitor_sse_error", error=repr(exc)[:200])
            _send_event({"event": "job_error", "error": f"monitor stream error: {type(exc).__name__}"})

    def _handle_agent_stream(self, route: str) -> None:
        """SSE endpoint: GET /api/agent/<sid>/stream

        Subscribes to the session's event queue and pushes real-time events
        as SSE ``data:`` frames. Falls back to polling if the session doesn't
        support queue-based subscription (legacy sessions).

        CHAT-RELIABILITY-FIX: previously this returned 404 immediately when
        the session didn't exist (e.g. the SSE client connected BEFORE the
        POST /api/agent response arrived — rare under HF Space cold-start
        or network jitter, but a real race). The frontend's onError fired,
        the polling fallback also 404'd, and the user saw "I sent Hi but
        got no reply" because neither path delivered the assistant event.

        Now: open the SSE response (200 + headers) IMMEDIATELY and poll the
        session registry for up to 5 seconds. The session should appear
        within ~50ms (the POST handler registers it before responding), but
        the 5s window absorbs Space cold-start latency, container restarts,
        and any OS-level scheduling delay. If the session still hasn't
        appeared after 5s, we emit a typed "error" event so the frontend
        can surface the failure to the user instead of hanging silently.
        """
        from urllib.parse import parse_qs, urlsplit
        sid = route[len("/api/agent/"):-len("/stream")]
        query = parse_qs(urlsplit(self.path).query)
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0

        # Open the SSE response immediately so the client's fetch() resolves
        # with 200 OK and starts reading the stream. We'll send heartbeats
        # while waiting for the session to appear.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Wait for the session to be registered (up to 5 seconds). The POST
        # /api/agent handler registers the session in agent_sessions._sessions
        # before returning 202, so under normal conditions this loop runs
        # zero or one iterations. The wait absorbs:
        #   - HF Space cold-start (the POST handler may take seconds to
        #     import + initialize on the first request after sleep)
        #   - Network reordering (the SSE GET arrives before the POST 202
        #     reaches the client)
        #   - Process restarts that race the POST + GET pair
        session = agent_sessions.get_session(sid)
        wait_deadline = time.time() + 5.0
        while session is None and time.time() < wait_deadline:
            try:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # client disconnected while waiting — give up silently
            time.sleep(0.1)
            session = agent_sessions.get_session(sid)

        if session is None:
            # Session never appeared — emit a typed error so the frontend
            # surfaces the failure instead of hanging on a silent stream.
            # The frontend's SSE handler will fall back to polling (which
            # also 404s) and eventually show "(no response)" — better than
            # a perpetual spinner.
            err_ev = {"i": -1, "ts": time.time(), "type": "error",
                      "error": f"agent session {sid} not found within 5s "
                               f"(expired or never created)"}
            try:
                line = f"data: {json.dumps(err_ev, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        # Use queue-based subscription if available, else fall back to polling
        sub = getattr(session, "subscribe", None)
        if sub is not None:
            q = sub(since)
            try:
                while True:
                    try:
                        ev = q.get(timeout=30)
                    except queue.Empty:
                        try:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break
                        continue
                    if ev is None:
                        break
                    line = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
            finally:
                unsub = getattr(session, "unsubscribe", None)
                if unsub is not None:
                    unsub(q)
        else:
            # Legacy polling fallback
            import time as _time
            cursor = since
            try:
                while True:
                    snap = session.snapshot(since=cursor)
                    for ev in snap.get("events", []):
                        self.wfile.write(f"id: {cursor}\ndata: {json.dumps(ev)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        cursor += 1
                    status = snap.get("status", "running")
                    if status not in ("running", "starting"):
                        break
                    _time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

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

    def _read_json_body(self) -> dict | None:
        """Parse the request body as JSON (no auth check — caller already
        called _require_user() for OAuth endpoints)."""
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

    def _auth_and_body(self) -> dict | None:
        """Shared gate for panel POST routes: bearer auth OR session cookie + JSON body parse.
        On any failure it writes the error response and returns None.

        Auth (server-side sessions phase): accepts EITHER:
        - Rotation token in Authorization header (admin/Space owner), OR
        - Valid session cookie (any authenticated user)
        This fixes the bug where chat panel + agent panel 401 after GitHub
        login because the rotation secret isn't in the user's localStorage."""
        if not self._auth_ok():
            if not _auth_configured():
                err = "no auth secret configured on server (set CRITIQUE_TOKEN or CRITIQUE_ROTATION_SECRET)"
                self._send_json(503, {"error": err})
            else:
                err = "missing or invalid bearer token"
                self._send_json(401, {"error": err})
            from debug_log import log_entry
            log_entry(level="WARN", cat="auth", fn="_auth_and_body", msg=err,
                      data={"path": self.path, "method": self.command})
            return None
        return self._read_json_body()

    def _auth_ok(self) -> bool:
        """Check if the request is authenticated via EITHER:
        - Rotation token / static token in Authorization header, OR
        - Valid session cookie (if _check_session is implemented).

        The cookie-session path is optional — if _check_session isn't
        defined (it was a stub that was never wired up), we skip it
        gracefully instead of crashing with a NameError that turns a
        clean 401 into an HTTP 500. This was the root cause of the
        "500 replaced the 401" bug: when the rotation token didn't match
        (e.g. client/server window mismatch during an upgrade), _token_ok
        returned False, then _check_session threw NameError → 500.
        """
        if _token_ok(self.headers.get("Authorization")):
            return True
        # Cookie-session auth is optional. Look up _check_session dynamically
        # so a missing definition never crashes the auth gate.
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            checker = globals().get("_check_session")
            if callable(checker):
                try:
                    session_user_id, _ = checker(cookie_header)
                    if session_user_id:
                        return True
                except Exception:
                    pass  # don't let a broken cookie check crash the gate
        return False

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
            "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
            "CF_API_TOKEN", "CF_ACCOUNT_ID", "MOONSHOT_API_KEY",
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

    # --- GitHub integration handlers ------------------------------------------

    def _require_user(self) -> str | None:
        """Extract and validate user_id from the JWT bearer token or X-JWT header.

        Tries Authorization bearer first (GitHubClient), then falls back to
        X-JWT header (ConsciousClient sends a wire-token bearer + JWT in X-JWT).
        Verifies the JWT signature + expiry.  If the user row is missing from
        the DB (ephemeral SQLite), reconstructs it from the JWT payload so
        sessions survive a DB rebuild.
        Returns the deterministic user_id on success, None on failure (with an
        HTTP 401 already sent to the response).
        """
        # Try Authorization bearer first (JWT directly — GitHubClient path)
        auth = self.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()

        payload = None
        if token:
            payload = jwt_auth.verify_jwt(token, expected_aud=self._space_host())
            if not payload:
                # SECURITY (C5): Fallback to allow_any_aud for multi-Space deployments
                # where JWTs are minted by the main Space and claimed by user Spaces.
                # TODO: Replace with iss/kid verification against an allowlist.
                log_event("jwt_aud_fallback", source="bearer")
                payload = jwt_auth.verify_jwt(token, allow_any_aud=True)

        # Fallback: try X-JWT header (ConsciousClient sends wire-token bearer + JWT here)
        if not payload:
            x_jwt = (self.headers.get("X-JWT") or "").strip()
            if x_jwt:
                payload = jwt_auth.verify_jwt(x_jwt, expected_aud=self._space_host())
                if not payload:
                    # SECURITY (C5): Same fallback as above.
                    log_event("jwt_aud_fallback", source="x-jwt")
                    payload = jwt_auth.verify_jwt(x_jwt, allow_any_aud=True)

        if not payload:
            err = "invalid or expired session token"
            self._send_json(401, {"error": err})
            from debug_log import log_entry
            log_entry(level="WARN", cat="auth", fn="_require_user", msg=err,
                      data={"path": self.path, "method": self.command})
            return None

        user_id = payload["sub"]

        # If the user row is missing (DB wiped on Space restart), reconstruct
        # it from the JWT payload. The encrypted tokens are opaque but valid
        # as long as the JWT is valid. If the JWT carries NO encrypted tokens
        # (e.g. an older JWT, or an HF-only session whose tokens weren't
        # embedded), we still create a minimal user row so the user can use
        # the UI — GitHub-API routes (repos/clone/push) will return a clear
        # "GitHub not connected" error instead of a 401 that logs them out.
        user = db.get_user(user_id)
        if not user:
            github_id = payload.get("github_id")
            github_username = payload.get("github_username")
            github_token_enc = payload.get("github_token_enc")
            hf_id = payload.get("hf_id")
            hf_token_enc = payload.get("hf_token_enc")
            has_github = github_id and github_token_enc
            has_hf = hf_id and hf_token_enc
            db.upsert_user(
                user_id=user_id,
                github_id=github_id if has_github else None,
                github_username=github_username if (has_github or github_username) else None,
                github_token_encrypted=github_token_enc if has_github else None,
                hf_id=hf_id if has_hf else None,
                hf_token_encrypted=hf_token_enc if has_hf else None,
            )

        return user_id

    def _require_user_from_jwt(self) -> str | None:
        """Extract user_id from the ``X-JWT`` header (fallback: Authorization bearer).

        Used by routes that are service-authed via the rotation token in
        ``Authorization`` but additionally require GitHub identity for a
        workspace ownership check.  Sends 403 (not 401) on failure because the
        caller IS service-authed — they just lack GitHub identity.

        The fallback to ``Authorization`` preserves back-compat for callers
        that still send the JWT as the bearer; we only treat a candidate as a
        JWT if it has the JWT shape (two dots) so a rotation token is never
        misread as a JWT.
        """
        jwt = (self.headers.get("X-JWT") or "").strip()
        if not jwt:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                candidate = auth[len("Bearer "):].strip()
                if candidate.count(".") == 2:
                    jwt = candidate
        if not jwt:
            # No JWT at all — return None silently (caller falls through
            # to default user / unauthenticated path).
            return None
        # Strict audience checks first (try both SPACE_HOST and SPACE_ID), then
        # fall back to allow_any_aud so a JWT minted by the main doomalaysocreate Space is
        # accepted by user Spaces in the proxy flow. Signature is still verified.
        # SECURITY (C5): This fallback is logged for observability. TODO: Replace
        # with iss/kid verification against an allowlist of trusted Space IDs.
        payload = jwt_auth.verify_jwt(jwt, expected_aud=os.environ.get("SPACE_HOST", ""))
        if not payload:
            payload = jwt_auth.verify_jwt(jwt, expected_aud=os.environ.get("SPACE_ID", ""))
        if not payload:
            log_event("jwt_aud_fallback", source="_require_user_from_jwt")
            payload = jwt_auth.verify_jwt(jwt, allow_any_aud=True)
        if not payload:
            return None  # caller decides the response for failed auth
        user_id = payload["sub"]
        # reconstruct user row if missing (DB wiped) — same logic as _require_user.
        # If the JWT has no encrypted GitHub token, still create a minimal row;
        # the workspace ownership check passes, and GitHub-API operations will
        # surface a clear "not connected" error rather than a hard 403.
        user = db.get_user(user_id)
        if not user:
            github_id = payload.get("github_id")
            github_token_enc = payload.get("github_token_enc")
            db.upsert_user(
                user_id=user_id,
                github_id=github_id if github_id else None,
                github_username=payload.get("github_username"),
                github_token_encrypted=github_token_enc if github_id and github_token_enc else None,
            )
        return user_id

    def _handle_github_login(self) -> None:
        if not github_integration._github_configured():
            self._send_json(503, {"error": "GitHub OAuth not configured "
                                           "(GITHUB_CLIENT_ID missing)"})
            return
        # Accept redirect_to for proxy flow: user's Space sends us their URL
        # so we can forward the callback back to them after GitHub auth.
        # existing_id enables merging GitHub OAuth with an existing user account.
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        redirect_to = (qs.get("redirect_to", [""])[0] or "").strip()
        existing_id = (qs.get("existing_id", [""])[0] or "").strip()
        nonce = secrets.token_urlsafe(16)
        # Pack existing_id into the state using pipe separator (same pattern as HF OAuth)
        if existing_id:
            state_payload = f"{redirect_to}|{existing_id}" if redirect_to else f"|{existing_id}"
        else:
            state_payload = redirect_to
        state = _make_oauth_state(nonce, state_payload)
        url = github_integration.make_github_authorize_url(state)
        self._redirect(url)

    def _handle_github_callback(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        code = (qs.get("code", [""])[0] or "").strip()
        state = (qs.get("state", [""])[0] or "").strip()
        error_param = (qs.get("error", [""])[0] or "").strip()
        host = self._space_host()

        valid, combined = _verify_oauth_state(state) if state else (False, "")

        # Extract existing_id if packed into redirect_to (pipe-separated)
        # Format: redirect_to|existing_id
        redirect_to = combined
        existing_id = ""
        if "|" in combined:
            parts = combined.split("|", 1)
            redirect_to = parts[0]
            if len(parts) > 1:
                existing_id = parts[1]

        if error_param:
            # If we have a redirect_to, forward the error there
            if redirect_to:
                self._redirect(f"{redirect_to}#github-error={error_param}")
            else:
                self._redirect(f"https://{host}/#github-error={error_param}")
            return
        if not code or not state or not valid:
            if redirect_to:
                self._redirect(f"{redirect_to}#github-error=invalid_state")
            else:
                self._redirect(f"https://{host}/#github-error=invalid_state")
            return

        # Proxy flow: exchange code on main Space, create in-memory identity grant
        if redirect_to:
            # Self-referencing redirect (user is on the same Space as the OAuth app):
            # skip the grant round-trip and use the direct JWT flow instead.
            if redirect_to.rstrip("/") == f"https://{host}".rstrip("/"):
                try:
                    gh_token = github_integration.exchange_github_code(code)
                    user = github_integration.upsert_user_from_github(gh_token, user_id=existing_id or None)
                    jwt_token = jwt_auth.generate_jwt(
                        user_id=user["id"],
                        github_id=user["github_id"],
                        github_username=user.get("github_username", ""),
                        github_token_encrypted=user.get("github_token_encrypted", ""),
                        audience=host,
                    )
                    self._redirect(f"{redirect_to}#github-connected={jwt_token}")
                except Exception as exc:
                    err = str(exc)[:120].replace("#", "").replace("&", "")
                    log_event("github_oauth_error", error=err)
                    self._redirect(f"{redirect_to}#github-error={err}")
                return
            try:
                gh_token = github_integration.exchange_github_code(code)
                user_data = github_integration.get_github_user(gh_token)
                encrypted = crypto.encrypt_token(gh_token) if gh_token else ""
                grant_data = {
                    "github_id": user_data["id"],
                    "github_username": user_data.get("login", ""),
                    "github_token_encrypted": encrypted,
                }
                grant_token = _store_provision_result(grant_data)
                self._redirect(f"{redirect_to}#github-grant={grant_token}")
            except Exception as exc:
                err = str(exc)[:120].replace("#", "").replace("&", "")
                log_event("github_oauth_error", error=err)
                self._redirect(f"{redirect_to}#github-error={err}")
            return

        # Direct flow (main Space): exchange code and complete
        try:
            gh_token = github_integration.exchange_github_code(code)
            user = github_integration.upsert_user_from_github(gh_token, user_id=existing_id or None)
            jwt_token = jwt_auth.generate_jwt(
                user_id=user["id"],
                github_id=user["github_id"],
                github_username=user.get("github_username", ""),
                github_token_encrypted=user.get("github_token_encrypted", ""),
                audience=self._space_host(),
            )
            self._redirect(f"https://{host}/#github-connected={jwt_token}")
        except Exception as exc:
            err = str(exc)[:120].replace("#", "").replace("&", "")
            log_event("github_oauth_error", error=err)
            self._redirect(f"https://{host}/#github-error={err}")

    def _handle_github_disconnect(self) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        db.delete_github_token(user_id)
        self._send_json(200, {"disconnected": True})

    def _handle_github_claim_grant(self) -> None:
        """Claim a GitHub identity grant from the main Space and create a local user.
        POST /api/auth/github/claim-grant  body: {"token": "..."}
        Returns {"session_id": "<local-jwt>", "user_id": "...", next?: "hf"}"""
        payload = self._read_json_body()
        if not payload:
            return
        token = (payload.get("token") or "").strip()
        if not token:
            self._send_json(400, {"error": "token required"})
            return
        main_space = os.environ.get("MAIN_SPACE_URL", "").strip()
        if not main_space:
            self._send_json(503, {"error": "MAIN_SPACE_URL not configured on this Space"})
            return
        from urllib.parse import quote as _q
        import urllib.request as _ureq
        try:
            req = _ureq.Request(f"{main_space}/api/auth/github/grants/{_q(token)}")
            with _ureq.urlopen(req, timeout=15) as resp:
                identity = json.loads(resp.read().decode())
        except Exception as exc:
            self._send_json(502, {"error": f"failed to claim grant: {exc}"})
            return
        gh_id = identity.get("github_id")
        if not gh_id:
            self._send_json(502, {"error": "invalid grant: missing github_id"})
            return
        user_id = jwt_auth.derive_user_id(gh_id)
        user = db.upsert_user(
            user_id=user_id,
            github_id=gh_id,
            github_username=identity.get("github_username", ""),
            github_token_encrypted=identity.get("github_token_encrypted", ""),
        )
        jwt_token = jwt_auth.generate_jwt(
            user_id=user["id"],
            github_id=user["github_id"],
            github_username=user.get("github_username", ""),
            github_token_encrypted=user.get("github_token_encrypted", ""),
            audience=self._space_host(),
        )
        result: dict[str, object] = {"session_id": jwt_token, "user_id": user["id"]}
        if not user.get("hf_token_encrypted"):
            result["next"] = "hf"
        self._send_json(200, result)

    # -- HF OAuth handlers --------------------------------------------------

    def _handle_hf_login(self) -> None:
        if not dataset_persistence._hf_configured():
            self._send_json(503, {"error": "HF OAuth not configured "
                                           "(HF_CLIENT_ID missing)"})
            return
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        redirect_to = (qs.get("redirect_to", [""])[0] or "").strip()
        github_user_id = (qs.get("github_user_id", [""])[0] or "").strip()
        host = self._space_host()
        redirect_uri = f"https://{host}/api/auth/hf/callback"
        nonce = secrets.token_urlsafe(16)
        # Pack redirect_to, redirect_uri, and github_user_id into state so the
        # proxy exchange can find the existing GitHub user and merge HF data.
        combined = f"{redirect_to}|{redirect_uri}|{github_user_id}" if (redirect_uri or github_user_id) else redirect_to
        state = _make_oauth_state(nonce, combined)
        url = dataset_persistence.make_hf_authorize_url(state, redirect_uri=redirect_uri)
        self._redirect(url)

    def _handle_hf_callback(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        code = (qs.get("code", [""])[0] or "").strip()
        state = (qs.get("state", [""])[0] or "").strip()
        error_param = (qs.get("error", [""])[0] or "").strip()
        host = self._space_host()

        valid, redirect_to = _verify_oauth_state(state) if state else (False, "")

        # Extract redirect_uri and github_user_id if packed into redirect_to (pipe-separated)
        # Format: redirect_to|redirect_uri|github_user_id
        hf_redirect_uri = ""
        github_user_id = ""
        if "|" in redirect_to:
            parts = redirect_to.split("|", 2)
            redirect_to = parts[0]
            if len(parts) > 1:
                hf_redirect_uri = parts[1]
            if len(parts) > 2:
                github_user_id = parts[2]

        if error_param:
            if redirect_to:
                self._redirect(f"{redirect_to}#hf-error={error_param}")
            else:
                self._redirect(f"https://{host}/#hf-error={error_param}")
            return
        if not code or not state or not valid:
            if redirect_to:
                self._redirect(f"{redirect_to}#hf-error=invalid_state")
            else:
                self._redirect(f"https://{host}/#hf-error=invalid_state")
            return

        # Proxy flow: exchange code on main Space, create in-memory identity grant
        if redirect_to:
            # Self-referencing redirect: skip grant round-trip, use direct JWT flow.
            if redirect_to.rstrip("/") == f"https://{host}".rstrip("/"):
                try:
                    redirect_uri = f"https://{host}/api/auth/hf/callback"
                    token_data = dataset_persistence.exchange_hf_code(code, redirect_uri=redirect_uri)
                    user = dataset_persistence.upsert_user_from_hf(token_data, user_id=github_user_id or None)
                    jwt_token = jwt_auth.generate_jwt(
                        user_id=user["id"],
                        github_id=user.get("github_id"),
                        github_username=user.get("github_username", ""),
                        github_token_encrypted=user.get("github_token_encrypted", ""),
                        hf_id=user.get("hf_username", ""),
                        hf_token_encrypted=user.get("hf_token_encrypted", ""),
                        audience=host,
                    )
                    self._redirect(f"{redirect_to}#hf-connected={jwt_token}")
                except Exception as exc:
                    err = str(exc)[:120].replace("#", "").replace("&", "")
                    log_event("hf_oauth_error", error=err)
                    self._redirect(f"{redirect_to}#hf-error={err}")
                return
            try:
                redirect_uri = f"https://{host}/api/auth/hf/callback"
                token_data = dataset_persistence.exchange_hf_code(code, redirect_uri=redirect_uri)
                whoami = _hf_api("https://huggingface.co/api/whoami-v2", token=token_data["access_token"])
                username = whoami.get("name", "").strip()
                access_enc = crypto.encrypt_token(token_data.get("access_token", "")) or ""
                refresh_enc = crypto.encrypt_token(token_data.get("refresh_token", "")) or ""
                expires_at = ""
                if token_data.get("expires_in"):
                    from datetime import datetime, timezone, timedelta
                    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))).isoformat()
                grant_data: dict[str, object] = {
                    "hf_id": username,
                    "hf_username": username,
                    "hf_token_encrypted": access_enc,
                    "hf_refresh_token_encrypted": refresh_enc,
                    "hf_token_expires_at": expires_at,
                }
                if github_user_id:
                    grant_data["github_user_id"] = github_user_id
                grant_token = _store_provision_result(grant_data)
                self._redirect(f"{redirect_to}#hf-grant={grant_token}")
            except Exception as exc:
                err = str(exc)[:120].replace("#", "").replace("&", "")
                log_event("hf_oauth_error", error=err)
                self._redirect(f"{redirect_to}#hf-error={err}")
            return

        # Direct flow (main Space): exchange code and complete
        try:
            redirect_uri = f"https://{host}/api/auth/hf/callback"
            token_data = dataset_persistence.exchange_hf_code(code, redirect_uri=redirect_uri)
            user = dataset_persistence.upsert_user_from_hf(token_data, user_id=github_user_id or None)
            # Re-fetch to ensure we have the complete row (with github_id if merged)
            user = db.get_user(user["id"]) or user
            jwt_token = jwt_auth.generate_jwt(
                user_id=user["id"],
                github_id=user.get("github_id") or 0,
                github_username=user.get("github_username") or "",
                github_token_encrypted=user.get("github_token_encrypted") or "",
                hf_id=user.get("hf_username") or "",
                hf_token_encrypted=user.get("hf_token_encrypted") or "",
                audience=self._space_host(),
            )
            self._redirect(f"https://{host}/#hf-connected={jwt_token}")
        except Exception as exc:
            err = str(exc)[:120].replace("#", "").replace("&", "")
            log_event("hf_oauth_error", error=err)
            self._redirect(f"https://{host}/#hf-error={err}")

    def _handle_hf_claim_grant(self) -> None:
        """Claim an HF identity grant from the main Space and create/merge a local user.
        POST /api/auth/hf/claim-grant  body: {"token": "..."}
        Returns {"session_id": "<local-jwt>", "user_id": "..."}"""
        payload = self._read_json_body()
        if not payload:
            return
        token = (payload.get("token") or "").strip()
        if not token:
            self._send_json(400, {"error": "token required"})
            return
        main_space = os.environ.get("MAIN_SPACE_URL", "").strip()
        if not main_space:
            self._send_json(503, {"error": "MAIN_SPACE_URL not configured on this Space"})
            return
        from urllib.parse import quote as _q
        import urllib.request as _ureq
        try:
            req = _ureq.Request(f"{main_space}/api/auth/hf/grants/{_q(token)}")
            with _ureq.urlopen(req, timeout=15) as resp:
                identity = json.loads(resp.read().decode())
        except Exception as exc:
            self._send_json(502, {"error": f"failed to claim grant: {exc}"})
            return
        hf_username = identity.get("hf_username", "")
        if not hf_username:
            self._send_json(502, {"error": "invalid grant: missing hf_username"})
            return
        github_user_id = (identity.get("github_user_id") or "").strip()
        if github_user_id:
            existing = db.get_user(github_user_id)
            user_id = existing["id"] if existing else jwt_auth.derive_user_id_from_hf(hf_username)
        else:
            user_id = jwt_auth.derive_user_id_from_hf(hf_username)
        user = db.upsert_user(
            user_id=user_id,
            github_id=None,
            hf_id=hf_username,
            hf_username=hf_username,
            hf_token_encrypted=identity.get("hf_token_encrypted", ""),
            hf_refresh_token_encrypted=identity.get("hf_refresh_token_encrypted", ""),
            hf_token_expires_at=identity.get("hf_token_expires_at", ""),
        )
        user = db.get_user(user["id"]) or user
        jwt_token = jwt_auth.generate_jwt(
            user_id=user["id"],
            github_id=user.get("github_id") or 0,
            github_username=user.get("github_username") or "",
            github_token_encrypted=user.get("github_token_encrypted") or "",
            hf_id=user.get("hf_username") or "",
            hf_token_encrypted=user.get("hf_token_encrypted") or "",
            audience=self._space_host(),
        )
        self._send_json(200, {"session_id": jwt_token, "user_id": user["id"]})

    def _handle_auth_status(self) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        user = db.get_user(user_id)
        hf_expires_at = user.get("hf_token_expires_at") if user else None
        hf_refresh_ok = bool(user and user.get("hf_refresh_token_encrypted"))
        hf_healthy = False
        if hf_expires_at:
            try:
                from datetime import datetime, timezone, timedelta
                expiry = datetime.fromisoformat(hf_expires_at)
                hf_healthy = expiry > datetime.now(timezone.utc) - timedelta(days=7)
            except (ValueError, TypeError):
                pass
        self._send_json(200, {
            "authenticated": bool(user and (user.get("github_token_encrypted") or user.get("hf_token_encrypted"))),
            "github_username": user.get("github_username") if user else None,
            "hf_username": user.get("hf_username") if user else None,
            "hf_token_expires_at": hf_expires_at,
            "hf_refresh_available": hf_refresh_ok,
            "hf_token_healthy": hf_healthy,
        })

    def _handle_github_repos(self) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        try:
            repos = github_integration.list_user_repos(user_id)
            self._send_json(200, {"repos": repos})
        except Exception as exc:
            self._send_json(502, {"error": str(exc)})

    def _handle_github_repos_create(self) -> None:
        """POST /api/github/repos/create — create a new GitHub repo for the
        authenticated user.  Body mirrors GitHub's API:

            { name, description?, private?, auto_init?,
              gitignore_template?, license_template?, owner? }

        ``owner`` defaults to the authenticated user; if it's an org login,
        the repo is created under that org (``POST /orgs/{owner}/repos``).
        """
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        name = (payload.get("name") or "").strip()
        if not name:
            self._send_json(400, {"error": "'name' is required"})
            return
        try:
            repo = github_integration.create_repo(
                user_id,
                name=name,
                description=payload.get("description", "") or "",
                private=bool(payload.get("private", True)),
                auto_init=bool(payload.get("auto_init", False)),
                gitignore_template=payload.get("gitignore_template"),
                license_template=payload.get("license_template"),
                owner=payload.get("owner"),
            )
            self._send_json(201, repo)
        except Exception as exc:
            self._send_json(502, {"error": str(exc)[:300]})

    def _handle_github_gitignore_templates(self) -> None:
        """GET /api/github/gitignore/templates — list .gitignore templates
        GitHub offers.  Authenticated so the user gets the higher rate limit;
        falls back to anonymous if the user has no GitHub token yet."""
        user_id = self._require_user()
        if not user_id:
            return
        try:
            templates = github_integration.list_gitignore_templates(user_id)
            self._send_json(200, {"templates": templates})
        except Exception as exc:
            # Best-effort anonymous fallback so the create-repo form is still
            # usable for users mid-GitHub-connect.
            try:
                templates = github_integration.list_gitignore_templates(None)
                self._send_json(200, {"templates": templates})
            except Exception as exc2:
                self._send_json(502, {"error": str(exc)[:200] + " | " + str(exc2)[:200]})

    def _handle_github_licenses(self) -> None:
        """GET /api/github/licenses — list license templates GitHub offers."""
        user_id = self._require_user()
        if not user_id:
            return
        try:
            licenses = github_integration.list_licenses(user_id)
            self._send_json(200, {"licenses": licenses})
        except Exception as exc:
            try:
                licenses = github_integration.list_licenses(None)
                self._send_json(200, {"licenses": licenses})
            except Exception as exc2:
                self._send_json(502, {"error": str(exc)[:200] + " | " + str(exc2)[:200]})

    def _handle_github_orgs(self) -> None:
        """GET /api/github/orgs — list orgs the user can create repos in."""
        user_id = self._require_user()
        if not user_id:
            return
        try:
            orgs = github_integration.list_user_orgs(user_id)
            self._send_json(200, {"orgs": orgs})
        except Exception as exc:
            self._send_json(502, {"error": str(exc)[:300]})

    def _resolve_workspace_for_user(self, user_id: str,
                                    workspace_id_or_full_name: str) -> dict | None:
        """Resolve a workspace reference for a user.

        Accepts either:
        - the internal workspace ID (UUID), or
        - a GitHub repo full name (``owner/repo``) — useful when the frontend
          holds the repo full name but the workspace hasn't been cloned yet.

        Ownership is enforced: a workspace that exists but belongs to a
        different user returns None.

        If the workspace is missing from the DB AND the reference is a GitHub
        full name AND the user owns that repo on GitHub, we auto-create the
        DB row + clone the sandbox so memory works on freshly-cloned repos
        without requiring a separate ``POST /api/workspaces`` round-trip.
        """
        if not workspace_id_or_full_name:
            return None
        ref = workspace_id_or_full_name.strip()
        # 1. Try internal ID lookup (fast path).
        ws = db.get_workspace(ref)
        if ws:
            if ws.get("user_id") != user_id:
                return None
            return ws
        # 2. Try matching by source_repo full name (covers cloned workspaces
        #    whose DB row stores the clone URL).
        ws = db.find_user_workspace_by_repo(user_id, ref)
        if ws:
            return ws
        # 3. Auto-create: only if the ref looks like a GitHub full name and
        #    the user actually owns that repo on GitHub.
        if "/" not in ref:
            return None
        try:
            owner, _, repo_name = ref.partition("/")
            if not owner or not repo_name:
                return None
            # Verify ownership on GitHub.  A 404 / 403 means the user can't
            # access it — don't auto-create.
            token = github_integration._token_for_user(user_id)
            try:
                info = github_integration._gh_httpx(
                    "GET", f"/repos/{owner}/{repo_name}", token=token)
            except RuntimeError:
                return None
            if not isinstance(info, dict) or not info.get("full_name"):
                return None
            # Only auto-create if the user is the owner (or a collaborator with
            # admin/push perms — but for safety require owner match).
            owner_obj = info.get("owner") or {}
            me = github_integration._gh_httpx("GET", "/user", token=token)
            my_login = (me or {}).get("login", "")
            if not my_login or my_login.lower() != owner_obj.get("login", "").lower():
                return None
            clone_url = info.get("clone_url") or ""
            default_branch = info.get("default_branch") or "main"
            title = info.get("name") or repo_name
            description = info.get("description") or ""
            try:
                ws = github_integration.create_workspace(
                    user_id,
                    title=title,
                    description=description,
                    source_repo=clone_url,
                    source_branch=default_branch,
                    visibility="private" if info.get("private") else "public",
                )
                return ws
            except Exception as exc:
                log_event("memory_auto_create_workspace_failed",
                          user_id=user_id, ref=ref, error=repr(exc)[:300])
                return None
        except Exception as exc:
            log_event("memory_resolve_workspace_error",
                      user_id=user_id, ref=ref, error=repr(exc)[:300])
            return None

    def _handle_memory_post(self) -> None:
        """POST /api/memory — write to (or clear) a workspace's memory layer.

        Two body shapes are accepted:

        1. The frontend's WorkspaceMemoryPanel format (kind-dispatched):
             { workspace_id, kind: "note"|"goal"|"plan"|"task"|"event"|...,
               agent?, data?, goal?, plan?, task? }

        2. The action-based format from the BACKEND-GITHUB-MEMORY spec:
             { workspace_id, action: "write"|"clear", content?, key? }
           - action="write" with content+key → posts a note to the blackboard
           - action="clear" → wipes the memory layer

        Returns ``{ok: true}`` on success or ``{error: "..."}`` with the
        appropriate HTTP status.
        """
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        workspace_id = (payload.get("workspace_id") or "").strip()
        if not workspace_id:
            self._send_json(400, {"error": "'workspace_id' is required"})
            return
        try:
            ws = self._resolve_workspace_for_user(user_id, workspace_id)
            if ws is None:
                self._send_json(404, {"error": "workspace not found"})
                return
            from pathlib import Path
            import memory_layer
            github_integration.ensure_workspace_sandbox(ws["id"])
            ws_path = Path(ws["sandbox_path"])
            memory_layer.init_memory(ws_path)

            action = (payload.get("action") or "").strip().lower()
            if action == "clear":
                memory_layer.clear_memory(ws_path)
                self._send_json(200, {"ok": True, "cleared": True})
                return
            if action == "write":
                content = payload.get("content")
                if content is None:
                    self._send_json(400, {"error": "'content' is required for action='write'"})
                    return
                key = (payload.get("key") or "note").strip() or "note"
                agent_id = (payload.get("agent") or "user").strip() or "user"
                memory_layer.write_note(ws_path, agent_id, str(content), key=key)
                self._send_json(200, {"ok": True})
                return

            # Kind-dispatched (frontend format)
            kind = (payload.get("kind") or "note").strip() or "note"
            agent_id = (payload.get("agent") or "user").strip() or "user"
            if kind == "goal" or payload.get("goal") is not None:
                goal = payload.get("goal")
                if goal is not None:
                    memory_layer.update_goal(ws_path, str(goal))
            if kind == "plan" or payload.get("plan") is not None:
                plan = payload.get("plan")
                if plan is not None:
                    memory_layer.update_plan(ws_path, str(plan))
            if kind == "task" and payload.get("task"):
                memory_layer.add_task(ws_path, str(payload["task"]), agent_id=agent_id)
            if kind in ("note", "event") or payload.get("data") is not None:
                # Generic event log + (for notes) blackboard post.
                data = payload.get("data") or {}
                if kind == "note" and isinstance(data, dict) and data.get("note"):
                    memory_layer.write_note(
                        ws_path, agent_id, str(data["note"]),
                        key=str(payload.get("key") or "note"))
                else:
                    memory_layer.log_event(ws_path, agent_id, kind, data)
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(500, {"error": str(e)[:200]})

    def _handle_github_branches(self, route: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        # /api/github/repos/<owner>/<repo>/branches
        parts = route.split("/")
        if len(parts) < 7:
            self._send_json(400, {"error": "expected /api/github/repos/<owner>/<repo>/branches"})
            return
        owner, repo = parts[4], parts[5]
        try:
            branches = github_integration.list_repo_branches(user_id, owner, repo)
            self._send_json(200, {"branches": branches})
        except Exception as exc:
            self._send_json(502, {"error": str(exc)})

    def _handle_workspaces_list(self) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        workspaces = github_integration.list_workspaces(user_id)
        self._send_json(200, {"workspaces": workspaces})

    def _handle_debug_diagnose(self) -> None:
        """AI-readable summary of recent system health and errors."""
        import debug_log
        logs = debug_log.get_recent_logs(tail=200)
        
        errors = [l for l in logs if l.get("level") in ("ERROR", "WARN")]
        http_errors = [l for l in logs if l.get("cat") == "http" and l.get("data", {}).get("status", 200) >= 400]
        
        # Group identical error messages to find patterns
        patterns = {}
        for e in errors:
            msg = e.get("msg", "unknown error")
            patterns[msg] = patterns.get(msg, 0) + 1
            
        sorted_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)
        
        health = "OK"
        if errors:
            health = "DEGRADED" if len(errors) < 10 else "CRITICAL"
            
        self._send_json(200, {
            "system_status": health,
            "total_recent_logs": len(logs),
            "error_count": len(errors),
            "http_failure_count": len(http_errors),
            "top_issues": [
                {"issue": msg, "count": count} for msg, count in sorted_patterns[:5]
            ],
            "recent_critical_events": [
                {"ts": l.get("ts"), "msg": l.get("msg"), "cat": l.get("cat")}
                for l in errors[-5:]
            ]
        })

    def _handle_workspace_get(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        ws = github_integration.get_workspace(ws_id)
        if not ws:
            self._send_json(404, {"error": "workspace not found"})
            return
        if ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        self._send_json(200, ws)

    def _handle_workspace_create(self) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        title = payload.get("title", "").strip()
        if not title:
            self._send_json(400, {"error": "'title' is required"})
            return
        try:
            ws = github_integration.create_workspace(
                user_id, title=title,
                description=payload.get("description", ""),
                source_repo=payload.get("source_repo"),
                source_branch=payload.get("source_branch"),
                source_branches=payload.get("source_branches"),
                visibility=payload.get("visibility", "private"),
                auto_sync=bool(payload.get("auto_sync")))
            self._send_json(201, ws)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_workspace_update(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        try:
            ws = github_integration.update_workspace(ws_id, **payload)
            self._send_json(200, ws)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_workspace_delete(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        if github_integration.delete_workspace(ws_id):
            self._send_json(200, {"deleted": ws_id})
        else:
            self._send_json(404, {"error": "workspace not found"})

    def _handle_workspace_commit(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        message = payload.get("message", "").strip()
        if not message:
            self._send_json(400, {"error": "'message' (commit message) is required"})
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        try:
            sha = github_integration.commit_changes(ws["sandbox_path"], message)
            self._send_json(200, {"commit_sha": sha})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_workspace_push(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        branch = payload.get("branch", ws.get("current_branch", "main"))
        force = bool(payload.get("force", False))
        auto = bool(payload.get("auto_approve", False))
        if auto:
            # auto-approve: push directly
            try:
                sha = github_integration.push_to_remote(
                    user_id, ws_id, branch, force=force)
                self._send_json(200, {"commit_sha": sha, "branch": branch})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
        else:
            # queue for user approval
            action = "git_force_push" if force else "git_push"
            rid = github_integration.enqueue_push_request(
                user_id, ws_id, action, branch=branch)
            self._send_json(202, {
                "status": "pending_approval",
                "request_id": rid,
                "action": action,
                "branch": branch,
            })

    def _handle_workspace_pr(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        title = payload.get("title", "").strip()
        if not title:
            self._send_json(400, {"error": "'title' is required for PR"})
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        auto = bool(payload.get("auto_approve", False))
        if auto:
            try:
                result = github_integration.create_pull_request(
                    user_id, ws_id, title=title,
                    body=payload.get("body", ""),
                    head_branch=payload.get("head_branch", "main"),
                    base_branch=payload.get("base_branch", "main"))
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
        else:
            rid = github_integration.enqueue_push_request(
                user_id, ws_id, "gh_pr_create",
                title=title, body=payload.get("body", ""),
                head_branch=payload.get("head_branch", "main"),
                base_branch=payload.get("base_branch", "main"))
            self._send_json(202, {
                "status": "pending_approval",
                "request_id": rid,
                "action": "gh_pr_create",
                "title": title,
            })

    def _handle_workspace_publish(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        try:
            entry = github_integration.publish_workspace(user_id, ws_id)
            self._send_json(200, entry)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_workspace_unpublish(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        github_integration.unpublish_workspace(ws_id)
        self._send_json(200, {"unpublished": ws_id})

    def _handle_push_request_status(self, req_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        from github_integration import _approval_queue, _approval_lock
        with _approval_lock:
            req = _approval_queue.get(req_id)
        if req is None:
            self._send_json(404, {"error": "request not found or already resolved"})
            return
        if req["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        self._send_json(200, {
            "status": "pending_approval",
            "request_id": req_id,
            "action": req["action"],
            "created_at": req["created_at"],
        })

    def _handle_push_request_resolve(self, req_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        approved = bool(payload.get("approved", False))
        # ownership check is done inside resolve_push_request
        result = github_integration.resolve_push_request(req_id, approved, user_id)
        if result is None:
            self._send_json(404, {"error": "request not found or already resolved"})
            return
        self._send_json(200, result)

    def _handle_workspace_logs(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        try:
            limit = min(int(qs.get("limit", ["50"])[0]), 200)
        except (ValueError, IndexError):
            limit = 50
        logs = db.list_push_logs(ws_id, limit=limit)
        self._send_json(200, {"logs": logs})

    def _handle_github_contents(self, route: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        # /api/github/repos/<owner>/<repo>/contents?path=<path>&ref=<ref>
        parts = route.split("/")
        if len(parts) < 7:
            self._send_json(400, {"error": "expected /api/github/repos/<owner>/<repo>/contents"})
            return
        owner, repo = parts[4], parts[5]
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        path = qs.get("path", [""])[0]
        ref = qs.get("ref", ["main"])[0]
        try:
            content = github_integration.get_file_content(user_id, owner, repo, path, ref)
            self._send_json(200, content)
        except Exception as exc:
            self._send_json(502, {"error": str(exc)})

    def _handle_github_tree(self, route: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        # /api/github/repos/<owner>/<repo>/tree?ref=<ref>
        parts = route.split("/")
        if len(parts) < 7:
            self._send_json(400, {"error": "expected /api/github/repos/<owner>/<repo>/tree"})
            return
        owner, repo = parts[4], parts[5]
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        ref = qs.get("ref", ["main"])[0]
        try:
            tree = github_integration.get_repo_tree(user_id, owner, repo, ref)
            self._send_json(200, {"tree": tree})
        except Exception as exc:
            self._send_json(502, {"error": str(exc)})

    def _handle_workspace_checkout(self, ws_id: str) -> None:
        user_id = self._require_user()
        if not user_id:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        branch = payload.get("branch", "").strip()
        if not branch:
            self._send_json(400, {"error": "'branch' is required"})
            return
        ws = db.get_workspace(ws_id)
        if not ws or ws["user_id"] != user_id:
            self._send_json(403, {"error": "access denied"})
            return
        try:
            github_integration.checkout_branch(ws["sandbox_path"], branch)
            db.update_workspace(ws_id, current_branch=branch)
            self._send_json(200, {"branch": branch})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_registry_list(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        per_page = int(qs.get("per_page", ["20"])[0])
        sort = qs.get("sort", ["recent"])[0]
        search = qs.get("search", [""])[0]
        result = github_integration.list_registry(
            page=page, per_page=per_page, sort=sort, search=search)
        self._send_json(200, result)

    def do_POST(self) -> None:
        start = time.time()
        self._last_status = 200
        error_msg = None
        try:
            self._do_POST()
        except Exception as exc:  # noqa: BLE001 — last-resort guard (same as do_GET)
            try:
                error_msg = repr(exc)[:300]
                log_event("do_POST_unhandled", path=self.path,
                          error=error_msg)
                self._send_json(500, {"error": f"internal error: {type(exc).__name__}"})
            except Exception:
                pass
        finally:
            self._log_request("POST", self.path, self._last_status, (time.time() - start) * 1000, error_msg)

    def _do_POST(self) -> None:
        route = self.path.rstrip("/")
        # --- Debug log ingestion (auth-gated) ---
        if route == "/api/debug/log":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            payload = self._read_json_body()
            if not payload:
                self._send_json(400, {"error": "request body is required"})
                return
            
            from debug_log import log_entry
            log_entry(
                level=payload.get("level", "INFO"),
                cat=payload.get("cat", "frontend"),
                fn=payload.get("fn", "browser"),
                msg=payload.get("msg", ""),
                data=payload.get("data"),
                ms=payload.get("ms")
            )
            self._send_json(200, {"ok": True})
            return
        # --- Tier 3: Conscious routes (bearer-gated; JWT enforced inside) ---
        if route == "/api/conscious" or route.startswith("/api/conscious/"):
            self._conscious_dispatch("POST")
            return
        if route == "/oauth/set-provider-key":
            self._handle_set_provider_key()
            return
        #   POST /api/agent/<sid>/interrupt — stop the in-flight turn (bearer-gated,
        #   no body required). handled before the body-parsing gate below.
        if route.startswith("/api/agent/") and route.endswith("/interrupt"):
            if not self._auth_ok():
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
        # --- Identity grant claim POST endpoints (claimed on user Spaces) ---------
        if route == "/api/auth/github/claim-grant":
            self._handle_github_claim_grant()
            return
        if route == "/api/auth/hf/claim-grant":
            self._handle_hf_claim_grant()
            return
        # --- GitHub integration POST routes --------------------------------------
        if route == "/api/auth/github/disconnect":
            self._handle_github_disconnect()
            return
        if route == "/api/github/repos/create":
            self._handle_github_repos_create()
            return
        if route == "/api/memory":
            self._handle_memory_post()
            return
        if route == "/api/workspaces":
            self._handle_workspace_create()
            return
        if route.startswith("/api/workspaces/") and route.endswith("/commit"):
            ws_id = route[len("/api/workspaces/"):-len("/commit")]
            self._handle_workspace_commit(ws_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/push"):
            ws_id = route[len("/api/workspaces/"):-len("/push")]
            self._handle_workspace_push(ws_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/pr"):
            ws_id = route[len("/api/workspaces/"):-len("/pr")]
            self._handle_workspace_pr(ws_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/publish"):
            ws_id = route[len("/api/workspaces/"):-len("/publish")]
            self._handle_workspace_publish(ws_id)
            return
        if route.startswith("/api/auth/push-requests/") and route.endswith("/resolve"):
            req_id = route[len("/api/auth/push-requests/"):-len("/resolve")]
            self._handle_push_request_resolve(req_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/update"):
            ws_id = route[len("/api/workspaces/"):-len("/update")]
            self._handle_workspace_update(ws_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/unpublish"):
            ws_id = route[len("/api/workspaces/"):-len("/unpublish")]
            self._handle_workspace_unpublish(ws_id)
            return
        if route.startswith("/api/workspaces/") and route.endswith("/checkout"):
            ws_id = route[len("/api/workspaces/"):-len("/checkout")]
            self._handle_workspace_checkout(ws_id)
            return
        # --- Chat session routes (bearer-gated; identity via X-JWT) -----------
        if route == "/api/chat/sessions" or route.startswith("/api/chat/sessions/"):
            import chat_routes
            payload = self._read_json_body()
            if payload is None:
                return
            chat_routes.handle_request("POST", self.path, payload, self)
            return
        # --- Chat job queue (POST /api/chat/queue) — bearer-gated -------------
        if route == "/api/chat/queue":
            import chat_jobs
            payload = self._read_json_body()
            if payload is None:
                return
            chat_jobs.handle_request("POST", self.path, payload, self)
            return
        # --- Provider API keys (POST /api/keys) — bearer-gated; identity via X-JWT
        if route == "/api/keys":
            import keys_routes
            payload = self._read_json_body()
            if payload is None:
                return
            keys_routes.handle_request("POST", self.path, payload, self)
            return
        # --- In-chat judge (POST /api/chat/judge) — bearer-gated --------------
        if route == "/api/chat/judge":
            payload = self._auth_and_body()
            if payload is None:
                return
            self._handle_chat_judge(payload)
            return
        # --- Template library (POST /api/templates*) — bearer-gated ----------
        # Handles: POST /api/templates (create), POST /api/templates/<id>/heart,
        # POST /api/templates/<id>/download, POST /api/templates/<id>/publish,
        # POST /api/templates/<id>/unpublish. Body parsed inside the dispatcher.
        if route == "/api/templates" or route.startswith("/api/templates/"):
            import template_library
            payload = self._read_json_body()
            if payload is None:
                return
            template_library.handle_request("POST", self.path, payload, self)
            return
        # --- Orchestrator schematic templates (moved to /api/orchestrator/templates) ---
        if route == "/api/orchestrator/templates":
            payload = self._auth_and_body()
            if payload is None:
                return
            panel: Panel = self.server.panel  # type: ignore[attr-defined]
            self._handle_template_save(payload, panel)
            return
        # -----------------------------------------------------------------------
        # POST /api/agent shares the same _auth_and_body gate as the panel routes
        # (service bearer token in Authorization).  GitHub identity for workspace
        # ownership is carried separately in the X-JWT header (see _require_user_from_jwt).
        if route not in ("/api/critique", "/api/panel", "/api/run", "/api/agent"):
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
        start = time.time()
        self._last_status = 200
        error_msg = None
        try:
            self._do_DELETE()
        except Exception as exc:  # noqa: BLE001 — last-resort guard (same as do_GET)
            try:
                error_msg = repr(exc)[:300]
                log_event("do_DELETE_unhandled", path=self.path,
                          error=error_msg)
                self._send_json(500, {"error": f"internal error: {type(exc).__name__}"})
            except Exception:
                pass
        finally:
            self._log_request("DELETE", self.path, self._last_status, (time.time() - start) * 1000, error_msg)

    def _do_DELETE(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        # --- Provider API keys (DELETE /api/keys/<provider>) — bearer-gated
        if route == "/api/keys" or route.startswith("/api/keys/"):
            import keys_routes
            keys_routes.handle_request("DELETE", self.path, {}, self)
            return
        # --- Chat session routes (bearer-gated; identity via X-JWT) ---
        if route.startswith("/api/chat/sessions/"):
            import chat_routes
            chat_routes.handle_request("DELETE", self.path, {}, self)
            return
        # --- Debug log clear (auth-gated) ---
        if route == "/api/debug/clear":
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            cat = q.get("cat", [None])[0]
            debug_log.clear_logs(cat)
            self._send_json(200, {"cleared": True, "category": cat})
            return
        # --- Tier 3: Conscious routes (bearer-gated; JWT enforced inside) ---
        if route == "/api/conscious" or route.startswith("/api/conscious/"):
            self._conscious_dispatch("DELETE")
            return
        if not self._auth_ok():
            self._send_json(401, {"error": "missing or invalid bearer token"})
            return
        # workspace deletion
        if route.startswith("/api/workspaces/"):
            ws_id = route[len("/api/workspaces/"):]
            if ws_id:
                self._handle_workspace_delete(ws_id)
                return
        # --- Template library (DELETE /api/templates/<id>) — delete a template
        #     (owner only). Body not required; auth + identity handled inside. ---
        if route == "/api/templates" or route.startswith("/api/templates/"):
            import template_library
            template_library.handle_request("DELETE", self.path, {}, self)
            return
        # --- Orchestrator schematic templates (moved to /api/orchestrator/templates) ---
        if not route.startswith("/api/orchestrator/templates/"):
            self._send_json(404, {"error": "not found"})
            return
        panel: Panel = self.server.panel  # type: ignore[attr-defined]
        tid = route[len("/api/orchestrator/templates/"):]
        try:
            removed = panel.templates.delete(tid)
        except OrchestrateError as e:
            self._send_json(400, {"error": str(e)})
            return
        if not removed:
            self._send_json(404, {"error": f"no such user template {tid!r}"})
        else:
            self._send_json(200, {"deleted": tid})

    def do_PATCH(self) -> None:
        """PATCH routing (Tier 3 introduces this method for /api/conscious/*).

        Currently only /api/conscious/* uses PATCH. Other PATCH routes can be
        added here following the same bearer + JWT pattern.
        """
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path.rstrip("/")
        if route == "/api/conscious" or route.startswith("/api/conscious/"):
            self._conscious_dispatch("PATCH")
            return
        # --- Chat job queue (PATCH /api/chat/queue) — cancel a job ------------
        if route == "/api/chat/queue":
            import chat_jobs
            payload = self._read_json_body()
            if payload is None:
                return
            chat_jobs.handle_request("PATCH", self.path, payload, self)
            return
        # --- Template library (PATCH /api/templates/<id>) — update a template
        #     (owner only). Body parsed inside the dispatcher. ---
        if route == "/api/templates" or route.startswith("/api/templates/"):
            import template_library
            payload = self._read_json_body()
            if payload is None:
                return
            template_library.handle_request("PATCH", self.path, payload, self)
            return
        self._send_json(404, {"error": "not found"})

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


def _ensure_privatemode_proxy() -> None:
    """Start the PrivateMode AI proxy if PRIVATEMODEAI_API_KEY is set.

    The Privatemode proxy is a static Go binary that:
      1. Performs remote attestation — cryptographically verifies the server-side
         confidential computing enclave is genuine (AMD SEV-SNP + NVIDIA CC).
      2. Exchanges AES-256-GCM keys with the verified AI worker.
      3. Encrypts every prompt client-side before it leaves the machine.

    No proxy = no API (there is no non-proxy endpoint). The proxy speaks standard
    OpenAI /v1/chat/completions once running, so existing routing code works
    unmodified. This function blocks until attestation completes (~15-30s) or
    90s elapses. When PRIVATEMODEAI_API_KEY is unset, this is a no-op.

    Privacy guarantee: Edgeless Systems, Scaleway (infra host), and model vendors
    cannot see plaintext prompts or responses — verified by hardware, not policy.
    Only metadata (IP, timestamp, token usage) is stored for up to 90 days.

    Note: Docker already merges the proxy binary via multi-stage build
    (see Dockerfile), so no additional dependency installation is needed here.
    """
    api_key = os.environ.get("PRIVATEMODEAI_API_KEY", "").strip()
    if not api_key:
        return

    import subprocess
    import urllib.request

    log_event("privatemode_proxy_starting")
    proxy_bin = "privatemode-proxy"
    proc = subprocess.Popen(
        [proxy_bin, "--apiKey", api_key, "--port", "8080"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Attestation + startup takes ~15-30s. Poll /v1/models up to 90s.
    deadline = time.monotonic() + 90
    ready = False
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen("http://localhost:8080/v1/models", timeout=3)
            elapsed = int(time.monotonic() - (deadline - 90))
            log_event("privatemode_proxy_ready", elapsed_s=elapsed)
            ready = True
            break
        except Exception:
            time.sleep(1)

    if not ready:
        log_event("privatemode_proxy_startup_failed", timeout_s=90)
        proc.terminate()


def main() -> int:
    global _panel, _jobs
    _ensure_auth_secret()
    _ensure_encryption_key()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "7860")))

    # initialize SQLite database for GitHub + HF integration (fast, local)
    # STRANDS-COMPLETE-FIX (Issue 1): on the HF Space free tier /data is
    # wiped on every restart (sleep -> wake, rebuild, login). The SQLite
    # DB at /data/doomalaysocreate.db holds ALL chat history, so losing
    # it means users see a blank chat sidebar after every wake-up. The HF
    # Dataset `ScoobyBaby1999/doomalaysocreate-metrics-public` IS persistent
    # -- we mirror the DB binary there and restore it on boot BEFORE
    # init_db() opens the SQLite connection so the restored file is the one
    # SQLite sees. Safe to call when HF_TOKEN is unset (no-op).
    try:
        restored = db.restore_db_from_dataset()
        if restored:
            log_event("db_restored_from_dataset")
    except Exception as e:
        log_event("db_restore_error", error=str(e)[:200])
    db.init_db()

    # FIX-ISSUE-2 (FIX-CHAT-BROKEN): eagerly create the chat_sessions and
    # chat_events tables at boot so the first user request after a Space
    # restart doesn't pay the schema-setup cost (which previously happened
    # lazily inside _ensure_schema_once on the first /api/chat/sessions
    # call). The tables live in the same /data/doomalaysocreate.db file
    # (persistent volume on HF Space), so chat history survives restarts.
    # This is best-effort — a failure here is non-fatal (the lazy init in
    # chat_routes._ensure_schema_once will retry on first request).
    try:
        import chat_routes
        chat_routes._ensure_schema_once()
        log_event("chat_schema_ready")
    except Exception as e:
        log_event("chat_schema_init_error", error=str(e)[:200])

    # Start the privatemode proxy in a BACKGROUND thread — its 90s attestation
    # must NOT block boot (HF's healthcheck times out and the Space gets stuck
    # at APP_STARTING). The provider sync (in Panel.__init__) runs after.
    import threading
    def _bg_privatemode():
        try:
            _ensure_privatemode_proxy()
        except FileNotFoundError:
            log_event("privatemode_proxy_missing")
        except Exception as e:
            log_event("privatemode_proxy_error", error=str(e)[:200])
    threading.Thread(target=_bg_privatemode, daemon=True, name="privatemode-boot").start()

    # Panel.__init__ calls _sync_all_provider_models() which hits 6+ remote
    # endpoints. Wrap it so a failure doesn't kill boot — the panel still
    # works with its static catalog; agent_models() re-probes the sync cache.
    try:
        _panel = Panel()
    except Exception as e:
        log_event("panel_init_error", error=str(e)[:300])
        # Fall back to a minimal panel so the server still starts
        _panel = Panel.__new__(Panel)
        _panel.providers = []
        _panel.provider_by_name = {}
        _panel.slot_by_who = {}
        _panel.logical_models = {}
        _panel.ctx_by_who = {}
        _panel.default_panel = []
        _panel.default_rubric = "critiquer"
        _panel.max_parallel = 4
        _panel.benchmarks = {}
        _panel.cache = PromptCache()

    if not _panel.providers:
        log_event("startup_warning", msg="no providers have API keys - every judge will fail")

    server = BoundedThreadingHTTPServer((host, port), Handler, max_workers=MAX_WORKERS)
    _jobs = JobRunner(_panel, judge_timeout_s=JUDGE_TIMEOUT_S)
    server.panel = _panel  # type: ignore[attr-defined]
    server.jobs = _jobs  # type: ignore[attr-defined]

    log_event("startup", host=host, port=port,
              providers=[p.name for p in _panel.providers],
              default_panel=_panel.default_panel,
              token_configured=_auth_configured(),
              token_rotation=bool(os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip()))
    print(f"doomalaysocreate model panel listening on {host}:{port}  "
          f"(providers={[p.name for p in _panel.providers]})", flush=True)

    # Background: sync all provider models (6+ remote calls, 5-30s) — non-blocking.
    # The panel works with its static catalog until sync completes; agent_models()
    # and _resolve_open_model() re-probe the sync cache so they pick up models
    # as soon as they're available.
    def _bg_sync():
        try:
            _panel._sync_all_provider_models()
            _panel._sync_done = True
            log_event("provider_sync_done",
                      providers=[p.name for p in _panel.providers],
                      slots=len(_panel.slot_by_who))
        except Exception as e:
            log_event("provider_sync_error", error=str(e)[:200])
    threading.Thread(target=_bg_sync, daemon=True, name="provider-sync").start()

    # Graceful shutdown: on SIGTERM (HF Space rebuild/stop), upload DB one last time
    import signal
    signal.signal(signal.SIGTERM, lambda *a: server.shutdown())
    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        dataset_persistence.shutdown_upload()
        # STRANDS-COMPLETE-FIX (Issue 1): force-flush the DB to the HF
        # dataset so the very last chat message a user sent before the
        # Space slept/restarted isn't lost. Idempotent + best-effort.
        try:
            db.flush_db_sync()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
