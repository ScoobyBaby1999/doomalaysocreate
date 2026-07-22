from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from content.roles import Roles
from oplog import log_event

# providers and slots: top-level registry of llm endpoints and the (provider, model)
# pairs they expose. each slot carries which roles it's allowed to play.
#
# ported from doomalaysocreate's backend/providers.py and trimmed for the standalone critique
# service. gpt-oss models are intentionally excluded (user preference, see HANDOFF
# section 4). panel.json is the source of truth for which models are *used* as
# judges - any provider/model named there is registered on the fly against that
# provider's key, even if it isn't listed below (see critique_service.ensure_panel_slots).


# every panel slot is allowed to play every role. the critique service calls
# judges directly and does NOT use pick_slot's role-eligibility to gate them
# (a frontier "generator" model is still a perfectly good "critiquer" here), so
# slots are built with the full role set and rotation/failover never blocks on role.
FULL_ROLES: tuple[Roles, ...] = (
    Roles("planner"), Roles("parser"),
    Roles("critiquer"), Roles("verifier"),
    Roles("generator"), Roles("transformer"),
    Roles("reviewer"), Roles("extractor"),
)


@dataclass(frozen=True)
class provider:
    name: str
    url: str
    api_key: str
    models: tuple[str, ...]
    rpm: int = 30                                       # rate per minute
    extra_headers: dict = field(default_factory=dict)   # some providers want custom headers
    note: str = ""
    pool: str = "core"                                  # "core" | "optin"
    region: str = ""
    rpd: int | None = None                              # requests/day (published free-tier ceiling)
    tpm: int | None = None                              # tokens/minute
    tpd: int | None = None                              # tokens/day
    concurrency: int | None = None
    monthly_credit: str = ""
    max_out: int | None = None                          # hard per-request output-token ceiling (None = uncapped)
    #   privacy posture (see providers_catalog.json -> "privacy" + docs/PRIVACY-RESEARCH.md).
    #   trains_on_data: True | False | "per-model" | "unknown". UNKNOWN is treated as
    #   UNSAFE by the privacy router (fail-safe for a privacy-first product).
    trains_on_data: object = "unknown"
    privacy_optout_url: str = ""
    stability_tier: int = 0                             # 1=most stable host ... higher=flakier

    def __repr__(self) -> str:
        #   NEVER expose api_key via repr/str: oplog's JSON fallback reprs unknown
        #   objects, so an accidental log_event(provider=...) must not leak the key.
        return (f"provider(name={self.name!r}, models={len(self.models)}, "
                f"api_key=***{'set' if self.api_key else 'unset'}***)")


def slot_is_privacy_safe(s: "slot") -> bool:
    #   privacy-safe = the host is verified NOT to train on / retain submissions.
    #   "per-model" (OpenRouter): a ':free' route REQUIRES logging/training consent,
    #   so only non-':free' routes are safe. unknown/None posture -> NOT safe.
    t = s.provider.trains_on_data
    if t == "per-model":
        return not s.model.strip().lower().endswith(":free")
    if isinstance(t, bool):
        return not t
    return False


@dataclass(frozen=True)
class slot:
    provider: provider
    model: str
    model_family: str
    roles: tuple[Roles, ...] = FULL_ROLES

    @property
    def who(self) -> str:
        return f"{self.provider.name}/{self.model}"


def load_env() -> None:
    #   tiny .env loader. format: KEY=value per line. only sets keys not already set.
    #   on hugging face the real keys arrive as process env (Space secrets) so this
    #   is a no-op there; it only matters for local dev with a .env file.
    envfile = Path(__file__).resolve().parent / ".env"
    if not envfile.exists():
        return
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# normalize model strings to a 'family' so multiple providers serving the same
# weights (qwen-3-32b on cerebras vs qwen/qwen3-32b on groq) share one stats bucket.
LLM_suffixes = (
    ":free", "-instruct-2507", "-instruct-fp8-fast", "-instruct-fast",
    "-fp8-fast", "-instruct", "-versatile", "-it", "-chat", "-preview",
)
LLM_prefixes = (
    "@cf/meta/", "@cf/", "openai/", "google/", "meta-llama/",
    "nousresearch/", "arcee-ai/", "qwen/", "z-ai/", "nvidia/",
    "minimax/", "openrouter/", "meta/", "deepseek-ai/",
)


def make_model_family(models: str) -> str:
    model = models.lower().strip()
    #   strip a known prefix once if present.
    for pfx in sorted(LLM_prefixes, key=len, reverse=True):
        if model.startswith(pfx):
            model = model[len(pfx):]
            break
    #   strip every recognised suffix - some models have multiple stacked.
    loop = True
    while loop:
        loop = False
        for sfx in sorted(LLM_suffixes, key=len, reverse=True):
            if model.endswith(sfx):
                model = model[:-len(sfx)]
                loop = True
                break
    return model


def make_model_role(models: str) -> tuple[Roles, ...]:
    #   route based on parameter count tags in the model name.
    #   NOTE: the critique service calls panel slots *directly* (it does not use
    #   pick_slot's role eligibility), so this routing does not gate the judges.
    #   it is kept so the ported scheduler stays drop-in compatible.
    model = models.lower()
    if any(tag in model for tag in ("120b", "200b", "235b", "253b", "405b", "480b", "671b")):
        return (Roles("generator"), Roles("transformer"), Roles("planner"))
    if any(tag in model for tag in ("70b", "72b", "80b", "4.5", "air")):
        return (
            Roles("planner"), Roles("parser"),
            Roles("critiquer"), Roles("verifier"),
            Roles("generator"), Roles("transformer"),
        )
    if any(tag in model for tag in ("8b", "17b", "20b", "26b", "27b", "30b", "31b", "32b", "flash", "nano")):
        return (Roles("parser"), Roles("verifier"), Roles("critiquer"))
    #       unknown size -> trust it on every role.
    return (
        Roles("planner"), Roles("parser"),
        Roles("critiquer"), Roles("verifier"),
        Roles("generator"), Roles("transformer"),
    )


# --- catalog loaders --------------------------------------------------------
# providers_catalog.json is the single source of truth for hosts + their free-tier
# limits; models_catalog.json maps logical names to ordered (provider, model)
# candidates. both are tolerant of // line comments so they can stay annotated.

HERE = Path(__file__).resolve().parent
PROVIDERS_CATALOG_PATH = Path(os.environ.get("PROVIDERS_CATALOG", HERE / "providers_catalog.json"))
MODELS_CATALOG_PATH = Path(os.environ.get("MODELS_CATALOG", HERE / "models_catalog.json"))


def _load_json_commented(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    no_comments = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("//")
    )
    return json.loads(no_comments)


def load_provider_catalog() -> list[dict]:
    return _load_json_commented(PROVIDERS_CATALOG_PATH).get("providers", [])


def load_models_catalog() -> dict[str, dict]:
    raw = _load_json_commented(MODELS_CATALOG_PATH).get("logical_models", {})
    #   keep only real logical-model entries: a dict with candidates. tolerates any
    #   "//..." section-marker keys or stray string values in the JSON.
    return {k: v for k, v in raw.items()
            if not k.startswith("//") and isinstance(v, dict) and v.get("candidates")}


BENCHMARKS_PATH = Path(os.environ.get("BENCHMARKS_CATALOG", HERE / "benchmarks.json"))


def load_benchmarks() -> dict[str, dict]:
    #   logical-model -> {arena_elo, aa_index, frontier, source, as_of}. hand-curated
    #   and DATED; indicative quality signal for the roster, not an authoritative
    #   ranking. tolerant: a missing/garbled file just means an empty roster signal.
    try:
        raw = _load_json_commented(BENCHMARKS_PATH).get("benchmarks", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("//") and isinstance(v, dict)}


REASONING_CATALOG_PATH = Path(os.environ.get("REASONING_CATALOG", HERE / "reasoning_catalog.json"))


def load_reasoning_catalog() -> dict[str, dict]:
    #   per-model thinking-mode adapter: model key -> {"body": {...}}. tolerant of
    #   comment keys. absent file -> empty (everything uses token budget only).
    try:
        raw = _load_json_commented(REASONING_CATALOG_PATH).get("reasoning", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("//") and isinstance(v, dict)}


def resolve_reasoning_entry(catalog: dict[str, dict], *, logical: str | None,
                            who: str | None, family: str | None) -> dict:
    #   pick the most specific catalog entry: provider/model -> logical -> family -> '*'.
    #   the entry carries "body" (thinking params) and optional "quirks" (per-model
    #   transport adaptations, e.g. system_in_user for hosts that strip system prompts).
    for key in (who, logical, family, "*"):
        if key and key in catalog:
            return catalog[key]
    return {}


def resolve_reasoning_body(catalog: dict[str, dict], *, logical: str | None,
                           who: str | None, family: str | None) -> dict:
    body = resolve_reasoning_entry(catalog, logical=logical, who=who, family=family).get("body")
    return dict(body) if isinstance(body, dict) else {}


def resolve_quirks(catalog: dict[str, dict], *, logical: str | None,
                   who: str | None, family: str | None) -> dict:
    quirks = resolve_reasoning_entry(catalog, logical=logical, who=who, family=family).get("quirks")
    return dict(quirks) if isinstance(quirks, dict) else {}


def resolve_effort_levels(catalog: dict[str, dict], *, logical: str | None,
                          who: str | None, family: str | None) -> list[str]:
    """Return the ordered list of effort level names this (provider, model)
    supports on its host, e.g. ``["low", "medium", "high"]`` or
    ``["none", "minimal", "low", "medium", "high", "xhigh", "max"]``.

    Empty list ``[]`` means the model has no reasoning/effort parameter
    (either it doesn't reason at all, or it reasons natively with no
    configurable level — phi-4-reasoning, deepseek-r1).

    The frontend uses this to:
      * show/hide the effort-mode button (hidden when ``effort_levels == []``)
      * populate the effort-mode dropdown with the model's canonical names
        (some models have 3 levels, some have 7, with different names).

    Resolution order: exact 'provider/model' -> logical -> family -> '*'
    (same as resolve_reasoning_entry). The '*' default has ``effort_levels=[]``.
    """
    entry = resolve_reasoning_entry(catalog, logical=logical, who=who, family=family)
    levels = entry.get("effort_levels")
    if not isinstance(levels, list):
        return []
    # Coerce to list[str], drop empties, preserve order + dedupe.
    out: list[str] = []
    seen: set[str] = set()
    for lvl in levels:
        s = str(lvl).strip() if lvl is not None else ""
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out



# --- web-search catalog (provider-native web search) -------------------------
# Same shape as the reasoning catalog, but for the per-provider "do they have a
# NATIVE web-search tool?" question. Lives under the "web_search" key in
# reasoning_catalog.json so all provider-native tooling lives in one file.

def load_web_search_catalog() -> dict[str, dict]:
    """Per-(provider, model) native web-search adapter. Returns the
    ``web_search`` section of reasoning_catalog.json. Keys are catalog keys
    like 'openrouter/*' or 'openrouter/moonshotai/kimi-k2.6:free'; values
    are dicts with 'native' (bool) and 'body' (the request-body fields).
    """
    try:
        raw = _load_json_commented(REASONING_CATALOG_PATH).get("web_search", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items()
            if not k.startswith("//") and isinstance(v, dict)}


def _ws_candidate_keys(provider: str, model: str) -> list[str]:
    """Build the ordered list of catalog keys to look up for a (provider, model).
    Resolution order: provider/model -> provider/<last-segment> -> provider/* -> *.
    """
    p = (provider or "").strip().lower().replace("_", "-").replace(" ", "")
    m = (model or "").strip()
    out: list[str] = []
    if p and m:
        out.append(f"{p}/{m}")
    if m:
        last = m.split("/")[-1]
        if last and last != m:
            out.append(f"{p}/{last}")
    if p:
        out.append(f"{p}/*")
    out.append("*")
    seen: set[str] = set()
    deduped: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


def resolve_web_search_entry(catalog: dict[str, dict], *,
                             provider: str | None, model: str | None) -> dict:
    """Pick the most-specific web_search catalog entry for this (provider, model).

    Returns the entry dict (which has 'native' + 'body' + optional 'note'),
    or {} when nothing matches. The catch-all '*' entry is only used when no
    more-specific entry exists (so a host-level 'openrouter/*' overrides '*').
    """
    keys = _ws_candidate_keys(provider or "", model or "")
    for key in keys:
        if key and key in catalog:
            # The '*' entry is the fall-through; only return it if it's the
            # last key in the resolution order (no host-level entry existed).
            if key == "*":
                continue
            entry = catalog.get(key)
            if isinstance(entry, dict):
                return entry
    star = catalog.get("*") or {}
    return star if isinstance(star, dict) else {}


def resolve_web_search_body(catalog: dict[str, dict], *,
                            provider: str | None, model: str | None) -> dict:
    """Return the request-body fields to enable NATIVE web search for this
    (provider, model), or {} when native web search isn't supported."""
    entry = resolve_web_search_entry(catalog, provider=provider, model=model)
    if not entry.get("native"):
        return {}
    body = entry.get("body")
    return dict(body) if isinstance(body, dict) else {}


def supports_native_web_search(catalog: dict[str, dict], *,
                               provider: str | None, model: str | None) -> bool:
    """True iff this (provider, model) has a NATIVE web-search tool."""
    return bool(resolve_web_search_entry(catalog, provider=provider, model=model)
                .get("native", False))


def _bench_lower(bench: dict | None) -> str:
    """Lowercase string of benchmark tags + note for capability matching."""
    if not isinstance(bench, dict):
        return ""
    return " ".join(str(bench.get(k, "") or "").lower()
                    for k in ("tags", "note"))


def get_model_capabilities(provider_name: str | None, model_id: str | None, *,
                           logical: str | None = None,
                           family: str | None = None,
                           benchmarks: dict | None = None,
                           reasoning_catalog: dict | None = None,
                           web_search_catalog: dict | None = None) -> dict:
    """Return what this (provider, model) actually supports on its host.

    This is the AUTHORITATIVE capability detector used by the roster endpoint
    (so the frontend shows accurate per-(provider, model) capability badges
    instead of "always True for web search"). It cross-references:

      - reasoning_catalog.json 'reasoning' section  -> effort support + param name
      - reasoning_catalog.json 'web_search' section -> native web search support
      - benchmarks.json tags/note                   -> tools / vision tags

    Returns:
        {
            'effort': bool,                # supports a reasoning/thinking param
            'effort_param': str|None,      # the param name (e.g. 'reasoning_effort')
            'web_search': bool,            # supports native web-search tool
            'web_search_native': bool,     # alias of web_search (clarity)
            'tools': bool,                 # supports function/tool calling
            'vision': bool,                # supports image input
        }
    """
    rcat = reasoning_catalog if reasoning_catalog is not None else load_reasoning_catalog()
    wscat = web_search_catalog if web_search_catalog is not None else load_web_search_catalog()

    # Normalize provider name (catalog uses dashes, env vars use underscores).
    p = (provider_name or "").strip().lower().replace("_", "-").replace(" ", "")
    m = (model_id or "").strip()
    who = f"{p}/{m}" if p and m else None
    if family is None and m:
        family = make_model_family(m)

    rbody = resolve_reasoning_body(rcat, logical=logical, who=who, family=family)
    if rbody:
        if "reasoning" in rbody:
            effort_param = "reasoning"
        elif "reasoning_effort" in rbody:
            effort_param = "reasoning_effort"
        elif "chat_template_kwargs" in rbody:
            effort_param = "chat_template_kwargs"
        else:
            effort_param = next(iter(rbody.keys()), None)
    else:
        effort_param = None

    ws_native = supports_native_web_search(wscat, provider=p, model=m)

    # Tools / vision: derive from benchmarks tags + catalog notes.
    bench = benchmarks or {}
    bench_lower = _bench_lower(bench)
    full = (bench_lower + " " + (logical or "").lower()
            + " " + (model_id or "").lower()
            + " " + (provider_name or "").lower())
    has_vision = any(t in full for t in ("vision", "image", "multimodal"))
    has_tools = any(t in full for t in ("tool", "function", "agentic"))

    # Host-level guarantees: OpenRouter exposes OpenAI-style tools on every
    # chat-completions model; CF Workers AI chat-completions exposes tools
    # when the model's tag says "Function calling". We're conservative: only
    # flip has_tools when we have positive evidence.
    if p == "openrouter":
        has_tools = True
    if p == "cloudflare" and m.startswith("@cf/"):
        if "function calling" in full or "tool" in full:
            has_tools = True

    # Effort levels: the ordered list of effort level names this
    # (provider, model) supports on its host. Empty list = no effort param
    # (model either doesn't reason, or reasons natively with no knob).
    # The frontend uses this to show/hide the effort button AND to render
    # the correct variant names (some models have 3 levels, some have 7,
    # with different names like 'low/mid/ultra' vs 'none/minimal/low/.../max').
    effort_levels = resolve_effort_levels(rcat, logical=logical, who=who, family=family)

    return {
        "effort": bool(rbody),
        "effort_param": effort_param,
        "effort_levels": effort_levels,
        "web_search": ws_native,
        "web_search_native": ws_native,
        "tools": has_tools,
        "vision": has_vision,
    }



def _first_env(env_var) -> str:
    #   env_var may be a single name or a list (first present wins).
    names = [env_var] if isinstance(env_var, str) else list(env_var or [])
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _opted_in() -> set[str]:
    raw = os.environ.get("OPTIN_PROVIDERS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def make_provider_registry() -> list[provider]:
    #   build the live provider list from providers_catalog.json. a provider is
    #   registered only when its key (and any 'requires' vars) are present, and it
    #   is either pool=="core" or explicitly opted in via OPTIN_PROVIDERS.
    load_env()
    opted_in = _opted_in()
    providers: list[provider] = []
    for entry in load_provider_catalog():
        name = entry["name"]
        pool = entry.get("pool", "core")
        if pool != "core" and name not in opted_in:
            continue
        api_key = _first_env(entry.get("env_var"))
        if not api_key:
            continue
        #       extra required env vars (e.g. CF_ACCOUNT_ID) must all be present.
        requires = entry.get("requires", [])
        missing = [v for v in requires if not os.environ.get(v, "").strip()]
        if missing:
            log_event("provider_skipped", provider=name, reason=f"missing {missing}")
            continue
        #       fill {VAR} placeholders in the base_url from the environment.
        url = entry["base_url"]
        for var in requires:
            url = url.replace("{" + var + "}", os.environ.get(var, "").strip())
        #       Log resolved URL (masked) so we can diagnose Cloudflare ConnectError
        masked_url = url.replace(api_key, "***API_KEY***") if api_key else url
        log_event("provider_url_resolved", provider=name, url=masked_url[:120])
        limits = entry.get("limits", {}) or {}
        privacy = entry.get("privacy", {}) or {}
        providers.append(provider(
            name=name,
            url=url,
            api_key=api_key,
            models=tuple(entry.get("models", [])),
            rpm=int(limits.get("rpm") or 30),
            extra_headers=dict(entry.get("extra_headers", {})),
            note=entry.get("note", ""),
            pool=pool,
            region=entry.get("region", ""),
            rpd=limits.get("rpd"),
            tpm=limits.get("tpm"),
            tpd=limits.get("tpd"),
            concurrency=limits.get("concurrency"),
            monthly_credit=limits.get("monthly_credit", ""),
            max_out=limits.get("max_out"),
            trains_on_data=privacy.get("trains_on_data", "unknown"),
            privacy_optout_url=privacy.get("optout_url", ""),
            stability_tier=int(privacy.get("stability_tier") or 0),
        ))
    return providers


def make_slot(prov: provider, model: str) -> slot:
    #   one slot, full role set (no role-gating in this service).
    return slot(provider=prov, model=model, model_family=make_model_family(model))


def make_slot_registry(providers: list[provider]) -> list[slot]:
    #   one slot per (provider, model), each with the full role set.
    slots: list[slot] = []
    for p in providers:
        for model in p.models:
            slots.append(make_slot(p, model))
    return slots


def print_provider_registry(providers: list[provider]) -> str:
    if not providers:
        return "!!! no registered providers, AI will not work."
    lines = []
    for p in providers:
        lines.append(f"- {p.name}: {len(p.models)} model(s), rpm={p.rpm}")
        lines.append(f"    url: {p.url}")
        if p.note:
            lines.append(f"    note: {p.note}")
    return "\n".join(lines)


def print_slot_registry(slots: list[slot]) -> str:
    lines = []
    for s in slots:
        roles = ",".join(r.value for r in s.roles)
        lines.append(f"  {s.who:60} family={s.model_family:25} roles=[{roles}]")
    return "\n".join(lines)


def main() -> int:
    providers = make_provider_registry()
    print(f"providers: {len(providers)}")
    print(print_provider_registry(providers))
    slots = make_slot_registry(providers)
    print(f"slots: {len(slots)}")
    print(print_slot_registry(slots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
