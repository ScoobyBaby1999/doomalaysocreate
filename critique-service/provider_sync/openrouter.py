"""OpenRouter model sync implementation."""

from __future__ import annotations

from provider_sync.base import BaseSync, ModelInfo


class OpenRouterSync(BaseSync):
    provider_name = "openrouter"
    models_url = "https://openrouter.ai/api/v1/models"
    requires_auth = True
    env_var = "OPENROUTER_API_KEY"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.output_modalities = kwargs.get("output_modalities", "text")

    def fetch_models(self) -> list[ModelInfo]:
        url = f"{self.models_url}?output_modalities={self.output_modalities}"
        headers = self._build_auth_header()
        data = self._make_request(url, headers)
        if not data:
            return []

        models = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", "")
            if not model_id:
                continue

            pricing = item.get("pricing", {}) or {}
            prompt_price = float(pricing.get("prompt", "0") or "0")
            completion_price = float(pricing.get("completion", "0") or "0")
            is_free = (prompt_price == 0 and completion_price == 0) or model_id.endswith(":free")

            context = item.get("context_length")
            if context:
                try:
                    context = int(context)
                except (ValueError, TypeError):
                    context = None

            models.append(ModelInfo(
                id=model_id,
                normalized_id=self.normalize_model_id(model_id),
                is_free=is_free,
                context_length=context,
                metadata=item,
            ))
        return models

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return [m for m in models if m.is_free]

    def normalize_model_id(self, model_id: str) -> str:
        normalized = model_id
        for prefix in ("openai/", "anthropic/", "google/", "meta-llama/", "meta/",
                       "mistralai/", "mistral/", "nousresearch/", "arcee-ai/",
                       "qwen/", "z-ai/", "nvidia/", "minimax/", "deepseek-ai/",
                       "cohere/", "cohere-ai/", "x-ai/", "gpt/", "claude/",
                       "gemini/", "llama/", "nemotron/", "openrouter/",
                       "poolside/", "cognitivecomputations/", "liquid/",
                       "hermes/", "north/", "lyria/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized