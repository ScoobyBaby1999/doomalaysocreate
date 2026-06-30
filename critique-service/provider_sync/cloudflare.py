"""Cloudflare Workers AI model sync implementation."""

from __future__ import annotations

import json
import logging
import os
import re
from provider_sync.base import BaseSync, ModelInfo
from oplog import log_event

log = logging.getLogger(__name__)


def _ssl_insecure_context():
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except Exception:
        return None


_cached_cloudflare_models: list[ModelInfo] | None = None


class CloudflareSync(BaseSync):
    provider_name = "cloudflare"
    requires_auth = False
    env_var = "CF_API_TOKEN"

    DOCS_URL = "https://developers.cloudflare.com/workers-ai/models/index.md"

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        if not self.api_key:
            self.api_key = os.environ.get("CF_API_TOKEN", "")
        self.account_id = kwargs.get("account_id") or kwargs.get("cf_account_id") or os.environ.get("CF_ACCOUNT_ID", "")
        self.models_url = None
        if self.account_id:
            self.models_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/models/search"
        self.hide_experimental = kwargs.get("hide_experimental", False)
        self.include_deprecated = kwargs.get("include_deprecated", False)

    def fetch_models(self) -> list[ModelInfo]:
        global _cached_cloudflare_models
        models = self._fetch_from_docs()
        source = "docs"
        if not models:
            models = self._fetch_from_api()
            source = "api"
        if models:
            _cached_cloudflare_models = models
            log_event("cloudflare_sync_ok", source=source, count=len(models))
        elif _cached_cloudflare_models is not None:
            models = _cached_cloudflare_models
            log_event("cloudflare_sync_cached", count=len(models))
        else:
            log_event("cloudflare_sync_failed", source=source)
        return models

    def _fetch_from_api(self) -> list[ModelInfo]:
        if not self.models_url:
            return []
        headers = self._build_auth_header()
        models: list[ModelInfo] = []
        seen: set[str] = set()
        page = 1
        total_pages = 1
        _FETCH_TIMEOUT = 15

        while page <= total_pages:
            params = []
            if not self.hide_experimental:
                params.append("hide_experimental=false")
            if self.include_deprecated:
                params.append("include_deprecated=true")
            params.append(f"per_page=500")
            params.append(f"page={page}")

            url = f"{self.models_url}?{'&'.join(params)}"
            data = self._make_request(url, headers)
            if not data or not data.get("success"):
                return []

            result_info = data.get("result_info", {})
            total_count = result_info.get("total_count", 0)
            per_page = result_info.get("per_page", 500)
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

    def _fetch_from_docs(self) -> list[ModelInfo]:
        content = self._fetch_docs_content(self.DOCS_URL)
        if not content:
            return []
        model_ids: list[str] = list(dict.fromkeys(
            m.rstrip("/") for m in re.findall(r'/workers-ai/models/([a-z0-9][a-z0-9._-]+)/', content)
        ))
        if not model_ids:
            return []
        models: list[ModelInfo] = []
        seen: set[str] = set()
        for model_id in model_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            normalized = self.normalize_model_id(model_id)
            models.append(ModelInfo(
                id=model_id,
                normalized_id=normalized,
                is_free=True,
                context_length=None,
                metadata={},
            ))
        return models

    def _fetch_docs_content(self, url: str) -> str | None:
        try:
            import httpx
            try:
                resp = httpx.get(url, headers={"User-Agent": "doomalaysocreate/1.0"}, timeout=30, verify=True)
                resp.raise_for_status()
                return resp.text
            except Exception:
                resp = httpx.get(url, headers={"User-Agent": "doomalaysocreate/1.0"}, timeout=30, verify=False)
                resp.raise_for_status()
                return resp.text
        except ImportError:
            pass
        try:
            import urllib.request
            ctx = _ssl_insecure_context()
            req = urllib.request.Request(url, headers={"User-Agent": "doomalaysocreate/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:
            log_event("cloudflare_docs_fetch_failed", url=url, error=str(exc)[:300])
            log.warning("Cloudflare docs fetch failed: %s", exc)
            return None

    def _make_request(self, url: str, headers: dict | None = None) -> dict | None:
        """Override: Cloudflare API requires httpx (urllib fails on TLS/HTTP2)."""
        request_headers = {
            "User-Agent": "doomalaysocreate/1.0",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        try:
            import httpx
            resp = httpx.get(url, headers=request_headers, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            pass
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            log_event("cloudflare_api_error", provider="cloudflare",
                      status=e.response.status_code, url=url[:120], error=body)
        except Exception as e:
            log_event("cloudflare_api_error", provider="cloudflare",
                      url=url[:120], error=str(e)[:300])
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            log_event("cloudflare_api_fallback_error", provider="cloudflare",
                      url=url[:120], error=str(e)[:300])
        return None

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        return models

    def normalize_model_id(self, model_id: str) -> str:
        return model_id