from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path

from content.roles import Roles

# providers and slots: top-level registry of llm endpoints and the (provider, model)
# pairs they expose. each slot carries which roles it's allowed to play.
#
# ported from loom's backend/providers.py and trimmed for the standalone critique
# service. gpt-oss models are intentionally excluded (user preference, see HANDOFF
# section 4). panel.json is the source of truth for which models are *used* as
# judges - any provider/model named there is registered on the fly against that
# provider's key, even if it isn't listed below (see critique_service.ensure_panel_slots).


@dataclass(frozen=True)
class provider:
    name: str
    url: str
    api_key: str
    models: tuple[str, ...]
    rpm: int = 30                                       # rate per minute
    extra_headers: dict = field(default_factory=dict)   # some providers want custom headers
    note: str = ""


@dataclass(frozen=True)
class slot:
    provider: provider
    model: str
    model_family: str
    roles: tuple[Roles, ...] = (
        Roles("planner"), Roles("parser"),
        Roles("critiquer"), Roles("verifier"),
        Roles("generator"), Roles("transformer"),
    )

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


# every provider we know about. only ones with an api key in env are returned.
# this is the *base catalog* of verified-good models; panel.json may name newer
# ones (e.g. glm-5.1, the largest nemotron) that get registered on the fly.

def make_provider_registry() -> list[provider]:
    load_env()
    providers: list[provider] = []

    # openrouter - shared free quota, easy to 429 across all users.
    tempkey = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="openrouter",
            url="https://openrouter.ai/api/v1/chat/completions",
            api_key=tempkey,
            models=(
                "nousresearch/hermes-3-llama-3.1-405b:free",
                "qwen/qwen3-coder:free",
                "qwen/qwen3-next-80b-a3b-instruct:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "z-ai/glm-4.5-air:free",
                "google/gemma-3-27b-it:free",
            ),
            rpm=20,
            extra_headers={
                "HTTP-Referer": "https://huggingface.co/spaces",
                "X-Title": "loom critique panel",
            },
            note="mostly shared quota, once it 429's it stays capped for a long time.",
        ))

    # cerebras - very fast, capped at ~1m tokens/day.
    tempkey = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="cerebras",
            url="https://api.cerebras.ai/v1/chat/completions",
            api_key=tempkey,
            models=(
                "qwen-3-235b-a22b-instruct-2507",
                "llama3.1-8b",
            ),
            rpm=40,
            note=">2000 tokens/sec, 1M tokens/day cap.",
        ))

    # groq - fastest provider, low daily budget.
    tempkey = os.environ.get("GROQ_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            api_key=tempkey,
            models=(
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "qwen/qwen3-32b",
            ),
            rpm=30,
            note="30 rpm, ~14.4k req/day. fastest provider.",
        ))

    # nvidia nim - openai-compat, free credits, generous rpm.
    tempkey = os.environ.get("NVIDIA_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="nvidia",
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            api_key=tempkey,
            models=(
                "meta/llama-3.3-70b-instruct",
                "nvidia/llama-3.1-nemotron-ultra-253b-v1",
                "nvidia/llama-3.3-nemotron-super-49b-v1",
                "deepseek-ai/deepseek-r1",
                "meta/llama-4-maverick-17b-128e-instruct",
                "qwen/qwen2.5-coder-32b-instruct",
            ),
            rpm=40,
            note="free credits, generous rpm, broad model lineup.",
        ))

    # z.ai (zhipu) - native GLM family, OpenAI-compatible endpoint. lets us use the
    # latest GLM (e.g. glm-5.1) on z.ai's own free allotment instead of paying for
    # it on OpenRouter's passthrough.
    tempkey = os.environ.get("ZAI_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="zai",
            url="https://api.z.ai/api/paas/v4/chat/completions",
            api_key=tempkey,
            models=(
                "glm-5.1",
                "glm-4.7",
                "glm-4.5-air",
            ),
            rpm=30,
            note="native GLM access (z.ai). latest GLM without OpenRouter's paid passthrough.",
        ))

    # moonshot (kimi) - native Kimi access, OpenAI-compatible. signup grants free
    # trial credits; kimi-k2.6 is the current frontier model (apr 2026).
    tempkey = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="moonshot",
            url="https://api.moonshot.ai/v1/chat/completions",
            api_key=tempkey,
            models=(
                "kimi-k2.6",
                "kimi-k2.5",
                "kimi-k2-0905-preview",
            ),
            rpm=30,
            note="native Kimi (Moonshot). frontier model, OpenAI-compatible.",
        ))

    # google gemini - OpenAI-compatible endpoint, free tier via AI Studio. modest
    # rpm but fine for an occasional judge panel. not in the default panel, but
    # registered so it can be baked off via a panel override.
    tempkey = os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if tempkey:
        providers.append(provider(
            name="google",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            api_key=tempkey,
            models=(
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-2.0-flash",
            ),
            rpm=15,
            note="gemini via openai-compat endpoint. free tier, modest rpm.",
        ))

    return providers


def make_slot_registry(providers: list[provider]) -> list[slot]:
    #   one slot per (provider, model). family + role tuple are derived once here.
    slots: list[slot] = []
    for p in providers:
        for model in p.models:
            slots.append(slot(
                provider=p,
                model=model,
                model_family=make_model_family(model),
                roles=make_model_role(model),
            ))
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
