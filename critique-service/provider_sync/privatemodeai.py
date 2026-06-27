"""PrivateMode AI model sync implementation.

PrivateMode AI (Edgeless Systems) provides confidential-computing-protected AI
inference via a local proxy that handles E2E encryption + remote attestation.
"""

from __future__ import annotations

from provider_sync.base import BaseSync, ModelInfo

# Free-tier chat models offered by PrivateMode. Speech/embedding models are
# excluded from sync since doomalaysocreate only routes chat completions.
_FREE_CHAT_MODELS = frozenset({
    "kimi-k2.6",
    "kimi-latest",
    "gemma-4-31b",
    "gpt-oss-120b",
})


class PrivateModeAISync(BaseSync):
    provider_name = "privatemodeai"
    models_url = "http://localhost:8080/v1/models"
    requires_auth = False
    env_var = "PRIVATEMODEAI_API_KEY"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)

    def fetch_models(self) -> list[ModelInfo]:
        data = self._make_request(self.models_url)
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
            context = item.get("max_context_length") or item.get("context_length")
            if context:
                try:
                    context = int(context)
                except (ValueError, TypeError):
                    context = None

            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=True,
                context_length=context,
                metadata=item,
            ))
        return models

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return [m for m in models if m.id in _FREE_CHAT_MODELS]

    def normalize_model_id(self, model_id: str) -> str:
        return model_id
