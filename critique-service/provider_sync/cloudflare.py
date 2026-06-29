"""Cloudflare Workers AI model sync implementation."""

from __future__ import annotations

import logging
import os
import re
import urllib.request
from provider_sync.base import BaseSync, ModelInfo
from oplog import log_event

log = logging.getLogger(__name__)

_cached_cloudflare_models: list[ModelInfo] | None = None


class CloudflareSync(BaseSync):
    provider_name = "cloudflare"
    requires_auth = False
    env_var = "CF_API_TOKEN"

    DOCS_URL = "https://developers.cloudflare.com/workers-ai/models/index.md"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.account_id = kwargs.get("account_id") or kwargs.get("cf_account_id") or os.environ.get("CF_ACCOUNT_ID", "")
        self.models_url = None
        if self.account_id:
            self.models_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/models/search"
        self.hide_experimental = kwargs.get("hide_experimental", False)
        self.include_deprecated = kwargs.get("include_deprecated", False)

    def fetch_models(self) -> list[ModelInfo]:
        global _cached_cloudflare_models
        models = self._fetch_from_docs()
        if not models:
            models = self._fetch_from_api()
        if models:
            _cached_cloudflare_models = models
        elif _cached_cloudflare_models is not None:
            models = _cached_cloudflare_models
        return models

    def _fetch_from_api(self) -> list[ModelInfo]:
        if not self.models_url:
            return []
        headers = self._build_auth_header()
        models: list[ModelInfo] = []
        seen: set[str] = set()
        page = 1
        total_pages = 1

        while page <= total_pages:
            params = []
            if not self.hide_experimental:
                params.append("hide_experimental=false")
            if self.include_deprecated:
                params.append("include_deprecated=true")
            params.append(f"per_page=500")
            params.append(f"page={page}")

            url = f"{self.models_url}?{'&'.join(params)}"
            data = self._make_request(url, headers)
            if not data or not data.get("success"):
                return []

            result_info = data.get("result_info", {})
            total_count = result_info.get("total_count", 0)
            per_page = result_info.get("per_page", 500)
            total_pages = -(-total_count // per_page) if total_count > 0 else 1

            for item in data.get("result", []):
                if not isinstance(item, dict):
                    continue
                model_id = item.get("name", "") or item.get("id", "")
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)

                normalized = self.normalize_model_id(model_id)
                models.append(ModelInfo(
                    id=model_id,
                    normalized_id=normalized,
                    is_free=True,
                    context_length=None,
                    metadata=item,
                ))

            page += 1

        return models

    def _fetch_from_docs(self) -> list[ModelInfo]:
        try:
            req = urllib.request.Request(self.DOCS_URL, headers={"User-Agent": "doomalaysocreate/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8")
        except Exception as exc:
            log_event("cloudflare_docs_fetch_failed", url=self.DOCS_URL, error=str(exc)[:300])
            log.warning("Cloudflare docs fetch failed: %s", exc)
            return []

        model_ids: list[str] = list(dict.fromkeys(
            m.rstrip("/") for m in re.findall(r'/workers-ai/models/([a-z0-9][a-z0-9._-]+)/', content)
        ))

        if not model_ids:
            return []

        models: list[ModelInfo] = []
        seen: set[str] = set()
        for model_id in model_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=True,
                context_length=None,
                metadata={},
            ))
        return models

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return models

    def normalize_model_id(self, model_id: str) -> str:
        return model_id