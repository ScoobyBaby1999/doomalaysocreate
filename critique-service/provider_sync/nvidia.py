"""NVIDIA NIM model sync implementation.

Fetches model list from integrate.api.nvidia.com/v1/models (121 models, no auth
needed for listing), then scrapes build.nvidia.com to determine which models have
free endpoints. The website shows "Free Endpoint" badges on model cards and
supports ?page=1&pageSize=1000 to return all 140 models in one response.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from provider_sync.base import BaseSync, ModelInfo

_WEBSITE_URL = "https://build.nvidia.com/models?page=1&pageSize=1000"
_PAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class NvidiaSync(BaseSync):
    provider_name = "nvidia"
    models_url = "https://integrate.api.nvidia.com/v1/models"
    requires_auth = True
    env_var = "NVIDIA_API_KEY"

    def fetch_models(self) -> list[ModelInfo]:
        headers = self._build_auth_header()
        data = self._make_request(self.models_url, headers)
        if not data:
            return []

        website_free, website_all = self._scrape_website_slugs()

        seen: set[str] = set()
        models: list[ModelInfo] = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)

            is_free = self._check_free(
                model_id, item.get("owned_by", ""),
                website_free, website_all,
            )
            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=is_free,
                context_length=None,
                metadata=item,
            ))
        return models

    def _scrape_website_slugs(self) -> tuple[set[str], set[str]]:
        """Scrape build.nvidia.com and return (free_slugs, all_slugs)."""
        req = urllib.request.Request(
            _WEBSITE_URL,
            headers={"User-Agent": _PAGE_UA, "Accept": "text/html"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8")
        except Exception:
            return set(), set()

        free_slugs: set[str] = set()
        all_slugs: set[str] = set()
        card_re = re.compile(
            r'<div hidden id="S:([^"]+)"(.*?)\$RS\("S:\1","P:\1"\)</script>',
            re.DOTALL,
        )
        for match in card_re.finditer(content):
            card = match.group(0)
            slug = self._extract_slug(card)
            if not slug:
                continue
            all_slugs.add(slug)
            if "Free Endpoint" in card:
                free_slugs.add(slug)
        return free_slugs, all_slugs

    @staticmethod
    def _extract_slug(card_html: str) -> str | None:
        m = re.search(
            r'data-nvtrack-nav-object="artifact-card"[^>]*'
            r'data-nvtrack-nav-object-label="([^"]+)"',
            card_html,
        )
        if m:
            return m.group(1)
        m = re.search(
            r'data-nvtrack-nav-object-label="([^"]+)"[^>]*class="linkbox-overlay"',
            card_html,
        )
        return m.group(1) if m else None

    @staticmethod
    def _match_slug(api_slug: str, website_slug: str) -> bool:
        """Check if an API slug matches a website slug with normalization."""
        if api_slug == website_slug:
            return True
        an = api_slug.replace("_", ".")
        wn = website_slug.replace("_", ".")
        if an == wn:
            return True
        if api_slug.replace(".", "_") == website_slug:
            return True
        if an.startswith(wn) or wn.startswith(an):
            return True
        return False

    @staticmethod
    def _check_free(
        model_id: str,
        owned_by: str,
        free_slugs: set[str],
        all_slugs: set[str],
    ) -> bool:
        """Determine if a model is free using website data + heuristic fallback.

        Matching priority:
        1. Slug matches a website model → use website free/paid badge
        2. No website match & nvidia-owned → free (old models were all free)
        3. No website match & other-owned → paid (conservative)
        """
        slug = model_id.split("/")[-1]

        for ws in all_slugs:
            if NvidiaSync._match_slug(slug, ws):
                return ws in free_slugs

        if "nvidia" in owned_by.lower():
            return True

        return False

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return [m for m in models if m.is_free]

    def normalize_model_id(self, model_id: str) -> str:
        normalized = model_id
        for prefix in (
            "nvidia/", "meta-llama/", "meta/", "google/", "mistralai/",
            "mistral/", "microsoft/", "openai/", "deepseek-ai/",
            "cohere/", "cohere-ai/", "qwen/", "z-ai/", "minimax/",
            "arcee-ai/", "nousresearch/", "01-ai/", "ibm/", "gradio/",
        ):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized
