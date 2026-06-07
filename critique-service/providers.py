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
# ported from loom's backend/providers.py and trimmed for the standalone critique
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
    return _load_json_commented(MODELS_CATALOG_PATH).get("logical_models", {})


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
        limits = entry.get("limits", {}) or {}
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
