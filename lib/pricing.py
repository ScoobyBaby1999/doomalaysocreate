"""Live pricing endpoint — OpenRouter pricing + local catalog merge.

Ports the Next.js ``/api/pricing`` endpoint:

  * GET /api/pricing  — returns:
      {
        "pricing": { "<provider>/<model>": {inputPerM, outputPerM, source, free},
                     ... },
        "liveOpenRouter": {<openrouter id>: {input, output, ...}, ...},
        "liveStale": bool,
        "cachedAt": ISO timestamp
      }

OpenRouter pricing is fetched from ``https://openrouter.ai/api/v1/models``
and cached in-memory for 10 minutes. The local ``providers_catalog.json``
contributes pricing for providers that aren't on OpenRouter (NVIDIA NIM,
Cloudflare Workers AI, GitHub Models, PrivateMode AI, OpenCode Zen/Go).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

# 10-minute cache TTL — matches the Next.js implementation.
CACHE_TTL_S = 600.0

_lock = threading.Lock()
_cache: dict | None = None
_cache_ts: float = 0.0

HERE = Path(__file__).resolve().parent
PROVIDERS_CATALOG_PATH = Path(os.environ.get("PROVIDERS_CATALOG", HERE / "providers_catalog.json"))


def _load_local_pricing() -> dict[str, dict]:
    """Read providers_catalog.json and return {provider_name: pricing_dict}.

    Each provider entry may carry a 'pricing' object:
        {"input_per_m": <float>, "output_per_m": <float>, "free": <bool>}
    """
    try:
        raw = PROVIDERS_CATALOG_PATH.read_text(encoding="utf-8")
        no_comments = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("//"))
        data = json.loads(no_comments)
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("providers", []):
        name = entry.get("name")
        if not name:
            continue
        pricing = entry.get("pricing") or {}
        if not pricing:
            # Most providers in this catalog are free-tier; assume free unless
            # pricing is explicitly set. We still surface them with 0/0.
            pricing = {"input_per_m": 0.0, "output_per_m": 0.0, "free": True}
        out[name] = {
            "inputPerM": float(pricing.get("input_per_m", 0.0) or 0.0),
            "outputPerM": float(pricing.get("output_per_m", 0.0) or 0.0),
            "free": bool(pricing.get("free", False)),
            "source": "catalog",
        }
    return out


def _fetch_openrouter_pricing() -> tuple[dict, bool]:
    """Fetch live OpenRouter pricing. Returns (parsed_dict, stale_flag).

    The stale flag is True when the fetch failed and we're returning the
    last-known-good cache (or an empty dict if no cache exists).
    """
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if _cache is not None and (now - _cache_ts) < CACHE_TTL_S:
            return _cache, False
    # Cache miss — fetch fresh.
    live: dict = {}
    try:
        # Use a sync httpx client because this runs in the HTTP handler thread.
        # The OpenRouter /api/v1/models endpoint is public (no auth needed).
        with httpx.Client(timeout=15.0) as client:
            r = client.get("https://openrouter.ai/api/v1/models",
                            headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            pricing = m.get("pricing", {}) or {}
            prompt = float(pricing.get("prompt", "0") or "0")
            completion = float(pricing.get("completion", "0") or "0")
            live[mid] = {
                "inputPerM": round(prompt * 1_000_000, 6),
                "outputPerM": round(completion * 1_000_000, 6),
                "free": prompt == 0.0 and completion == 0.0,
                "source": "openrouter",
                "context_length": m.get("context_length", 0),
                "name": m.get("name", ""),
            }
        with _lock:
            _cache = live
            _cache_ts = now
        return live, False
    except Exception:
        # Fetch failed — return the stale cache if we have one.
        with _lock:
            if _cache is not None:
                return _cache, True
            return {}, True


def get_pricing() -> dict:
    """Build the full pricing response: catalog + live OpenRouter merged.

    Returns:
        {
          "pricing": {key: {inputPerM, outputPerM, source, free}, ...},
          "liveOpenRouter": {<id>: {...}, ...},
          "liveStale": bool,
          "cachedAt": <iso ts>,
        }
    """
    live, stale = _fetch_openrouter_pricing()
    local = _load_local_pricing()

    # Merge: every local provider keyed by name, every OpenRouter model keyed
    # by its openrouter id. For an OpenRouter model that matches a local
    # provider's model id, prefer the OpenRouter live price (it's fresher and
    # more specific) but tag it with both sources.
    merged: dict[str, dict] = {}
    # 1) Local catalog first.
    for name, p in local.items():
        merged[name] = p
    # 2) OpenRouter live pricing (keyed by full openrouter id).
    for mid, p in live.items():
        merged[f"openrouter/{mid}"] = p

    return {
        "pricing": merged,
        "liveOpenRouter": live,
        "liveStale": stale,
        "cachedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_cache_ts or time.time())),
        "cacheTtlS": int(CACHE_TTL_S),
        "localProviders": sorted(local.keys()),
    }


def clear_cache() -> None:
    """Force a fresh fetch on the next get_pricing() call."""
    global _cache, _cache_ts
    with _lock:
        _cache = None
        _cache_ts = 0.0
