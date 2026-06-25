"""OpenCode Zen/Go model sync implementation."""

from __future__ import annotations

import os

from provider_sync.base import BaseSync, ModelInfo


class OpenCodeSync(BaseSync):
    requires_auth = False
    env_var = None

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.variant = kwargs.get("variant", "zen")
        if self.variant == "go":
            self.provider_name = "opencode-go"
            self.models_url = "https://opencode.ai/zen/go/v1/models"
            self.env_var = "OPENCODE_GO_API_KEY"
        else:
            self.provider_name = "opencode-zen"
            self.models_url = "https://opencode.ai/zen/v1/models"
            self.env_var = "OPENCODE_ZEN_API_KEY"

        if self.api_key is None:
            self.api_key = os.environ.get(self.env_var, "")

    def fetch_models(self) -> list[ModelInfo]:
        headers = self._build_auth_header() if self.api_key else None
        data = self._make_request(self.models_url, headers)
        if not data:
            return []

        models = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", "")
            if not model_id:
                continue

            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=model_id.endswith("-free"),
                context_length=None,
                metadata=item,
            ))
        return models

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        if self.variant == "go":
            return models
        return [m for m in models if m.is_free]

    def normalize_model_id(self, model_id: str) -> str:
        return model_id