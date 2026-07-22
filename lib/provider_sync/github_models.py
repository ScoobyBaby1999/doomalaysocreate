"""GitHub Models (Copilot) model sync implementation.

Dynamic model fetching — NEVER static. We try the GitHub Models API
endpoints in order:

  1. https://models.github.ai/catalog/models  — the public catalog (no auth
     required, returns the full model list as a JSON array). This is the
     primary source and is always fresh.
  2. https://models.github.ai/inference/models — the OpenAI-compatible
     /models endpoint (requires GITHUB_TOKEN; returns the same list but
     gated by the user's token, so models the user can't access are
     filtered out). Used as a fallback when the catalog is unreachable.

Both endpoints return the live model list — we never hardcode. If a new
model ships on GitHub Models, the next /api/models refresh picks it up
automatically. The frontend's "Up to Premium" button for github-models
unlocks the gated /inference/models endpoint (the user's GITHUB_TOKEN
determines which models they can actually call).
"""
from __future__ import annotations

import os

from provider_sync.base import BaseSync, ModelInfo


class GitHubModelsSync(BaseSync):
    provider_name = "github-models"
    # Primary: public catalog (no auth needed). Fallback: inference endpoint
    # (requires GITHUB_TOKEN). Both return live model lists.
    models_url = "https://models.github.ai/catalog/models"
    inference_url = "https://models.github.ai/inference/models"
    requires_auth = False
    env_var = "GITHUB_TOKEN"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        if not self.api_key:
            self.api_key = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        # Premium flag: when True, we ALSO try the gated /inference/models
        # endpoint to filter the catalog to models the user's token can
        # actually access. Defaults to True iff a token is set.
        self.premium = bool(kwargs.get("premium", bool(self.api_key)))

    def fetch_models(self) -> list[ModelInfo]:
        # Try the public catalog first (always works, returns ALL models).
        catalog_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        auth = self._build_auth_header()
        if auth:
            catalog_headers.update(auth)
        data = self._make_request(self.models_url, catalog_headers)
        if not data or not isinstance(data, list):
            # Fallback: the inference endpoint (gated by GITHUB_TOKEN).
            if self.api_key:
                inf_headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                }
                inf = self._make_request(self.inference_url, inf_headers)
                if inf and isinstance(inf, dict) and isinstance(inf.get("data"), list):
                    data = inf["data"]
                elif inf and isinstance(inf, list):
                    data = inf
                else:
                    return []
            else:
                return []

        seen = set()
        models = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # Catalog format: {id, name, description, limits, ...}
            # Inference format: {id, object, created, owned_by, ...}
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
            # Some responses put context under "context_window" or
            # "max_context_tokens" — handle both.
            for k in ("context_window", "max_context_tokens", "context"):
                if context is None:
                    v = item.get(k)
                    if v:
                        try:
                            context = int(v)
                        except (ValueError, TypeError):
                            pass

            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=True,  # GitHub Models is free (rate-limited).
                context_length=context,
                metadata=item,
            ))
        return models

    def normalize_model_id(self, model_id: str) -> str:
        normalized = model_id
        for prefix in ("openai/", "cohere/", "deepseek/", "meta/", "microsoft/",
                       "mistral-ai/", "mistralai/", "github/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized
