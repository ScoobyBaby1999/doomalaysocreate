"""OpenRouter model sync implementation.

Dynamic model fetching — NEVER static. The /api/v1/models endpoint returns
ALL ~400 models (free + paid). Premium behaviour:

  * **Default (free tier):** only the ``:free`` models are returned. This is
    the safe default — having an OPENROUTER_API_KEY set does NOT imply the
    user has paid credit on OpenRouter (anyone can sign up for a free key),
    so we surface only the models that will not 401/402 when called.
  * **Premium tier:** when the user has explicitly opted into premium mode
    (env var ``OPENROUTER_PREMIUM=1`` OR an explicit ``premium=True`` kwarg
    from the caller), ALL ~400 models are returned so the upgraded user can
    pick any paid model.

The premium flag is read once per :class:`OpenRouterSync` instance. The
catalog builder (``provider_sync.catalog``) reads the env var when it
constructs the sync instance and also passes it down to the family-fetch
helper so the public roster endpoint respects the same flag.
"""
from __future__ import annotations

import os

from provider_sync.base import BaseSync, ModelInfo


def _env_premium() -> bool:
    """Read the premium flag from the environment.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Everything else (including unset) is False.
    """
    raw = os.environ.get("OPENROUTER_PREMIUM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


class OpenRouterSync(BaseSync):
    provider_name = "openrouter"
    models_url = "https://openrouter.ai/api/v1/models"
    requires_auth = False  # /v1/models is public; only /chat/completions needs a key.
    env_var = "OPENROUTER_API_KEY"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.output_modalities = kwargs.get("output_modalities", "text")
        # Premium flag resolution (in priority order):
        #   1. Explicit kwarg from the caller (catalog builder / tests).
        #   2. Env var OPENROUTER_PREMIUM (set by the frontend's "Upgrade to
        #      Premium" flow via the Space secret mirror).
        #   3. Default False — only :free models are returned.
        #
        # IMPORTANT: having an api_key alone does NOT enable premium. A free
        # OpenRouter key is enough to call /v1/models but paid routes would
        # 401/402 if the user has no credit. Default to the safe free-only
        # behaviour and let the user opt in explicitly.
        if "premium" in kwargs:
            self.premium = bool(kwargs.get("premium"))
        else:
            self.premium = _env_premium()

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
            try:
                prompt_price = float(pricing.get("prompt", "0") or "0")
            except (ValueError, TypeError):
                prompt_price = 0.0
            try:
                completion_price = float(pricing.get("completion", "0") or "0")
            except (ValueError, TypeError):
                completion_price = 0.0
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
        # Premium: return ALL models (free + paid). Only enabled when the
        # user has explicitly opted in (env var OPENROUTER_PREMIUM=1 or an
        # explicit premium=True kwarg). Without explicit opt-in we keep
        # only the :free routes so the panel doesn't try to call paid
        # models that will 401/402.
        if self.premium:
            return list(models)
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
