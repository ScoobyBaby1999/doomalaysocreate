"""Provider-native tool adapters.

This module is the single source of truth for "does this provider+model
support NATIVE web search / reasoning / tools / vision, and what's the
exact JSON body shape to invoke it?".

The data lives in `lib/reasoning_catalog.json` under two top-level keys:

  - ``reasoning``  — per-model thinking-mode body (the existing catalog,
                      verified 2026-06 against each provider's docs page).
  - ``web_search`` — per-provider "do they offer a NATIVE web-search
                      tool on the chat-completions endpoint?" + the body
                      fields needed to turn it on.

When a capability IS available natively, we send the provider's own
parameter (e.g. OpenRouter's ``plugins:[{id:"web"}]``) instead of
injecting search results into the prompt via ``lib/web_tools.py``.
When it is NOT available, we fall back to the web_tools injection
(ReAct-style ACTION: web_search / web_fetch loop, or research templates).

Why a separate module (instead of inlining into providers.py)?
  - providers.py is the data-registry layer (provider/slot/model dataclasses).
  - This module is the capability-detection layer (builds the per-request
    body fields from the catalog). Keeping them separate makes it easy to
    extend with more provider-native tools (file search, code interpreter,
    computer use, etc.) without bloating providers.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from providers import (
    REASONING_CATALOG_PATH,
    _load_json_commented,
    make_model_family,
    resolve_reasoning_body,
)


# ----------------------------------------------------------------------------
# provider_quirks.json — per-provider Python invocation reference
# ----------------------------------------------------------------------------

_PROVIDER_QUIRKS_PATH = Path(os.environ.get(
    "PROVIDER_QUIRKS", Path(__file__).resolve().parent / "provider_quirks.json"))
_QUIRKS_CACHE: dict | None = None


def load_provider_quirks() -> dict:
    """Load lib/provider_quirks.json — per-provider Python invocation reference.

    Cached on the module (the file ships with the codebase; a Space restart
    picks up any edits). The file documents how each provider expects to be
    called (base_url, auth, params, reasoning shape, web-search shape, model
    quirks, python snippet). Read by the roster endpoint so the frontend can
    surface provider-specific docs, and by StrandsAdapter for context-length
    defaults + temperature ranges.

    Returns the parsed JSON dict (with the ``providers`` section keyed by
    provider name) or {} when the file is missing/garbled.
    """
    global _QUIRKS_CACHE
    if _QUIRKS_CACHE is not None:
        return _QUIRKS_CACHE
    try:
        _QUIRKS_CACHE = _load_json_commented(_PROVIDER_QUIRKS_PATH)
    except (OSError, ValueError):
        _QUIRKS_CACHE = {}
    return _QUIRKS_CACHE


def get_provider_quirks(provider_name: str) -> dict:
    """Return the quirks dict for a specific provider.

    Normalises the provider name (dashes/spaces/underscores) so the caller
    can pass any form (``openrouter``, ``OPENROUTER``, ``Open Router``).
    Returns {} when the provider isn't documented.
    """
    p = (provider_name or "").strip().lower().replace("_", "-").replace(" ", "")
    sec = load_provider_quirks().get("providers", {}) or {}
    return dict(sec.get(p) or {})


def provider_max_context(provider_name: str, default: int = 8192) -> int:
    """Return the documented max-context default for this provider."""
    q = get_provider_quirks(provider_name)
    val = q.get("max_context_default")
    if isinstance(val, int) and val > 0:
        return val
    return default


def provider_temperature_range(provider_name: str) -> tuple[float, float, float]:
    """Return (min, max, default) temperature for this provider."""
    q = get_provider_quirks(provider_name)
    t = q.get("temperature") or {}
    try:
        mn = float(t.get("min", 0.0))
        mx = float(t.get("max", 2.0))
        df = float(t.get("default", 0.7))
    except (TypeError, ValueError):
        mn, mx, df = 0.0, 2.0, 0.7
    return (mn, mx, df)


def provider_extra_headers(provider_name: str) -> dict:
    """Return any documented extra HTTP headers for this provider.

    These are merged into the LiteLLM client_args.extra_headers at runtime
    by StrandsAdapter (e.g. OpenRouter wants HTTP-Referer + X-Title).
    """
    q = get_provider_quirks(provider_name)
    h = q.get("extra_headers") or {}
    return dict(h) if isinstance(h, dict) else {}


# ----------------------------------------------------------------------------
# catalog loader
# ----------------------------------------------------------------------------

def _catalog() -> dict:
    """Load the full reasoning_catalog.json (reasoning + web_search sections).

    Cached on the module to avoid re-reading on every call. The catalog is
    static at runtime (it ships with the codebase); a Space restart picks up
    any edits.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        try:
            _CATALOG_CACHE = _load_json_commented(REASONING_CATALOG_PATH)
        except (OSError, ValueError):
            _CATALOG_CACHE = {}
    return _CATALOG_CACHE


_CATALOG_CACHE: dict | None = None


def _web_search_section() -> dict[str, dict]:
    """Return the web_search section of the catalog (or {} if absent)."""
    sec = _catalog().get("web_search", {})
    return {k: v for k, v in sec.items()
            if not k.startswith("//") and isinstance(v, dict)}


# ----------------------------------------------------------------------------
# helpers — turn a (provider, model) into catalog keys
# ----------------------------------------------------------------------------

def _normalize_provider(provider: str) -> str:
    """Normalize provider names across catalogs/env vars (separator-insensitive)."""
    return (provider or "").strip().lower().replace("_", "-").replace(" ", "")


def _candidate_keys(provider: str, model: str) -> list[str]:
    """Build the ordered list of catalog keys to look up for a (provider, model).

    Resolution order (most-specific → least-specific):
      1. exact provider/model           e.g. "openrouter/moonshotai/kimi-k2.6:free"
      2. provider/<model-suffix>        e.g. "openrouter/kimi-k2.6:free"
      3. provider/*                      e.g. "openrouter/*"
      4. "*"                             (catch-all default)
    """
    p = _normalize_provider(provider)
    m = (model or "").strip()
    out = []
    if p and m:
        out.append(f"{p}/{m}")
    # also try with just the last path segment (handles "@cf/moonshotai/kimi-k2.6")
    if m:
        last = m.split("/")[-1]
        if last and last != m:
            out.append(f"{p}/{last}")
    if p:
        out.append(f"{p}/*")
    out.append("*")
    # de-dup while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


# ----------------------------------------------------------------------------
# native web-search detection
# ----------------------------------------------------------------------------

def supports_native_web_search(provider_name: str, model_id: str) -> bool:
    """True if this (provider, model) has a NATIVE web-search tool.

    Returns True only when the catalog explicitly says so — we never
    optimistically turn on a provider's web search for a model we haven't
    verified (a 400 blacklists the slot, and some providers charge real
    money per request).
    """
    sec = _web_search_section()
    for key in _candidate_keys(provider_name, model_id):
        entry = sec.get(key)
        if isinstance(entry, dict):
            # The catch-all "*" is the default-false fallback; only a more
            # specific entry can flip it True.
            if key == "*":
                continue
            return bool(entry.get("native", False))
    # Last resort: the "*" default (False unless someone overrides it).
    star = sec.get("*") or {}
    return bool(star.get("native", False))


def native_web_search_body(provider_name: str, model_id: str) -> dict:
    """Return the request-body fields to turn on NATIVE web search.

    Empty dict means: no native web search available — fall back to
    ``lib/web_tools.py`` injection (Tavily/DuckDuckGo) instead.
    """
    if not supports_native_web_search(provider_name, model_id):
        return {}
    sec = _web_search_section()
    for key in _candidate_keys(provider_name, model_id):
        entry = sec.get(key)
        if isinstance(entry, dict) and entry.get("native") and key != "*":
            body = entry.get("body")
            return dict(body) if isinstance(body, dict) else {}
    return {}


# ----------------------------------------------------------------------------
# effort / reasoning
# ----------------------------------------------------------------------------

def reasoning_body_for(provider_name: str, model_id: str,
                       *, logical: str | None = None,
                       family: str | None = None) -> dict:
    """Return the reasoning/thinking body fields for this (provider, model).

    Thin wrapper around ``providers.resolve_reasoning_body`` that fills in
    the ``who`` and ``family`` from the provider+model so callers don't
    have to. Empty dict means "no reasoning param — rely on the model's
    default behaviour + token budget".
    """
    who = f"{_normalize_provider(provider_name)}/{model_id}" if provider_name else None
    if family is None and model_id:
        family = make_model_family(model_id)
    return resolve_reasoning_body(
        _catalog().get("reasoning", {}) or {},
        logical=logical, who=who, family=family,
    )


def reasoning_param_name(provider_name: str, model_id: str) -> str | None:
    """Return the canonical top-level param name this provider uses for effort.

    Useful for the capability endpoint so the frontend can show which
    param is in play. Returns one of:
      - "reasoning"            (OpenRouter: reasoning:{enabled|effort})
      - "reasoning_effort"     (NVIDIA NIM new models + Cloudflare kimi-k2.6 + OpenAI o-series)
      - "chat_template_kwargs" (NVIDIA NIM legacy models + Cloudflare nemotron-120b/gemma-4)
      - None                    (no thinking param documented)
    """
    body = reasoning_body_for(provider_name, model_id)
    if not body:
        return None
    if "reasoning" in body:
        return "reasoning"
    if "reasoning_effort" in body:
        return "reasoning_effort"
    if "chat_template_kwargs" in body:
        return "chat_template_kwargs"
    # Some other shape (e.g. reasoning_budget alone) — best-effort label.
    return next(iter(body.keys()), None)


# ----------------------------------------------------------------------------
# capability roll-up
# ----------------------------------------------------------------------------

def get_model_capabilities(provider_name: str, model_id: str,
                           *, logical: str | None = None,
                           family: str | None = None,
                           benchmarks: dict | None = None,
                           use_live_detection: bool = True) -> dict:
    """Return what this (provider, model) actually supports on its host.

    Delegates to ``providers.get_model_capabilities`` so the live-effort
    detector (``lib/effort_detector.py``) is consulted in a single place.
    The detector merges:
      - LIVE OpenRouter /api/v1/models supported_parameters per model
        (dynamically detects effort + tools + vision for OpenRouter models)
      - LIVE GitHub Models catalog capabilities array
        (surfaces reasoning + tool-calling for github-models)
      - curated reasoning_catalog.json entries for providers without a live
        per-model effort API (NVIDIA NIM, Cloudflare, PrivateMode AI, OpenCode)

    Returns:
        {
            'effort': bool,         # supports a reasoning/thinking param
            'effort_param': str|None,  # the param name (e.g. 'reasoning_effort')
            'effort_levels': list[str],  # ordered level names (empty = no knob)
            'web_search': bool,     # supports native web-search tool
            'web_search_native': bool,  # alias of web_search (clarity)
            'tools': bool,          # supports function/tool calling
            'vision': bool,         # supports image input
            'audio': bool,          # supports audio input (OpenRouter live)
            'video': bool,          # supports video input (OpenRouter live)
        }

    ``benchmarks`` is the optional per-logical-model benchmark dict from
    ``benchmarks.json`` (used to surface tools/vision tags when the catalog
    doesn't already imply them).

    ``use_live_detection`` (default True) toggles the live OpenRouter +
    GitHub Models API fetch. Set to False for offline mode (catalog-only).
    """
    from providers import get_model_capabilities as _get_caps
    return _get_caps(
        provider_name, model_id,
        logical=logical, family=family, benchmarks=benchmarks,
        use_live_detection=use_live_detection,
    )


# ----------------------------------------------------------------------------
# convenience: merge everything into one extra_body for a request
# ----------------------------------------------------------------------------

def build_extra_body(provider_name: str, model_id: str, *,
                     logical: str | None = None,
                     family: str | None = None,
                     enable_reasoning: bool = True,
                     enable_web_search: bool = False) -> dict:
    """Build the merged extra_body for a chat-completions request.

    Combines:
      - reasoning_body_for(...)  when enable_reasoning=True
      - native_web_search_body(...)  when enable_web_search=True

    Shallow-merges both into one dict suitable for passing as
    ``extra_body`` to ``scheduler.call_slot`` (or as
    ``additional_request_params={"extra_body": ...}`` to Strands' LiteLLM).

    Caller is responsible for deciding whether to enable each — typically:
      - enable_reasoning = (effort != "low")
      - enable_web_search = (web_search or deep_research requested) AND
                            supports_native_web_search(...)
      - if native web search is NOT supported, caller falls back to the
        web_tools.py injection loop (research templates).
    """
    extra: dict = {}
    if enable_reasoning:
        extra.update(reasoning_body_for(
            provider_name, model_id, logical=logical, family=family))
    if enable_web_search:
        extra.update(native_web_search_body(provider_name, model_id))
    return extra


__all__ = [
    "supports_native_web_search",
    "native_web_search_body",
    "reasoning_body_for",
    "reasoning_param_name",
    "get_model_capabilities",
    "build_extra_body",
    "load_provider_quirks",
    "get_provider_quirks",
    "provider_max_context",
    "provider_temperature_range",
    "provider_extra_headers",
]
