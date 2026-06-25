"""Cloudflare Workers AI model sync implementation."""

from __future__ import annotations

import os

from provider_sync.base import BaseSync, ModelInfo


class CloudflareSync(BaseSync):
    provider_name = "cloudflare"
    requires_auth = True
    env_var = "CF_API_TOKEN"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.account_id = kwargs.get("account_id") or kwargs.get("cf_account_id") or os.environ.get("CF_ACCOUNT_ID", "")
        if not self.account_id:
            raise ValueError("CF_ACCOUNT_ID required for Cloudflare sync")
        self.models_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/models/search"
        self.hide_experimental = kwargs.get("hide_experimental", False)
        self.include_deprecated = kwargs.get("include_deprecated", False)

    def fetch_models(self) -> list[ModelInfo]:
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
            params.append(f"per_page=200")
            params.append(f"page={page}")

            url = f"{self.models_url}?{'&'.join(params)}"
            data = self._make_request(url, headers)
            if not data or not data.get("success"):
                break

            result_info = data.get("result_info", {})
            total_count = result_info.get("total_count", 0)
            per_page = result_info.get("per_page", 200)
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

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return models

    def normalize_model_id(self, model_id: str) -> str:
        return model_id