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
    resolve_reasoning_entry,
)


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
                           benchmarks: dict | None = None) -> dict:
    """Return what this (provider, model) actually supports on its host.

    Returns:
        {
            'effort': bool,         # supports a reasoning/thinking param
            'effort_param': str|None,  # the param name (e.g. 'reasoning_effort')
            'web_search': bool,     # supports native web-search tool
            'web_search_native': bool,  # alias of web_search (clarity)
            'tools': bool,          # supports function/tool calling
            'vision': bool,         # supports image input
        }

    ``benchmarks`` is the optional per-logical-model benchmark dict from
    ``benchmarks.json`` (used to surface tools/vision tags when the catalog
    doesn't already imply them).
    """
    rbody = reasoning_body_for(provider_name, model_id, logical=logical, family=family)
    effort_param = reasoning_param_name(provider_name, model_id) if rbody else None
    ws_native = supports_native_web_search(provider_name, model_id)

    # Tools/vision: derive from benchmarks tags + catalog notes when available.
    bench = benchmarks or {}
    bench_lower = " ".join(str(bench.get(k, "") or "").lower()
                           for k in ("tags", "note")).lower()
    full = (bench_lower + " " + (logical or "").lower()
            + " " + (model_id or "").lower()
            + " " + (provider_name or "").lower())
    has_vision = any(t in full for t in ("vision", "image", "multimodal"))
    has_tools = any(t in full for t in ("tool", "function", "agentic"))

    # Reasoning bodies themselves imply tool support (thinking models are
    # always tool-capable); OpenRouter always supports tools; CF Workers AI
    # chat-completions exposes tools on every model that has a chat-completions
    # endpoint. Conservative: leave it as-is (only flag when benchmark says).
    p = _normalize_provider(provider_name)
    if p in ("openrouter",):
        # OpenRouter exposes OpenAI-style tools on every chat-completions model.
        has_tools = True
    if p == "cloudflare" and (model_id or "").startswith("@cf/"):
        # CF chat-completions endpoint supports tools on every model that
        # accepts function calling (documented as "Function calling" tag).
        if "function calling" in full or "tool" in full:
            has_tools = True

    return {
        "effort": bool(rbody),
        "effort_param": effort_param,
        "web_search": ws_native,
        "web_search_native": ws_native,
        "tools": has_tools,
        "vision": has_vision,
    }


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
]
