"""Base classes and common utilities for provider model sync."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from oplog import log_event

log = logging.getLogger(__name__)

_FETCH_TIMEOUT = 15
_USER_AGENT = "doomalaysocreate/1.0"


@dataclass
class ModelInfo:
    id: str
    normalized_id: str
    is_free: bool = False
    context_length: int | None = None
    metadata: dict = field(default_factory=dict)


class BaseSync(ABC):
    provider_name: str
    models_url: str
    requires_auth: bool = True
    env_var: str | None = None
    extra_headers: dict = {}

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        self.api_key = api_key
        self.extra_headers = kwargs.get("extra_headers", {})

    @abstractmethod
    def fetch_models(self) -> list[ModelInfo]:
        """Fetch model list from provider API. Must be implemented by subclass."""

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        """Override to filter for free-tier models. Default: return all."""
        return models

    def normalize_model_id(self, model_id: str) -> str:
        """Override for provider-specific normalization. Default: return as-is."""
        return model_id

    def _make_request(self, url: str, headers: dict | None = None) -> dict | None:
        """Make HTTP request with standard headers and error handling."""
        request_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)

        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            log_event(
                "provider_sync_error",
                provider=self.provider_name,
                status=e.code,
                error=body[:500],
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            log_event(
                "provider_sync_error",
                provider=self.provider_name,
                error=str(e)[:500],
            )
        return None

    def _build_auth_header(self) -> dict | None:
        """Build Authorization header if API key available."""
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return None

    def sync(self) -> list[str]:
        """Public sync method: fetch, filter, normalize, return model IDs."""
        raw_models = self.fetch_models()
        if not raw_models:
            log_event("provider_sync_empty", provider=self.provider_name)
            return []

        filtered = self.filter_free_models(raw_models)
        normalized = [self.normalize_model_id(m.normalized_id) for m in filtered]

        seen = set()
        deduped = []
        for m in normalized:
            if m not in seen:
                seen.add(m)
                deduped.append(m)

        log_event(
            "provider_sync_ok",
            provider=self.provider_name,
            total=len(raw_models),
            filtered=len(filtered),
            final=len(deduped),
        )
        return deduped


def get_provider_sync_class(provider_name: str) -> type[BaseSync] | None:
    """Get sync class for a provider name. Lazy imports to avoid circular deps."""
    sync_map = {
        "openrouter": "provider_sync.openrouter.OpenRouterSync",
        "cloudflare": "provider_sync.cloudflare.CloudflareSync",
        "nvidia": "provider_sync.nvidia.NvidiaSync",
        "opencode-zen": "provider_sync.opencode.OpenCodeSync",
        "opencode-go": "provider_sync.opencode.OpenCodeSync",
    }
    path = sync_map.get(provider_name)
    if not path:
        return None
    module_name, class_name = path.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)