#!/usr/bin/env python3
"""
providers.py - Registry of LLM API providers the pipeline can reach.

# What this module is for

The orchestrator's scheduler picks a (provider, model) "slot" for every
LLM call. This module defines the universe of slots — which providers we
have keys for, which models each one serves, what their RPM caps are,
and which roles each model is good at.

Phase A registers the **core 4 providers**: OpenRouter, Cerebras, Groq,
and Google Gemini. All four expose OpenAI-compatible chat-completions
endpoints, so a single `_call_slot` in the orchestrator handles all of
them via `(provider.base_url, provider.api_key, provider.extra_headers)`.

Phase C providers (Ollama Cloud, OpenRouter#2, Cloudflare, Vercel) are
also registered here as opt-in blocks: each is guarded by `if key:` so
absent env vars silently skip the provider. This means a user with only
3 of 4 core keys in `.env` still gets a working pipeline.

# Concepts

  Provider      One API account at one vendor. Holds base URL, key,
                rate cap, model list, and any vendor-specific quirks.
                Multiple accounts at the same vendor get distinct names
                (e.g. "openrouter", "openrouter_2").

  Slot          One (provider, model) pair. Every Slot also carries a
                `model_family` (canonical model identity, stripped of
                provider prefix and `:free` suffix) and a tuple of
                `roles` it's eligible to serve. The scheduler scores
                bandit success per FAMILY (so wins on llama-3.3-70b at
                Groq count toward the same family as wins at Cerebras)
                but tracks cooldowns/blacklists per SLOT (a 429 on
                Groq's Llama doesn't cool Cerebras's Llama).

# Design notes

- `Provider` is frozen — built once per run, never mutated.
- All current providers use OpenAI-compatible endpoints. The only
  per-provider adapter logic is base_url and extra_headers; the request
  body shape is identical.
- Gemini's path is `/v1beta/openai/...` not `/v1/chat/completions` —
  Google's OpenAI-compat shim. We pre-bake the full URL.
- Cloudflare's URL has `{account_id}` — we substitute in at load time.
- Refusal-phrase detection lives at the call layer, not here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.roles import Role


# ---------- Dataclasses ----------

@dataclass(frozen=True)
class Provider:
    """One API account at one LLM vendor.

    Attributes
    ----------
    name : str
        Short identifier used in logs and by the scheduler's rotation
        rule. Must be unique across the registry.

    base_url : str
        Full URL to the chat-completions endpoint. Pre-baked at load
        time so the call layer never substitutes anything.

    api_key : str
        Loaded from the corresponding env var in `load_registry()`.

    models : tuple[str, ...]
        Model slugs this provider serves. Each becomes one Slot when
        build_slots() flattens the registry.

    rpm : int
        Requests-per-minute cap. The scheduler uses this to enforce
        min_gap_s = 60 / rpm between calls to the same provider.
        Set conservatively below the documented cap to clear burst-
        detection heuristics.

    extra_headers : dict[str, str]
        Vendor-specific headers required on every request. OpenRouter
        wants HTTP-Referer + X-Title for attribution. Most others empty.

    note : str
        Free-text reminder of provider quirks. Printed at startup.
    """
    name: str
    base_url: str
    api_key: str
    models: tuple[str, ...]
    rpm: int = 30
    extra_headers: dict = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class Slot:
    """One (provider, model) pair — the unit the scheduler picks.

    Attributes
    ----------
    provider : Provider
        The carrier. Multiple slots can share a provider (one per model
        the provider serves).

    model : str
        Provider-specific model slug used in the API call. Includes
        any provider prefixes (e.g. `meta-llama/llama-3.3-70b-instruct:free`
        for OpenRouter, `llama-3.3-70b` for Groq).

    model_family : str
        Canonical model identity, stripped of provider prefix and `:free`
        suffix. Used by the scheduler to pool bandit success across all
        carriers serving the same model. Examples:
          openrouter/meta-llama/llama-3.3-70b-instruct:free → "llama-3.3-70b"
          groq/llama-3.3-70b-versatile                      → "llama-3.3-70b"
          cerebras/llama-3.3-70b                            → "llama-3.3-70b"

    roles : tuple[Role, ...]
        Which roles this slot is eligible to serve. Driven by model size:
        big models for generator/transformer (longform), small models for
        reviewer/extractor/verifier (precision over creativity), mid-tier
        spans everything including planner.
    """
    provider: Provider
    model: str
    model_family: str
    roles: tuple[Role, ...] = (
        Role.PLANNER, Role.GENERATOR, Role.REVIEWER,
        Role.TRANSFORMER, Role.EXTRACTOR, Role.VERIFIER,
    )

    @property
    def id(self) -> str:
        """Human-readable slot identifier for logs and scheduler keys."""
        return f"{self.provider.name}/{self.model}"


# ---------- .env loader ----------

def _ensure_env_loaded() -> None:
    """Load .env from the project root into os.environ if not already done."""
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ---------- Family & role inference ----------

# Suffixes we strip when computing model_family from a model slug.
# Order matters: longest-first so "-instruct-2507" gets handled before "-2507".
_FAMILY_SUFFIX_STRIPS = (
    ":free", "-instruct-2507", "-instruct-fp8-fast", "-instruct-fast",
    "-fp8-fast", "-instruct", "-versatile", "-it", "-chat", "-preview",
)
_FAMILY_PREFIX_STRIPS = (
    "@cf/meta/", "@cf/", "openai/", "google/", "meta-llama/",
    "nousresearch/", "arcee-ai/", "qwen/", "z-ai/", "nvidia/",
    "minimax/", "openrouter/", "meta/",
)


def model_family(model_slug: str) -> str:
    """Compute canonical model_family from a provider-specific model slug.

    Strips known prefixes (`openai/`, `meta-llama/`, etc.) and suffixes
    (`:free`, `-instruct`, `-versatile`). Lowercases the result. Designed
    to map equivalent models across carriers to the same family string.

    Examples:
      "openai/gpt-oss-120b:free"                → "gpt-oss-120b"
      "meta-llama/llama-3.3-70b-instruct:free"  → "llama-3.3-70b"
      "llama-3.3-70b-versatile"                 → "llama-3.3-70b"
      "qwen-3-235b-a22b-instruct-2507"          → "qwen-3-235b-a22b"
      "gemini-2.5-flash"                        → "gemini-2.5-flash"
      "@cf/meta/llama-3.3-70b-instruct-fp8-fast" → "llama-3.3-70b"

    The scheduler uses these strings as keys in its FamilyState dict.
    Two models with the same canonical family share bandit score, even
    if they have slightly different provider-specific quirks.
    """
    s = model_slug.lower().strip()
    # Strip prefixes (longest match first).
    for pfx in sorted(_FAMILY_PREFIX_STRIPS, key=len, reverse=True):
        if s.startswith(pfx):
            s = s[len(pfx):]
            break
    # Strip suffixes (longest match first; loop because some compound).
    changed = True
    while changed:
        changed = False
        for sfx in sorted(_FAMILY_SUFFIX_STRIPS, key=len, reverse=True):
            if s.endswith(sfx):
                s = s[:-len(sfx)]
                changed = True
                break
    return s


def _infer_roles(model_slug: str) -> tuple[Role, ...]:
    """Decide which roles a model is eligible for, based on size hints.

    The slug usually contains a parameter-count tag like "70b" or "120b".
    We classify into tiers:

      120B+ tier  → (generator, transformer)
                    Too expensive to waste on critique/extract/verify.
      70-90B tier → (planner, generator, reviewer, transformer,
                     extractor, verifier)
                    The versatile middle. Especially good at planner
                    because they emit reliable JSON.
      <=40B / "flash" / "nano" → (planner, reviewer, extractor, verifier)
                                  Precision tier. Good for JSON-emitting
                                  Planner role; too small for longform.
      Unknown → all six roles, let the scheduler bandit sort it out.

    Per-provider overrides happen in load_registry() by passing an
    explicit `roles=` to the Slot constructor (we don't do that yet,
    but the hook is here).
    """
    m = model_slug.lower()
    # Big-model hints (120B+, 200B, 235B, 405B, 480B, 671B).
    if any(t in m for t in (
        "120b", "200b", "235b", "405b", "480b", "671b",
    )):
        return (Role.GENERATOR, Role.TRANSFORMER)
    # Mid-tier 70-90B hints.
    if any(t in m for t in ("70b", "72b", "80b", "glm-4.5-air")):
        return (
            Role.PLANNER, Role.GENERATOR, Role.REVIEWER,
            Role.TRANSFORMER, Role.EXTRACTOR, Role.VERIFIER,
        )
    # Small precision tier (8B, 17B, 20B-32B, "nano", "flash").
    if any(t in m for t in (
        "8b", "17b", "20b", "26b", "27b", "30b", "31b", "32b",
        "nano", "flash",
    )):
        return (Role.PLANNER, Role.REVIEWER, Role.EXTRACTOR, Role.VERIFIER)
    # Unknown — full eligibility.
    return (
        Role.PLANNER, Role.GENERATOR, Role.REVIEWER,
        Role.TRANSFORMER, Role.EXTRACTOR, Role.VERIFIER,
    )


# ---------- Registry ----------

def load_registry() -> list[Provider]:
    """Build the provider list from environment variables.

    Each provider block reads its env var and appends a Provider only
    if the key is present. Missing keys silently skip — partial `.env`
    files work fine. This is what lets a user opt into Phase C providers
    incrementally just by setting their env vars, no code edit needed.
    """
    _ensure_env_loaded()
    providers: list[Provider] = []

    # --- Phase A core 4 -----------------------------------------------------

    # OpenRouter — the original path. Free tier aggregates across users so
    # popular models 429 fast at peak hours. Quirks:
    #   - Upstream errors arrive as HTTP 200 with {"error": {...}} body.
    #   - Wants HTTP-Referer + X-Title for site attribution.
    #   - Nemotron-3-super-120b reliably 524s; scheduler blacklists on first hit.
    _key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1/chat/completions",
            api_key=_key,
            models=(
                "openai/gpt-oss-120b:free",
                "nousresearch/hermes-3-llama-3.1-405b:free",
                "qwen/qwen3-coder:free",
                "qwen/qwen3-next-80b-a3b-instruct:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "z-ai/glm-4.5-air:free",
                "google/gemma-3-27b-it:free",
                "openai/gpt-oss-20b:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
                "google/gemma-4-31b-it:free",
            ),
            rpm=20,
            extra_headers={
                "HTTP-Referer": "https://github.com/.md/.pied",
                "X-Title": ".md Replay research loop",
            },
            note="Free-tier shared quota; 429s fast.",
        ))

    # Cerebras — ultra-fast (>2000 tok/s), 1M tok/day free cap, 30 RPM.
    # OpenAI-compat at /v1/chat/completions.
    _key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="cerebras",
            base_url="https://api.cerebras.ai/v1/chat/completions",
            api_key=_key,
            models=(
                "gpt-oss-120b",
                "qwen-3-32b",
                "llama-3.3-70b",
                "llama-4-scout-17b-16e-instruct",
            ),
            rpm=30,
            note="Ultra-fast inference (>2000 tok/s). 1M tok/day free cap.",
        ))

    # Groq — labeled "grok" in user's .env but it's actually Groq.
    # 30 RPM, 14.4K req/day free. OpenAI-compat at /openai/v1/chat/completions.
    _key = os.environ.get("GROQ_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="groq",
            base_url="https://api.groq.com/openai/v1/chat/completions",
            api_key=_key,
            models=(
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3-32b",
            ),
            rpm=30,
            note="30 RPM, 14.4K req/day. Fastest llama-3.3-70b carrier.",
        ))

    # Google Gemini — OpenAI-compat shim at /v1beta/openai/.
    # 15 RPM, 1.5M tok/day free for gemini-2.5-flash.
    _key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            api_key=_key,
            models=(
                "gemini-2.5-pro", 
                "gemini-2.5-flash",
                "gemini-2.0-flash",
                "gemini-2.5-flash-8b"
            ),
            rpm=10,
            note="OpenAI-compat shim. Strong JSON output.",
        ))

    # --- Phase C opt-ins (registered when env var present) ------------------

    # OpenRouter account #2 — separate quota, same model zoo.
    _key = os.environ.get("OPENROUTER_API_KEY_2", "").strip()
    if _key:
        providers.append(Provider(
            name="openrouter_2",
            base_url="https://openrouter.ai/api/v1/chat/completions",
            api_key=_key,
            models=(
                "openai/gpt-oss-120b:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "z-ai/glm-4.5-air:free",
            ),
            rpm=20,
            extra_headers={
                "HTTP-Referer": "https://github.com/.md/.pied",
                "X-Title": ".md Replay research loop",
            },
            note="Second OpenRouter account; separate free-tier quota.",
        ))

    # Ollama Cloud — works fine for free; light quota.
    _key = os.environ.get("OLLAMA_CLOUD_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="ollama_cloud",
            base_url="https://ollama.com/v1/chat/completions",
            api_key=_key,
            models=(
                "gpt-oss:120b",
                "qwen3-coder:480b",
                "deepseek-v3.1:671b",
            ),
            rpm=30,
            note="User-confirmed working when openrouter rate-limits.",
        ))

    # Cloudflare Workers AI — 10K neurons/day free. URL needs account_id.
    _account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    _key = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if _account and _key:
        providers.append(Provider(
            name="cloudflare",
            base_url=(
                f"https://api.cloudflare.com/client/v4/accounts/{_account}"
                f"/ai/v1/chat/completions"
            ),
            api_key=_key,
            models=(
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "@cf/meta/llama-3.1-8b-instruct-fast",
            ),
            rpm=20,
            note="10K neurons/day free. 4096-token prompt cap on free tier.",
        ))

    # Vercel AI Gateway — aggregator; may share upstream with OpenRouter.
    _key = os.environ.get("VERCEL_AI_API_KEY", "").strip()
    if _key:
        providers.append(Provider(
            name="vercel",
            base_url="https://ai-gateway.vercel.sh/v1/chat/completions",
            api_key=_key,
            models=(
                "openai/gpt-oss-120b",
                "meta/llama-3.3-70b",
            ),
            rpm=15,
            note="Aggregator; may share upstream with OpenRouter — low priority.",
        ))

    return providers


def build_slots(providers: list[Provider]) -> list[Slot]:
    """Flatten providers into (provider, model, model_family, roles) slots.

    For each (provider, model), compute the canonical family and the
    role-eligibility tuple, then construct a Slot. Role-eligibility is
    inferred from the model slug; per-provider overrides can be added
    here later (e.g. "Cerebras's Qwen-235B is unusually good at JSON,
    add Role.PLANNER explicitly").
    """
    slots: list[Slot] = []
    for p in providers:
        for model in p.models:
            slots.append(Slot(
                provider=p,
                model=model,
                model_family=model_family(model),
                roles=_infer_roles(model),
            ))
    return slots


# ---------- CLI: registry inspection + ping smoke test ----------

def _format_registry(providers: list[Provider]) -> str:
    """Pretty-print the loaded registry for stdout."""
    if not providers:
        return "(no providers registered — check your .env)"
    lines = []
    for p in providers:
        lines.append(f"- {p.name}: {len(p.models)} model(s), rpm={p.rpm}")
        lines.append(f"    url: {p.base_url}")
        if p.note:
            lines.append(f"    note: {p.note}")
    return "\n".join(lines)


def _format_slots(slots: list[Slot]) -> str:
    """Pretty-print every slot with its family + roles."""
    lines = []
    for s in slots:
        roles = ",".join(r.value for r in s.roles)
        lines.append(
            f"  {s.id:60} family={s.model_family:25} roles=[{roles}]"
        )
    return "\n".join(lines)


async def _ping_provider(p: Provider) -> tuple[str, bool, str]:
    """Hit one provider with a trivial prompt. Return (name, ok, detail)."""
    import httpx
    payload = {
        "model": p.models[0],
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {p.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(p.extra_headers)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(p.base_url, headers=headers, json=payload)
            if r.status_code != 200:
                return (p.name, False, f"HTTP {r.status_code}: {r.text[:120]}")
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                return (p.name, False, f"upstream error: {str(data['error'])[:120]}")
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return (p.name, True, f"got {len(content)} chars: {content[:40]!r}")
    except Exception as e:
        return (p.name, False, f"exception: {e!r}"[:200])


async def _run_pings(providers: list[Provider]) -> int:
    """Ping every provider in parallel, print results, return exit code."""
    import asyncio
    results = await asyncio.gather(*(_ping_provider(p) for p in providers))
    failures = 0
    for name, ok, detail in results:
        marker = "[OK ]" if ok else "[FAIL]"
        print(f"  {marker} {name:14} {detail}")
        if not ok:
            failures += 1
    return 0 if failures == 0 else 1


def main() -> int:
    """CLI entry point.

      python scripts/providers.py                 prints registry + slots
      python scripts/providers.py --show-slots    also dumps every slot
      python scripts/providers.py --ping          hits each provider once
    """
    import sys
    providers = load_registry()
    print("Provider registry:")
    print(_format_registry(providers))
    slots = build_slots(providers)
    print(f"\nTotal slots: {len(slots)}")
    if "--show-slots" in sys.argv:
        print()
        print(_format_slots(slots))

    if "--ping" in sys.argv:
        import asyncio
        print("\nPinging each provider with a 1-token prompt...")
        return asyncio.run(_run_pings(providers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
