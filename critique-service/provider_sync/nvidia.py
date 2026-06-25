"""NVIDIA NIM model sync implementation."""

from __future__ import annotations

from provider_sync.base import BaseSync, ModelInfo


class NvidiaSync(BaseSync):
    provider_name = "nvidia"
    models_url = "https://integrate.api.nvidia.com/v1/models"
    requires_auth = True
    env_var = "NVIDIA_API_KEY"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)

    def fetch_models(self) -> list[ModelInfo]:
        headers = self._build_auth_header()
        data = self._make_request(self.models_url, headers)
        if not data:
            return []

        seen = set()
        models = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)

            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=False,
                context_length=None,
                metadata=item,
            ))
        return models

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return models

    def normalize_model_id(self, model_id: str) -> str:
        normalized = model_id
        for prefix in ("nvidia/", "meta-llama/", "meta/", "google/", "mistralai/",
                       "mistral/", "microsoft/", "openai/", "deepseek-ai/",
                       "cohere/", "cohere-ai/", "qwen/", "z-ai/", "minimax/",
                       "arcee-ai/", "nousresearch/", "01-ai/", "ibm/", "gradio/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized