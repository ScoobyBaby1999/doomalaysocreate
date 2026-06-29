"""PrivateMode AI model sync implementation.

PrivateMode AI (Edgeless Systems) provides confidential-computing-protected AI
inference via a local proxy that handles E2E encryption + remote attestation.

All models discovered dynamically — first from the local proxy API, falling
back to doc page scraping. No static model lists.

Filters out `-latest` model variants when a more specific model exists
(e.g. ``kimi-latest`` removed when ``kimi-k2.6`` is present).
"""

from __future__ import annotations

import re
import urllib.request

from provider_sync.base import BaseSync, ModelInfo

_COMMON_WORDS = frozenset({"dimensions"})


class PrivateModeAISync(BaseSync):
    provider_name = "privatemodeai"
    models_url = "http://localhost:8080/v1/models"
    DOCS_URL = "https://docs.privatemode.ai/models/overview/"
    requires_auth = False
    env_var = "PRIVATEMODEAI_API_KEY"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)

    def fetch_models(self) -> list[ModelInfo]:
        models = self._fetch_from_api()
        if models:
            return self._dedup_latest(models)
        return self._dedup_latest(self._fetch_from_docs())

    def _dedup_latest(self, models: list[ModelInfo]) -> list[ModelInfo]:
        """Remove `-latest` model variants when a more specific model exists.
        
        E.g. if both ``kimi-latest`` and ``kimi-k2.6`` are in the list, the
        ``kimi-latest`` entry is removed since ``kimi-k2.6`` carries more detail.
        """
        model_ids = {m.id for m in models}
        keep: list[ModelInfo] = []
        for m in models:
            if not m.id.endswith("-latest"):
                keep.append(m)
                continue
            base = m.id[:-7]
            if base in model_ids:
                continue
            if any(other != m.id and other.startswith(base) for other in model_ids):
                continue
            keep.append(m)
        return keep

    def _fetch_from_api(self) -> list[ModelInfo]:
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

    def _fetch_from_docs(self) -> list[ModelInfo]:
        try:
            req = urllib.request.Request(self.DOCS_URL, headers={"User-Agent": "doomalaysocreate/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8")
        except Exception:
            return []

        model_ids: list[str] = []
        for match in re.finditer(r'<code[^>]*>([^<]+)</code>', content):
            mid = match.group(1).strip()
            if not mid or mid in _COMMON_WORDS:
                continue
            if not re.match(r'^[a-z][a-z0-9._-]+$', mid):
                continue
            if mid not in model_ids:
                model_ids.append(mid)

        if not model_ids:
            return []

        models = []
        seen = set()
        for model_id in model_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=self.normalize_model_id(model_id),
                is_free=True,
                context_length=None,
                metadata={},
            ))
        return models

    def normalize_model_id(self, model_id: str) -> str:
        return model_id
