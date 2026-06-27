"""Provider model sync package - unified auto-sync for all providers."""

from __future__ import annotations

import os
from typing import Any

from providers import load_provider_catalog, provider
from provider_sync.base import BaseSync, get_provider_sync_class


def sync_all_providers(registered_providers: dict[str, provider]) -> dict[str, list[str]]:
    """Fetch live model lists from all registered providers that support sync.

    Args:
        registered_providers: Dict of provider_name -> provider instance from Panel.provider_by_name

    Returns:
        Dict mapping provider_name -> list of raw model IDs (e.g. ``deepseek-ai/deepseek-v4-pro``).
        Slots are created with these raw IDs so call_slot sends the correct model name.
    """
    results: dict[str, list[str]] = {}
    catalog = load_provider_catalog()

    for entry in catalog:
        name = entry["name"]
        if name not in registered_providers:
            continue

        prov = registered_providers[name]
        sync_config = entry.get("sync_config", {})
        if not sync_config.get("enabled", True):
            continue

        sync_class = get_provider_sync_class(name)
        if not sync_class:
            continue

        try:
            api_key = getattr(prov, "api_key", None)
            params = dict(sync_config.get("params", {}))
            for req in entry.get("requires", []):
                val = os.environ.get(req, "").strip()
                if val:
                    params[req.lower()] = val
            sync_instance = sync_class(api_key=api_key, **params)
            models = sync_instance.sync()
            results[name] = models
        except Exception as e:
            from oplog import log_event
            log_event("provider_sync_error", provider=name, error=str(e)[:500])
            results[name] = []

    return results


def register_synced_models(panel: Any, sync_results: dict[str, list[str]]) -> None:
    """Register newly discovered models as extra slots in the scheduler.

    sync_results contains RAW model IDs (with author prefixes like
    ``deepseek-ai/deepseek-v4-pro``) so call_slot sends the exact model name
    the provider API expects.  Models already registered via models_catalog.json
    (same provider/raw-id key) are skipped.
    """
    for provider_name, model_ids in sync_results.items():
        if not model_ids:
            continue
        for model_id in model_ids:
            slot_key = f"{provider_name}/{model_id}"
            if slot_key not in panel.slot_by_who:
                panel._ensure_slot(slot_key)