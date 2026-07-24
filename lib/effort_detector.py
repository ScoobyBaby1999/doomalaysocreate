"""Dynamic effort-mode detection per provider/model.

For each (provider, model_id) we determine:
  - Does this model support a reasoning/effort parameter?
  - What are the canonical effort level NAMES it accepts?
  - What's the request-body shape that turns effort on for a given level?

Live data sources (in priority order):

  1. **OpenRouter /api/v1/models** — returns ``supported_parameters`` per model.
     If the list contains ``reasoning`` (or ``reasoning_effort``), the model
     accepts the OpenRouter reasoning shape ``reasoning:{effort: LEVEL}`` with
     the canonical 7-level set
     ``["none", "minimal", "low", "medium", "high", "xhigh", "max"]``
     (verified live at https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
     Also surfaces ``architecture.input_modalities`` (vision/image) +
     tool-calling support, so we can dynamically compute capability bits for
     the roster endpoint.

  2. **GitHub Models catalog** — returns ``capabilities: ["reasoning", ...]``.
     "reasoning" on GitHub Models means the model reasons NATIVELY (no
     configurable effort knob — phi-4-reasoning, deepseek-r1), so the effort
     list is EMPTY (frontend hides the effort button). Tool-calling capability
     is also exposed via the same ``capabilities`` field.

  3. **Curated catalog** — ``lib/reasoning_catalog.json`` carries hand-verified
     per-(provider/model) entries for providers whose API does NOT expose a
     per-model effort schema (NVIDIA NIM per-model reference pages, Cloudflare
     Workers AI, PrivateMode AI, OpenCode Zen). These entries encode the exact
     param shape each host expects and the canonical effort level names.

The detector merges the three sources: live OpenRouter data wins for OpenRouter
models, GitHub Models catalog wins for github-models models, curated catalog
wins for the rest.

Caching: OpenRouter's model list is cached for 10 minutes in-process (matches
provider_sync.catalog). GitHub Models catalog is cached for 10 minutes too.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from pathlib import Path

from oplog import log_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The canonical OpenRouter 7-level effort ladder (verified live at
# https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
# Sent as `reasoning:{effort: LEVEL}` (mutually exclusive with reasoning.max_tokens).
OPENROUTER_EFFORT_LEVELS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

# NVIDIA NIM per-model effort ladders (verified via each model's docs page).
# Each model exposes EITHER a `reasoning_effort` enum OR a boolean
# `chat_template_kwargs:{thinking|enable_thinking}` toggle — never both.
NVIDIA_EFFORT_LEVELS_REASONING_EFFORT_3 = ("none", "high", "max")          # deepseek-v4-pro/flash
NVIDIA_EFFORT_LEVELS_REASONING_EFFORT_3_MEDIUM = ("none", "medium", "high")  # nemotron-3-ultra
NVIDIA_EFFORT_LEVELS_THINKING_TOGGLE = ("on", "off")                     # kimi-k2.6, glm-5.1

# Cloudflare Workers AI per-model effort ladders (verified via per-model docs page).
CF_EFFORT_LEVELS_REASONING_EFFORT = ("low", "medium", "high")            # kimi-k2.6
CF_EFFORT_LEVELS_THINKING_TOGGLE = ("on", "off")                          # nemotron-120b, gemma-4

# PrivateMode AI per-model effort ladders (verified at
# https://docs.privatemode.ai/models/overview/).
PMAI_EFFORT_LEVELS_THINKING_TOGGLE = ("on", "off")                       # kimi-k2.6, gemma-4

_openrouter_models_url = "https://openrouter.ai/api/v1/models?output_modalities=text"
_github_models_url = "https://models.github.ai/catalog/models"

_fetch_timeout = 15
_user_agent = "doomalaysocreate/1.0"
_cache_ttl_s = 600  # 10 minutes — matches provider_sync.catalog

# ---------------------------------------------------------------------------
# Live-fetch caches (OpenRouter + GitHub Models model lists)
# ---------------------------------------------------------------------------

_or_cache: dict[str, dict[str, Any]] | None = None  # raw_id -> model metadata
_or_cache_at: float = 0
_or_cache_lock = threading.Lock()

_gh_cache: list[dict[str, Any]] | None = None
_gh_cache_at: float = 0
_gh_cache_lock = threading.Lock()


def _fetch_json(url: str, headers: dict | None = None) -> Any:
    request_headers = {
        "User-Agent": _user_agent,
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(req, timeout=_fetch_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log_event("effort_detector_fetch_error", url=url[:160], error=str(e)[:300])
        return None


def _openrouter_models() -> dict[str, dict[str, Any]]:
    """Return OpenRouter's model list as a dict {raw_model_id: model_metadata}.

    Cached for 10 minutes in-process. The fetch is unauthenticated (the
    /api/v1/models endpoint is public); the caller decides whether to also
    filter by free-tier premium gate.
    """
    global _or_cache, _or_cache_at
    now = time.time()
    if _or_cache is not None and now - _or_cache_at < _cache_ttl_s:
        return _or_cache
    with _or_cache_lock:
        if _or_cache is not None and now - _or_cache_at < _cache_ttl_s:
            return _or_cache
        data = _fetch_json(_openrouter_models_url)
        out: dict[str, dict[str, Any]] = {}
        if isinstance(data, dict):
            for item in data.get("data", []) or []:
                if isinstance(item, dict) and item.get("id"):
                    out[item["id"]] = item
        if out:
            _or_cache = out
            _or_cache_at = time.time()
            log_event("effort_detector_or_cached", count=len(out))
        return out or {}


def _github_models() -> list[dict[str, Any]]:
    """Return GitHub Models catalog as a list of model dicts.

    Cached for 10 minutes. The catalog is public; each entry exposes
    ``capabilities`` (array) + ``supported_input_modalities``.
    """
    global _gh_cache, _gh_cache_at
    now = time.time()
    if _gh_cache is not None and now - _gh_cache_at < _cache_ttl_s:
        return _gh_cache
    with _gh_cache_lock:
        if _gh_cache is not None and now - _gh_cache_at < _cache_ttl_s:
            return _gh_cache
        data = _fetch_json(_github_models_url)
        out: list[dict[str, Any]] = []
        if isinstance(data, list):
            out = [m for m in data if isinstance(m, dict) and m.get("id")]
        if out:
            _gh_cache = out
            _gh_cache_at = time.time()
            log_event("effort_detector_gh_cached", count=len(out))
        return out or []


def refresh_live_cache() -> None:
    """Force a refresh of the live OpenRouter + GitHub Models caches.

    Called by ``POST /api/models/resync`` so the user can pull the latest
    effort-mode data on demand.
    """
    global _or_cache, _or_cache_at, _gh_cache, _gh_cache_at
    with _or_cache_lock:
        _or_cache = None
        _or_cache_at = 0
    with _gh_cache_lock:
        _gh_cache = None
        _gh_cache_at = 0
    _openrouter_models()
    _github_models()


# ---------------------------------------------------------------------------
# Provider normalisation + curated-catalog fallback
# ---------------------------------------------------------------------------

def _normalize_provider(provider: str) -> str:
    return (provider or "").strip().lower().replace("_", "-").replace(" ", "")


def _load_curated_reasoning() -> dict[str, dict]:
    """Load the curated reasoning_catalog.json 'reasoning' section.

    This is the fallback for providers whose API does NOT expose a per-model
    effort schema (NVIDIA NIM, Cloudflare Workers AI, PrivateMode AI, OpenCode
    Zen). Each entry encodes the exact param shape + effort_levels array.
    """
    here = Path(__file__).resolve().parent
    path = Path(os.environ.get("REASONING_CATALOG", here / "reasoning_catalog.json"))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    no_comments = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("//")
    )
    try:
        data = json.loads(no_comments)
    except ValueError:
        return {}
    sec = data.get("reasoning", {}) if isinstance(data, dict) else {}
    return {k: v for k, v in sec.items()
            if not k.startswith("//") and isinstance(v, dict)}


def _curated_lookup(provider: str, model_id: str, *,
                    logical: str | None = None,
                    family: str | None = None) -> dict:
    """Return the curated reasoning_catalog entry for (provider, model).

    Resolution order: provider/model -> provider/<last-seg> -> provider/* ->
    logical -> family -> '*'. Returns {} when nothing matches.
    """
    catalog = _load_curated_reasoning()
    if not catalog:
        return {}
    p = _normalize_provider(provider)
    m = (model_id or "").strip()
    who = f"{p}/{m}" if p and m else None
    candidates: list[str | None] = []
    if who:
        candidates.append(who)
    if m:
        last = m.split("/")[-1]
        if last and last != m:
            candidates.append(f"{p}/{last}")
    if p:
        candidates.append(f"{p}/*")
    if logical:
        candidates.append(logical)
    if family:
        candidates.append(family)
    candidates.append("*")
    for key in candidates:
        if key and key in catalog:
            entry = catalog[key]
            if isinstance(entry, dict):
                return entry
    return {}


def _curated_to_levels(entry: dict) -> list[str]:
    levels = entry.get("effort_levels")
    if not isinstance(levels, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for lvl in levels:
        s = str(lvl).strip() if lvl is not None else ""
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Per-provider live detection
# ---------------------------------------------------------------------------

def _detect_openrouter(model_id: str) -> tuple[list[str], dict, dict[str, Any]]:
    """Live-detect effort levels for an OpenRouter model.

    Returns (effort_levels, body_template, capabilities_extra).
    - effort_levels: [] when the model doesn't support reasoning on OpenRouter.
    - body_template: the body to send at level X. Empty when no reasoning.
    - capabilities_extra: {vision, tools} hints from the OpenRouter model
      metadata (we don't trust benchmarks.json alone).
    """
    models = _openrouter_models()
    if not models:
        return [], {}, {}
    m = models.get(model_id)
    if not m:
        # Try matching by stripping :free suffix in case the caller passed the
        # logical ID instead of the raw OpenRouter ID.
        if model_id and model_id.endswith(":free"):
            m = models.get(model_id[:-5])
        if not m:
            return [], {}, {}
    sp = m.get("supported_parameters") or []
    has_reasoning = ("reasoning" in sp) or ("reasoning_effort" in sp) or ("include_reasoning" in sp)
    caps_extra: dict[str, Any] = {}
    arch = m.get("architecture") or {}
    modalities = arch.get("input_modalities") or []
    if "image" in modalities:
        caps_extra["vision"] = True
    if "video" in modalities:
        caps_extra["video"] = True
    if "audio" in modalities:
        caps_extra["audio"] = True
    if "tools" in sp or "tool_choice" in sp:
        caps_extra["tools"] = True
    if not has_reasoning:
        return [], {}, caps_extra
    # OpenRouter reasoning: send `reasoning:{effort: LEVEL}`. Default body
    # uses 'high' (matches the catalog's existing entry). The detector
    # returns the canonical 7-level set so the frontend can show all options.
    body_template = {"reasoning": {"effort": "high"}}
    return list(OPENROUTER_EFFORT_LEVELS), body_template, caps_extra


def _detect_github_models(model_id: str) -> tuple[list[str], dict, dict[str, Any]]:
    """Live-detect effort levels for a GitHub Models model.

    GitHub Models returns ``capabilities: ["reasoning", ...]`` for models
    that reason NATIVELY (phi-4-reasoning, deepseek-r1). There is NO
    configurable effort knob — the model just thinks on every request.
    So effort_levels=[] (frontend hides the effort button) for both cases.
    Returns (effort_levels, body_template, capabilities_extra).
    """
    models = _github_models()
    if not models:
        return [], {}, {}
    # Match by raw id or by stripping publisher prefix (openai/gpt-4.1 -> gpt-4.1).
    by_id = {m.get("id"): m for m in models if m.get("id")}
    m = by_id.get(model_id)
    if not m and model_id:
        stripped = model_id.split("/")[-1]
        for k, v in by_id.items():
            if k.split("/")[-1] == stripped:
                m = v
                break
    if not m:
        return [], {}, {}
    caps_extra: dict[str, Any] = {}
    caps_list = m.get("capabilities") or []
    if "tool-calling" in caps_list:
        caps_extra["tools"] = True
    modalities = m.get("supported_input_modalities") or []
    if "image" in modalities or "Image" in modalities:
        caps_extra["vision"] = True
    # GitHub Models reasoning models reason natively — no effort param.
    # Effort_levels is empty (frontend hides the button). This is intentional.
    return [], {}, caps_extra


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_effort_levels(provider: str, model_id: str, *,
                         logical: str | None = None,
                         family: str | None = None) -> list[str]:
    """Return the list of effort level names this (provider, model) supports.

    Resolution order:
      1. OpenRouter live API (supported_parameters contains 'reasoning')
         -> canonical 7-level set.
      2. GitHub Models live catalog (capabilities=['reasoning']) -> [] (native).
      3. Curated reasoning_catalog.json entry for (provider, model) -> its
         effort_levels array.
      4. [] when none of the above match (model has no effort parameter).

    Examples:
      - OpenRouter kimi-k3 -> ["none","minimal","low","medium","high","xhigh","max"]
      - NVIDIA deepseek-v4-pro -> ["none","high","max"]
      - NVIDIA kimi-k2.6 -> ["on","off"]
      - GitHub Models phi-4-reasoning -> [] (native reasoning, no knob)
      - gpt-oss-120b on Cloudflare -> [] (no reasoning param on CF chat-completions)
    """
    p = _normalize_provider(provider)
    if p == "openrouter":
        levels, _, _ = _detect_openrouter(model_id)
        if levels:
            return levels
    if p == "github-models":
        levels, _, caps_extra = _detect_github_models(model_id)
        # GitHub Models with "reasoning" capability -> []. But a non-reasoning
        # GitHub model also returns [] from the detector. Both are correct:
        # the frontend hides the button in either case.
        if caps_extra:
            # We have a live catalog hit — trust it (no effort param).
            return []
    # Curated catalog fallback (NVIDIA, Cloudflare, PrivateModeAI, OpenCode,
    # AND OpenRouter/github-models when the live fetch failed).
    entry = _curated_lookup(p, model_id, logical=logical, family=family)
    return _curated_to_levels(entry)


def detect_effort_body(provider: str, model_id: str, level: str | None = None, *,
                       logical: str | None = None,
                       family: str | None = None) -> dict:
    """Return the request-body fields to set the model to ``level`` effort.

    For OpenRouter: ``{"reasoning": {"effort": level}}`` (level default "high").
    For NVIDIA: ``{"reasoning_effort": level}`` (3-level) OR
                ``{"chat_template_kwargs": {"thinking": True/False}}`` (toggle) --
                resolved from the curated catalog entry.
    For Cloudflare / PrivateModeAI: same approach as NVIDIA -- curated catalog.
    For GitHub Models: empty (no effort param).

    For toggle ("on"/"off") models, "on" -> True, "off" -> False, anything else
    -> True (treat as on, the model's default reasoning behaviour).

    Returns ``{}`` when the model has no effort parameter.
    """
    p = _normalize_provider(provider)
    if p == "openrouter":
        levels, body_template, _ = _detect_openrouter(model_id)
        if levels:
            # Pick a valid level (default 'high' if the requested one isn't supported).
            chosen = level if level in levels else "high"
            return {"reasoning": {"effort": chosen}}
    if p == "github-models":
        return {}
    # Curated catalog: use the body as-is (it encodes the "on" / default state).
    # For toggle models, translate the level to a boolean.
    entry = _curated_lookup(p, model_id, logical=logical, family=family)
    body = entry.get("body")
    if not isinstance(body, dict):
        return {}
    levels = _curated_to_levels(entry)
    # Toggle-style models: "on"/"off" -> True/False on the chat_template_kwargs flag.
    if levels == list(NVIDIA_EFFORT_LEVELS_THINKING_TOGGLE) or \
       levels == list(CF_EFFORT_LEVELS_THINKING_TOGGLE) or \
       levels == list(PMAI_EFFORT_LEVELS_THINKING_TOGGLE):
        ctkw = body.get("chat_template_kwargs")
        if isinstance(ctkw, dict):
            new_body = dict(body)
            new_ctkw = dict(ctkw)
            # The first boolean key (thinking | enable_thinking) is the toggle.
            for k, v in list(ctkw.items()):
                if isinstance(v, bool):
                    new_ctkw[k] = (level != "off")
                break
            new_body["chat_template_kwargs"] = new_ctkw
            return new_body
    # reasoning_effort enum models: swap in the requested level if valid.
    if "reasoning_effort" in body and levels:
        chosen = level if (level and level in levels) else body.get("reasoning_effort", "high")
        new_body = dict(body)
        new_body["reasoning_effort"] = chosen
        return new_body
    return dict(body)


def detect_model_capabilities(provider: str, model_id: str, *,
                              logical: str | None = None,
                              family: str | None = None,
                              benchmarks: dict | None = None) -> dict:
    """Return the full capability dict for this (provider, model).

    Combines:
      - effort support + effort_levels + effort_param (live + curated)
      - tools / vision / audio / video (live OpenRouter architecture +
        GitHub Models capabilities + benchmarks tags fallback)
      - native web-search support (from provider_tools.supports_native_web_search)

    Returns:
        {
            "effort": bool,
            "effort_param": str|None,
            "effort_levels": list[str],
            "web_search": bool,
            "web_search_native": bool,
            "tools": bool,
            "vision": bool,
            "audio": bool,
            "video": bool,
        }
    """
    p = _normalize_provider(provider)
    levels: list[str] = []
    body_template: dict = {}
    caps_extra: dict[str, Any] = {}

    if p == "openrouter":
        levels, body_template, caps_extra = _detect_openrouter(model_id)
        if not levels:
            # Fall back to curated catalog (covers :free models when the live
            # fetch failed AND the curated entry has effort_levels).
            entry = _curated_lookup(p, model_id, logical=logical, family=family)
            levels = _curated_to_levels(entry)
            body_template = entry.get("body") if isinstance(entry.get("body"), dict) else {}
    elif p == "github-models":
        levels, _, caps_extra = _detect_github_models(model_id)
    else:
        entry = _curated_lookup(p, model_id, logical=logical, family=family)
        levels = _curated_to_levels(entry)
        body_template = entry.get("body") if isinstance(entry.get("body"), dict) else {}

    # effort_param: derived from the body template.
    if body_template:
        if "reasoning" in body_template:
            effort_param = "reasoning"
        elif "reasoning_effort" in body_template:
            effort_param = "reasoning_effort"
        elif "chat_template_kwargs" in body_template:
            effort_param = "chat_template_kwargs"
        else:
            effort_param = next(iter(body_template.keys()), None)
    else:
        effort_param = None

    # Tools / vision: prefer live signals, fall back to benchmarks + name heuristics.
    bench = benchmarks or {}
    bench_lower = " ".join(str(bench.get(k, "") or "").lower()
                           for k in ("tags", "note")).lower()
    full = (bench_lower + " " + (logical or "").lower()
            + " " + (model_id or "").lower()
            + " " + (provider or "").lower())
    has_vision = bool(caps_extra.get("vision")) or any(
        t in full for t in ("vision", "image", "multimodal"))
    has_tools = bool(caps_extra.get("tools")) or any(
        t in full for t in ("tool", "function", "agentic"))
    has_audio = bool(caps_extra.get("audio")) or "audio" in full
    has_video = bool(caps_extra.get("video")) or "video" in full

    # Host-level guarantees (mirror providers.get_model_capabilities).
    if p == "openrouter":
        has_tools = True
    if p == "cloudflare" and (model_id or "").startswith("@cf/"):
        if "function calling" in full or "tool" in full:
            has_tools = True

    # Native web search (from the catalog's web_search section via provider_tools).
    web_search_native = False
    try:
        from provider_tools import supports_native_web_search as _ws
        web_search_native = bool(_ws(p, model_id))
    except Exception:
        pass

    return {
        "effort": bool(body_template) or bool(levels),
        "effort_param": effort_param,
        "effort_levels": levels,
        "web_search": web_search_native,
        "web_search_native": web_search_native,
        "tools": has_tools,
        "vision": has_vision,
        "audio": has_audio,
        "video": has_video,
    }


__all__ = [
    "OPENROUTER_EFFORT_LEVELS",
    "NVIDIA_EFFORT_LEVELS_REASONING_EFFORT_3",
    "NVIDIA_EFFORT_LEVELS_REASONING_EFFORT_3_MEDIUM",
    "NVIDIA_EFFORT_LEVELS_THINKING_TOGGLE",
    "CF_EFFORT_LEVELS_REASONING_EFFORT",
    "CF_EFFORT_LEVELS_THINKING_TOGGLE",
    "PMAI_EFFORT_LEVELS_THINKING_TOGGLE",
    "refresh_live_cache",
    "detect_effort_levels",
    "detect_effort_body",
    "detect_model_capabilities",
]
