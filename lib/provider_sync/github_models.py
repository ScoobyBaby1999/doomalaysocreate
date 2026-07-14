"""GitHub Models model sync implementation.

GitHub Models provides free (rate-limited) access to models from OpenAI,
Meta, Mistral, DeepSeek, Cohere, and Microsoft. The catalog endpoint at
https://models.github.ai/catalog/models returns all available models
without requiring authentication (richer response with GITHUB_TOKEN).
"""

from __future__ import annotations

import os

from provider_sync.base import BaseSync, ModelInfo


class GitHubModelsSync(BaseSync):
    provider_name = "github-models"
    models_url = "https://models.github.ai/catalog/models"
    requires_auth = False
    env_var = "GITHUB_TOKEN"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        if not self.api_key:
            self.api_key = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def fetch_models(self) -> list[ModelInfo]:
        headers = self._build_auth_header()
        if headers:
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2026-03-10"
        data = self._make_request(self.models_url, headers)
        if not data or not isinstance(data, list):
            return []

        seen = set()
        models = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)

            context = None
            limits = item.get("limits") or {}
            if isinstance(limits, dict):
                ctx = limits.get("max_input_tokens")
                if ctx:
                    try:
                        context = int(ctx)
                    except (ValueError, TypeError):
                        pass

            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=True,
                context_length=context,
                metadata=item,
            ))
        return models

    def normalize_model_id(self, model_id: str) -> str:
        normalized = model_id
        for prefix in ("openai/", "cohere/", "deepseek/", "meta/", "microsoft/",
                       "mistral-ai/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized
