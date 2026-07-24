"""Build the provider catalog JSON for GET /api/models.

Assembles the SyncResult shape the frontend expects (mirrors the TypeScript
sync.ts orchestrator in temporaryshidy). Combines live-synced model data with
host-policy config from providers_catalog.json + privacy/display fields.

All model data comes from live provider API syncs — no static model lists.

Usage:
    from provider_sync.catalog import build_provider_catalog
    result = build_provider_catalog(force_refresh=True)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from oplog import log_event

HERE = Path(__file__).resolve().parent.parent
CATALOG_PATH = HERE / "providers_catalog.json"

_FETCH_TIMEOUT = 15
_USER_AGENT = "doomalaysocreate/1.0"
_CACHE_TTL_S = 600  # 10 minutes

_cache: dict[str, Any] | None = None
_cache_at: float = 0
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Family key normalisation — mirrors TypeScript family.ts
# ---------------------------------------------------------------------------
_FAMILY_SUFFIXES = sorted([
    ":free", "-instruct-2507", "-instruct-fp8-fast", "-instruct-fast",
    "-fp8-fast", "-instruct", "-versatile", "-it", "-chat", "-preview",
    "-free",
], key=len, reverse=True)

_ACRONYMS = {"glm", "gpt", "oss", "llm", "mimo", "api", "fp8"}


def make_family(model_id: str) -> str:
    m = model_id.lower().strip()
    slash = m.rfind("/")
    if slash >= 0:
        m = m[slash + 1:]
    if m.startswith("@"):
        m = m[1:]
    changed = True
    while changed:
        changed = False
        for s in _FAMILY_SUFFIXES:
            if m.endswith(s):
                m = m[:-len(s)]
                changed = True
                break
    # After suffix stripping, if a prefix path remains (e.g. "meta/llama-3.3-70b"),
    # strip everything before the last "/" so models share the same family key
    # regardless of the provider serving them.
    slash = m.rfind("/")
    if slash >= 0:
        m = m[slash + 1:]
    return m


def derive_display_name(model_id: str) -> str:
    fam = make_family(model_id)
    tokens = [t for t in fam.split("-") if t]
    pretty = []
    for t in tokens:
        if re.fullmatch(r"\d+(\.\d+)*", t):
            pretty.append(t)
        elif t.lower() in _ACRONYMS:
            pretty.append(t.upper())
        else:
            pretty.append(t[0].upper() + t[1:])
    name = " ".join(pretty)
    if re.search(r"(?:^|[/:-])free$", model_id.lower()) or "-free" in model_id.lower():
        name += " (Free)"
    return name


def derive_capabilities_from_name(model_id: str) -> list[str]:
    fam = make_family(model_id)
    caps: list[str] = []
    if re.search(r"\b(gemma|kimi|llama-4|qwen3|glm-5|step)\b", fam) or "vision" in fam:
        caps.append("vision")
    return caps


def is_free_model(model_id: str, prompt_price: float | None = None, completion_price: float | None = None) -> bool:
    if re.search(r":free$", model_id.lower()) or "-free" in model_id.lower():
        return True
    if prompt_price is not None and completion_price is not None:
        return prompt_price == 0 and completion_price == 0
    return False


def _safe_float(v: Any) -> float | None:
    """Best-effort float coercion for pricing fields. Returns None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Static display config per provider (from Ts config.ts)
# ---------------------------------------------------------------------------
_PROVIDER_DISPLAY: dict[str, dict[str, Any]] = {
    "openrouter": {
        "displayName": "OpenRouter",
        "pool": "core",
        "region": "us",
        "icon": "Route",
        "color": "#f59e0b",
        "usageLimits": "Pay-as-you-go credits. Free (:free) models = $0 in/out. ~300 req/min typical. Per-request ZDR via provider.data_collection=deny.",
        "settingsUrl": "https://openrouter.ai/settings/privacy",
        "manageLabel": "Manage OpenRouter privacy & keys",
        "privacy": {
            "notice": "✓ Free (:free) models may log prompts for provider training. Opt out in Privacy settings. OpenRouter stores no prompts unless you opt in.",
            "confidence": "high",
            "retention": 'OpenRouter itself has a ZDR policy — prompts/responses NOT stored unless you opt in to Input & Output Logging (off by default; if on, retained ≥3 mo encrypted). Metadata (token counts, latency) always stored. Free (:free) models: "Prompts and completions may be logged by the provider and used to improve the model" (verbatim).',
            "training": 'OpenRouter does not train its own models. Free-model PROVIDERS may log & train on prompts (see notice). Opt out: disable "allow providers that may train on your data" (separate paid/free toggles) + per-model-group ZDR toggles at /settings/privacy. Per-request: pass provider.data_collection=deny.',
            "sources": [
                "https://openrouter.ai/collections/free-models",
                "https://openrouter.ai/docs/guides/privacy/data-collection",
                "https://openrouter.ai/docs/guides/privacy/provider-logging",
                "https://openrouter.ai/docs/guides/features/zdr",
            ],
        },
    },
    "nvidia": {
        "displayName": "NVIDIA",
        "pool": "core",
        "region": "us",
        "icon": "Cpu",
        "color": "#76b900",
        "usageLimits": "Free dev tier (build.nvidia.com): 1,000 credits (1,000 req/model). No published concurrency cap. Partner endpoints (DeepInfra, GMI, Vultr) billed separately.",
        "settingsUrl": "https://build.nvidia.com/settings/api-keys",
        "manageLabel": "Manage NVIDIA API keys",
        "privacy": {
            "notice": '✓ Default: prompts/outputs not stored after session (ToS §2.3). ⚠ NVIDIA may use them to improve its AI (§3.3) — no opt-out on free tier.',
            "confidence": "high",
            "retention": 'Default (free dev tier): prompts/completions NOT stored after the API session ends (NVIDIA API Trial ToS §2.3). Exceptions: Fine-Tuning API stores User Content 30 days, generated content 90 days (§2.4); security/fraud/abuse logs retained with no explicit cap. Enterprise (paid): Customer Data deleted within 90 days of termination (Cloud Services DPA). NOTE: the "5 years" figure in NVIDIA\'s Privacy Policy applies ONLY to account/contact personal data, NOT to model inputs/outputs.',
            "training": 'Per Trial ToS §3.3(iv): NVIDIA may use User Content & Generated Content "to improve NVIDIA products and services, including AI models." Training is contractually permitted on the free dev tier with NO documented opt-out. Enterprise tier governed by separate DPA.',
            "sources": [
                "https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf",
                "https://www.nvidia.com/en-us/about-nvidia/privacy-policy",
                "https://www.nvidia.com/en-us/agreements/data-processing-addendum/nvidia-cloud-services-data-processing-addendum",
            ],
        },
    },
    "cloudflare": {
        "displayName": "Cloudflare Workers AI",
        "pool": "core",
        "region": "global",
        "icon": "Cloud",
        "color": "#f97316",
        "usageLimits": "Free: 10,000 Neurons/day (resets 00:00 UTC). Paid: $0.011 / 1K Neurons above free. Text-gen rate limit: 300 req/min. Privacy identical on free & paid.",
        "settingsUrl": "https://dash.cloudflare.com/?to=/:account/ai/workers-ai",
        "manageLabel": "Manage Cloudflare Workers AI",
        "privacy": {
            "notice": "✓ Cloudflare stores no prompts/completions & never trains on them (per official Workers AI data-usage docs).",
            "confidence": "high",
            "retention": "Cloudflare does NOT store Workers AI prompts/completions. Storage only if you explicitly wire up R2/KV/Durable Objects/Vectorize. No abuse-monitoring retention window. Caveat: separate AI Gateway product (opt-in proxy) DOES log full request/response payloads — only if you route through it.",
            "training": 'Verbatim: "Cloudflare does not use your Customer Content to (1) train any AI models made available on Workers AI or (2) improve any Cloudflare or third-party services" without explicit consent. Cloudflare also does not create/train the models themselves (third-party open-source).',
            "sources": [
                "https://developers.cloudflare.com/workers-ai/platform/data-usage/",
                "https://developers.cloudflare.com/workers-ai/platform/pricing/",
                "https://developers.cloudflare.com/workers-ai/platform/limits/",
            ],
        },
    },
    "opencode-zen": {
        "displayName": "OpenCode Zen",
        "pool": "core",
        "region": "us",
        "icon": "Code",
        "color": "#10b981",
        "usageLimits": "Free tier (100 req/day) + pay-as-you-go. Free models have $0 inference. Paid models billed per-token via Zen account.",
        "settingsUrl": "https://opencode.ai/zen/keys",
        "manageLabel": "Manage OpenCode Zen keys",
        "privacy": {
            "notice": "✓ Go providers: zero-retention, no training. ⚠ Free Zen models (Big Pickle, Nemotron Free, MiMo/North Free) may train on data.",
            "confidence": "high",
            "retention": 'Upstream model providers (Go plan) follow a zero-retention policy — verbatim: "Our providers follow a zero-retention policy and do not use your data for model training." OpenCode itself (Anomaly) retains prompts as Personal Data "as long as necessary" (no fixed period published).',
            "training": 'Go-plan + paid Zen upstream providers do NOT train on your data. EXCEPTIONS (Zen free-tier only): Big Pickle, DeepSeek V4 Flash Free, MiMo V2.5 Free, North Mini Code Free — "collected data may be used to improve the model." Nemotron 3 Ultra Free is logged by NVIDIA for security & product improvement (not linked to identity).',
            "sources": [
                "https://opencode.ai/docs/go",
                "https://opencode.ai/docs/zen",
                "https://opencode.ai/legal/privacy-policy",
            ],
        },
    },
    "opencode-go": {
        "displayName": "OpenCode Go",
        "pool": "core",
        "region": "global (US · EU · SG)",
        "icon": "Code",
        "color": "#10b981",
        "usageLimits": "$5 first mo → $10/mo. Rolling caps: $12 / 5hr · $30 / wk · $60 / mo (limits in $, not requests). Cheaper models = more requests. Free models still usable when capped.",
        "settingsUrl": "https://console.opencode.ai",
        "manageLabel": "Manage on OpenCode Console",
        "privacy": {
            "notice": "✓ Zero-retention, no training on all Go-plan models.",
            "confidence": "high",
            "retention": 'OpenCode Go upstream providers follow a zero-retention policy. OpenCode retains prompts as Personal Data "as long as necessary" (no fixed period published).',
            "training": "Go-plan upstream providers do NOT train on your data per the OpenCode Go data-usage policy.",
            "sources": [
                "https://opencode.ai/docs/go",
                "https://opencode.ai/legal/privacy-policy",
            ],
        },
    },
    "github-models": {
        "displayName": "GitHub Models",
        "pool": "core",
        "region": "us",
        "icon": "GitBranch",
        "color": "#6e40c9",
        "usageLimits": "Free (low-tier rate limits). ~15 req/min, ~150 req/day. 4,000 max output tokens per request.",
        "settingsUrl": "https://github.com/marketplace/models",
        "manageLabel": "Manage GitHub Models",
        "privacy": {
            "notice": "✓ GitHub Models is free for prototyping. Prompts may be shared with model providers. See GitHub Models preview terms.",
            "confidence": "medium",
            "retention": "GitHub may retain prompts/outputs for abuse monitoring. Model providers have their own retention policies.",
            "training": "GitHub does not train on your prompts. Third-party model providers may have separate data usage policies.",
            "sources": [
                "https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models#about-data-privacy",
            ],
        },
    },
    "privatemodeai": {
        "displayName": "PrivateMode AI",
        "pool": "core",
        "region": "eu",
        "icon": "Shield",
        "color": "#4f46e5",
        "usageLimits": "Free: 5M initial tokens, then 1M prompt + 1M completion/mo. 20 req/min, 200K TPM, 20K TPD.",
        "settingsUrl": "https://portal.privatemode.ai",
        "manageLabel": "Manage PrivateMode account & keys",
        "privacy": {
            "notice": "✓ Hardware-enforced E2EE: prompts/responses never stored or trained on. Zero-access architecture — no one (not even Edgeless Systems) can see your data.",
            "confidence": "high",
            "retention": "Prompts/responses: NOT retained (stateless by design — zero-clear after inference completes). Metadata (API key, request path/method/status code): stored up to 90 days for monitoring. Token usage: permanently stored for billing. Optional prompt caching: cryptographically isolated per tenant (no cross-tenant leakage). Website privacy policy (www.privatemode.ai) is separate from API — covers marketing site cookies & contact forms only.",
            "training": "ZERO training — architecturally enforced via confidential computing (AMD SEV-SNP + Intel TDX + NVIDIA CC). Verbatim: 'We can never access your prompts, thus we can't train our models on your data.' No human review possible — prompts encrypted client-side with AES-256-GCM, decrypted only inside hardware-isolated enclaves. Public source code + reproducible builds enable independent verification.",
            "sources": [
                "https://docs.privatemode.ai/security/",
                "https://docs.privatemode.ai/getting-started/faq/",
                "https://docs.privatemode.ai/architecture/overview/",
                "https://docs.privatemode.ai/architecture/encryption/",
                "https://www.privatemode.ai/security-and-encryption",
                "https://www.privatemode.ai/privacy-policy",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _fetch_json(url: str, headers: dict | None = None) -> Any:
    req_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log_event("catalog_fetch_error", url=url, error=str(e)[:500])
        return None


# ---------------------------------------------------------------------------
# OpenRouter — richest source, builds the family registry
# ---------------------------------------------------------------------------
def _openrouter_premium() -> bool:
    """Read the OpenRouter premium flag (env var OPENROUTER_PREMIUM).

    Truthy values: 1, true, yes, on (case-insensitive). Default False.
    Mirrors provider_sync.openrouter._env_premium so the catalog builder
    and the sync class agree on the premium state without import cycles.
    """
    raw = os.environ.get("OPENROUTER_PREMIUM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _fetch_openrouter_family() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch OpenRouter's model list and build the family registry.

    Returns (models_list, family_registry) where family_registry maps
    family_key → {context, capabilities, benchmarks, pricing, ranks}.

    Premium gating: when the user has NOT opted into premium mode
    (env var OPENROUTER_PREMIUM unset/false), only ``:free`` models are
    kept — paid models are filtered out of BOTH the raw list and the
    family registry so the public catalog response never surfaces paid
    OpenRouter routes to a free-tier user. When premium is enabled, the
    full ~400-model list is returned.
    """
    url = "https://openrouter.ai/api/v1/models?output_modalities=text"
    data = _fetch_json(url)
    if not data:
        return [], {}

    all_models: list[dict] = data.get("data", [])
    # Free-tier filter: when not premium, keep only :free / $0-pricing models.
    if not _openrouter_premium():
        raw_models = [
            m for m in all_models
            if is_free_model(
                m.get("id", ""),
                _safe_float((m.get("pricing") or {}).get("prompt")),
                _safe_float((m.get("pricing") or {}).get("completion")),
            )
        ]
    else:
        raw_models = list(all_models)
    registry: dict[str, dict[str, Any]] = {}

    # Pass 1: register every model's family metadata
    for m in raw_models:
        mid = m.get("id", "")
        if not mid:
            continue
        family = make_family(mid)
        meta: dict[str, Any] = {}

        # context
        ctx = m.get("top_provider", {}) or {}
        context_length = ctx.get("context_length") or m.get("context_length") or 0
        if context_length:
            try:
                meta["context"] = int(context_length)
            except (ValueError, TypeError):
                pass

        # capabilities
        caps: list[str] = []
        inputs = m.get("architecture", {}) or {}
        modalities = inputs.get("input_modalities") or []
        if "image" in modalities:
            caps.append("vision")
        if "video" in modalities:
            caps.append("video")
        if "audio" in modalities:
            caps.append("audio")
        params = m.get("supported_parameters") or []
        if "tools" in params:
            caps.append("tools")
        if "reasoning" in params or "include_reasoning" in params:
            caps.append("reasoning")
        if caps:
            meta["capabilities"] = caps

        # benchmarks
        benchmarks = m.get("benchmarks", {}) or {}
        aa = benchmarks.get("artificial_analysis") or {}
        bm: dict[str, float] = {}
        if isinstance(aa.get("intelligence_index"), (int, float)):
            bm["intelligence"] = float(aa["intelligence_index"])
        if isinstance(aa.get("coding_index"), (int, float)):
            bm["coding"] = float(aa["coding_index"])
        if isinstance(aa.get("agentic_index"), (int, float)):
            bm["agentic"] = float(aa["agentic_index"])
        if bm:
            meta["benchmarks"] = bm

        # pricing
        pricing = m.get("pricing") or {}
        pp = pricing.get("prompt")
        cp = pricing.get("completion")
        if pp is not None and cp is not None:
            try:
                p_in = float(pp) * 1_000_000
                p_out = float(cp) * 1_000_000
                if p_in == 0 and p_out == 0:
                    meta["pricing"] = "$0 / $0 (free)"
                else:
                    fmt = lambda n: f"${n:.2f}" if n >= 0.01 else (f"{n:.2e}" if n > 0 else "$0.00")
                    meta["pricing"] = f"{fmt(p_in)} / {fmt(p_out)} per M"
            except (ValueError, TypeError):
                pass

        # ranks (OpenRouter-specific popularity)
        da = benchmarks.get("design_arena") or []
        if da and isinstance(da, list):
            sorted_da = sorted(da, key=lambda x: x.get("rank", 999))
            top = sorted_da[:3]
            ranks = [{"label": e.get("category", ""), "rank": e.get("rank", 0)} for e in top]
            if ranks:
                meta["ranks"] = ranks

        # free-model note
        prices = m.get("pricing") or {}
        prompt_price = prices.get("prompt")
        completion_price = prices.get("completion")
        try:
            pf = float(prompt_price) if prompt_price else None
            cf = float(completion_price) if completion_price else None
        except (ValueError, TypeError):
            pf = cf = None
        if is_free_model(mid, pf, cf):
            meta["free_note"] = "Free-model retention: prompts may be logged for provider training."

        if meta:
            # merge: keep first-defined per field (OpenRouter's metadata should not be
            # clobbered by a sparser source registering the same family later)
            if family not in registry:
                registry[family] = meta
            else:
                existing = registry[family]
                for k in ("context", "capabilities", "benchmarks", "pricing", "ranks"):
                    if k not in existing and k in meta:
                        existing[k] = meta[k]

    return raw_models, registry


# ---------------------------------------------------------------------------
# Sync provider model lists
# ---------------------------------------------------------------------------
def _sync_provider_models(catalog_entries: list[dict]) -> tuple[dict[str, list[str]], set[str]]:
    """Get live model ID lists, first trying the Panel's cached sync results
    (which succeeded at startup), falling back to a fresh sync if unavailable.

    Returns (model_map, live_set) where model_map maps provider_name → model IDs
    and live_set contains names of providers whose data came from a live sync.
    """
    from provider_sync import get_panel_sync_cache, sync_all_providers
    from providers import make_provider_registry

    # Try Panel's cached sync results first (avoids SSL errors from fresh instances)
    cached = get_panel_sync_cache()
    if cached:
        live = dict(cached)
        live_set = set(live.keys())
    else:
        # Fallback: fresh sync with new provider instances
        try:
            reg = make_provider_registry()
        except Exception:
            reg = []

        provider_map: dict[str, Any] = {}
        for p in reg:
            if hasattr(p, "name"):
                provider_map[p.name] = p

        live: dict[str, list[str]] = {}
        try:
            live = sync_all_providers(provider_map)
        except Exception as e:
            log_event("catalog_sync_error", error=str(e)[:500])
        live_set = set(live.keys())

    # A live sync that returned empty means the sync failed — provider gets no models.
    # Synced IDs are raw (e.g. "deepseek-ai/deepseek-v4-pro"). We return a dict
    # mapping {stripped_display_name: raw_id} so _build_provider_models can
    # construct slot IDs for pinned routing.
    # Synced IDs are raw (e.g. "deepseek-ai/deepseek-v4-pro"); normalize for display
    # by stripping the author prefix so models resolve through the logical catalog.
    result: dict[str, dict[str, str]] = {}
    actual_live: set[str] = set()
    for entry in catalog_entries:
        name = entry["name"]
        synced = live.get(name)
        if synced:
            result[name] = {m.split("/")[-1] if "/" in m else m: m for m in synced}
            actual_live.add(name)

    # Docs-only fallback for providers whose sync failed but can list models
    # without an API key (requires_auth=False). Runs a fresh fetch_models()
    # with api_key=None so the result is independent of registration.
    for entry in catalog_entries:
        name = entry["name"]
        if name in actual_live:
            continue
        try:
            sync_class = get_provider_sync_class(name)
        except Exception:
            sync_class = None
        if not sync_class or getattr(sync_class, "requires_auth", True):
            continue
        try:
            sync_instance = sync_class(api_key=None)
            models = sync_instance.fetch_models()
            # Apply the provider's free-tier filter (e.g. OpenRouter's premium
            # gate) — the docs-only fallback bypasses sync() which normally
            # applies this. Without it, a free-tier user would see ALL ~400
            # OpenRouter models instead of just the :free ones.
            if hasattr(sync_instance, "filter_free_models"):
                try:
                    models = sync_instance.filter_free_models(models)
                except Exception:
                    pass  # best-effort — keep unfiltered on filter error
            if models:
                result[name] = {
                    m.id.split("/")[-1] if "/" in m.id else m.id: m.id
                    for m in models
                }
                actual_live.add(name)
        except Exception as e:
            log_event(f"sync_docs_fallback_error", provider=name, error=str(e)[:500])

    return result, actual_live


# ---------------------------------------------------------------------------
# Assemble per-provider model list with enrichment
# ---------------------------------------------------------------------------
def _build_provider_models(
    provider_name: str,
    model_ids: dict[str, str],  # {stripped_display: raw_id}
    registry: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    # CHAT-RELIABILITY-FIX: import effort_detector lazily so this module stays
    # import-safe even if the detector's live OpenRouter/GitHub fetch fails
    # (best-effort: the detector swallows its own errors and returns []).
    try:
        from effort_detector import detect_effort_levels as _det_levels
    except Exception:
        _det_levels = None

    models: list[dict[str, Any]] = []
    for display_id, raw_id in model_ids.items():
        if not display_id:
            continue

        logical_id = display_id
        family = make_family(raw_id)
        fam_meta = registry.get(family, {})

        context_length = fam_meta.get("context", 0)
        if not context_length:
            context_length = 0

        attributes: dict[str, Any] = {}

        caps = fam_meta.get("capabilities")
        if caps:
            attributes["capabilities"] = caps
        else:
            dc = derive_capabilities_from_name(raw_id)
            if dc:
                attributes["capabilities"] = dc

        bm = fam_meta.get("benchmarks")
        if bm:
            attributes["benchmarks"] = bm

        pricing = fam_meta.get("pricing")
        if pricing:
            attributes["pricing"] = pricing

        ranks = fam_meta.get("ranks")
        if ranks:
            attributes["ranks"] = ranks
            attributes["note"] = "Ranks = usage/spend-share popularity (not intelligence)."

        free_note = fam_meta.get("free_note")
        if free_note:
            existing = attributes.get("note", "")
            attributes["note"] = f"{existing} {free_note}".strip() if existing else free_note

        # CHAT-RELIABILITY-FIX: per-HOST effort_levels.
        #
        # Previously the catalog returned the OpenRouter family-level
        # capabilities (e.g. ["reasoning","tools","vision"]) but NEVER
        # populated `attributes.effort_levels`. The frontend fell back to a
        # hardcoded ["low","med","high","max"] tuple for every model — so
        # the effort popover showed the SAME four names whether the user
        # picked NVIDIA GLM-5.2 (real levels: on/off), OpenRouter GLM-5.2
        # (7-level ladder), or PrivateModeAI GLM-5.2 (on/off). The user
        # reported exactly this: "the effort mode showed the same naming
        # conventions for both privatemodeai and Nvidia."
        #
        # Fix: call effort_detector.detect_effort_levels(provider, raw_id)
        # for each (provider, raw_model_id) pair. The detector:
        #   - For OpenRouter: live /api/v1/models `supported_parameters`
        #     includes "reasoning" → canonical 7-level ladder.
        #   - For NVIDIA / Cloudflare / PrivateModeAI / OpenCode: curated
        #     reasoning_catalog.json entry for (provider/model_id).
        #   - For GitHub Models: [] (native reasoning, no knob).
        # Result: every host entry carries its own authoritative effort_levels
        # list. The frontend's `selectedModelInfo` lookup (which matches by
        # slotId == "provider/raw_id") finds the right host and shows its
        # specific effort names.
        if _det_levels is not None:
            try:
                host_effort_levels = _det_levels(
                    provider_name, raw_id, logical=logical_id, family=family)
                if host_effort_levels:
                    attributes["effort_levels"] = list(host_effort_levels)
            except Exception:
                # Detector is best-effort; never break the catalog build
                # on a single model's effort detection error.
                pass

        if bm and not attributes.get("capabilities"):
            all_low = all(isinstance(v, (int, float)) and v < 20 for v in bm.values())
            if all_low:
                continue

        models.append({
            "id": logical_id,
            "displayName": fam_meta.get("display_name") or derive_display_name(logical_id),
            "contextLength": context_length,
            "family": family,
            "slotId": f"{provider_name}/{raw_id}",
        })
        if attributes:
            models[-1]["attributes"] = attributes

    return models


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_provider_catalog(force_refresh: bool = False) -> dict[str, Any]:
    """Build and return the full SyncResult JSON dict.

    Cache lives 10 minutes in-process. Concurrent callers block on the same
    build when the cache is being refreshed.
    """
    global _cache, _cache_at

    now = time.time()
    if not force_refresh and _cache is not None and now - _cache_at < _CACHE_TTL_S:
        return _cache

    with _cache_lock:
        if _cache is not None and not force_refresh and now - _cache_at < _CACHE_TTL_S:
            return _cache

        catalog = _load_catalog()
        display_map = _PROVIDER_DISPLAY

        # 1. Build family registry from OpenRouter
        or_models, family_registry = _fetch_openrouter_family()
        or_live = any(or_models)  # True if the fetch succeeded

        # 2. Sync provider model lists
        provider_models, live_set = _sync_provider_models(catalog)

        # 3. Build ProviderGroup for each catalog entry
        providers_list: list[dict[str, Any]] = []
        sync_status: list[dict[str, Any]] = []

        for entry in catalog:
            name = entry["name"]
            display = display_map.get(name, entry)
            model_ids = provider_models.get(name, {})
            is_live = name in live_set

            models = _build_provider_models(name, model_ids, family_registry)

            provider_group = {
                "name": name,
                "displayName": display.get("displayName", name),
                "pool": display.get("pool", "core"),
                "region": display.get("region", ""),
                "icon": display.get("icon", "Box"),
                "color": display.get("color", "#888"),
                "models": models,
                "privacy": display.get("privacy", {
                    "notice": "Privacy info not yet available.",
                    "confidence": "low",
                    "sources": [],
                }),
                "usageLimits": display.get("usageLimits", ""),
                "settingsUrl": display.get("settingsUrl", ""),
                "manageLabel": display.get("manageLabel", "Manage keys"),
                "syncedLive": is_live,
            }

            providers_list.append(provider_group)
            sync_status.append({
                "provider": name,
                "live": is_live,
                "error": None if is_live else "sync unavailable or returned no models",
                "modelCount": len(models),
            })

        total_models = sum(len(p["models"]) for p in providers_list)

        # 4. Build de-duped logical catalog from provider models
        logical_models = _build_logical_catalog(providers_list, family_registry, catalog)

        result: dict[str, Any] = {
            "providers": providers_list,
            "logical": logical_models,
            "totalModels": total_models,
            "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "syncStatus": sync_status,
        }

        _cache = result
        _cache_at = time.time()
        return result


# ---------------------------------------------------------------------------
# Logical (de-duped) catalog builder
# ---------------------------------------------------------------------------
def _build_logical_catalog(
    provider_groups: list[dict[str, Any]],
    family_registry: dict[str, dict[str, Any]],
    catalog_entries: list[dict],
) -> list[dict[str, Any]]:
    """Group all provider models by family into de-duped logical entries.

    Each logical entry carries a *hosts* list — every provider that serves
    this model, with per-host details and the provider's API key status.
    Attributes (capabilities, benchmarks, pricing) are merged from the
    OpenRouter family registry.
    """
    # Cache API-key presence per provider
    provider_keys: dict[str, bool] = {}
    for entry in catalog_entries:
        name = entry["name"]
        env_vars = entry.get("env_var", [])
        if isinstance(env_vars, str):
            env_vars = [env_vars]
        has_key = any(os.environ.get(v, "").strip() for v in env_vars if v)
        provider_keys[name] = has_key

    groups: dict[str, dict[str, Any]] = {}

    for pg in provider_groups:
        prov_name = pg["name"]
        disp = pg.get("displayName", prov_name)
        icon = pg.get("icon", "Box")
        color = pg.get("color", "#888")
        has_api_key = provider_keys.get(prov_name, False)
        synced_live = pg.get("syncedLive", False)

        for model in pg.get("models", []):
            family = model.get("family") or model["id"]
            if family not in groups:
                groups[family] = {
                    "logical": family,
                    "displayName": model["displayName"],
                    "family": family,
                    "contextLength": 0,
                    "hosts": [],
                }
            group = groups[family]

            group["hosts"].append({
                "provider": prov_name,
                "providerDisplayName": disp,
                "icon": icon,
                "color": color,
                "modelId": model["id"],
                "contextLength": model.get("contextLength", 0) or 0,
                "hasApiKey": has_api_key,
                "syncedLive": synced_live,
                "defaultPriority": len(group["hosts"]) + 1,
            })

            ctx = model.get("contextLength", 0) or 0
            if ctx > group["contextLength"]:
                group["contextLength"] = ctx

    # Merge family-registry attributes per group
    result: list[dict[str, Any]] = []
    for family in sorted(groups.keys()):
        group = groups[family]
        fam_meta = family_registry.get(family, {})
        attributes: dict[str, Any] = {}

        caps = fam_meta.get("capabilities")
        if caps:
            attributes["capabilities"] = caps
        bm = fam_meta.get("benchmarks")
        if bm:
            attributes["benchmarks"] = bm
        pricing = fam_meta.get("pricing")
        if pricing:
            attributes["pricing"] = pricing
        ranks = fam_meta.get("ranks")
        if ranks:
            attributes["ranks"] = ranks
        free_note = fam_meta.get("free_note")
        if free_note:
            attributes["note"] = free_note

        entry: dict[str, Any] = {
            "logical": group["logical"],
            "displayName": group["displayName"],
            "family": group["family"],
            "contextLength": group["contextLength"],
            "hosts": group["hosts"],
        }
        if attributes:
            entry["attributes"] = attributes
        result.append(entry)

    log_event("logical_catalog_built", count=len(result))
    return result


def _load_catalog() -> list[dict]:
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("providers", [])
    except (OSError, json.JSONDecodeError) as e:
        log_event("catalog_load_error", error=str(e)[:500])
        return []
