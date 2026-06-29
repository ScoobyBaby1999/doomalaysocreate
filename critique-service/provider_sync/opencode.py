"""OpenCode Zen/Go model sync implementation.

Fetches from OpenAI-compatible /v1/models endpoints (no auth needed for listing):
  Zen: https://opencode.ai/zen/v1/models  (49 models, 6 free + big-pickle)
  Go:  https://opencode.ai/zen/go/v1/models (20 models, subscription)

Falls back to scraping docs pricing tables when API is unreachable:
  Zen: https://opencode.ai/docs/zen/#pricing
  Go:  https://opencode.ai/docs/go/#usage-limits (pricing table is 2nd in section)

Pricing is extracted from docs tables and stored in metadata under "pricing":
  {"input": 0.30, "output": 1.20, "cached_read": 0.06, "cached_write": null}
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from html import unescape
from provider_sync.base import BaseSync, ModelInfo

_DOCS_UA = "doomalaysocreate/1.0"

_ZEN_FREE_IDS = frozenset({"big-pickle"})

_ZEN_DOCS_API_URL = "https://opencode.ai/zen/v1/models"
_ZEN_DOCS_DOCS_URL = "https://opencode.ai/docs/zen/#pricing"
_GO_API_URL = "https://opencode.ai/zen/go/v1/models"
_GO_DOCS_URL = "https://opencode.ai/docs/go/#usage-limits"


class OpenCodeSync(BaseSync):
    requires_auth = False
    env_var = None

    def __init__(self, api_key: str | None = None, **kwargs) -> None:
        super().__init__(api_key, **kwargs)
        self.variant = kwargs.get("variant", "zen")
        if self.variant == "go":
            self.provider_name = "opencode-go"
            self.models_url = _GO_API_URL
            self.docs_url = _GO_DOCS_URL
            self.docs_section = "usage-limits"
            self.env_var = "OPENCODE_GO_API_KEY"
        else:
            self.provider_name = "opencode-zen"
            self.models_url = _ZEN_DOCS_API_URL
            self.docs_url = _ZEN_DOCS_DOCS_URL
            self.docs_section = "pricing"
            self.env_var = "OPENCODE_ZEN_API_KEY"

        if self.api_key is None:
            self.api_key = os.environ.get(self.env_var, "")

    def fetch_models(self) -> list[ModelInfo]:
        models = self._try_api()
        if models:
            return models
        return self._try_docs_fallback()

    def _try_api(self) -> list[ModelInfo]:
        headers = self._build_auth_header() if self.api_key else None
        data = self._make_request(self.models_url, headers)
        if not data:
            return []

        models: list[ModelInfo] = []
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
                is_free=self._is_free(model_id),
                context_length=None,
                metadata=item,
            ))
        return models

    def _is_free(self, model_id: str) -> bool:
        if model_id.endswith("-free"):
            return True
        if model_id in _ZEN_FREE_IDS:
            return True
        return False

    def _try_docs_fallback(self) -> list[ModelInfo]:
        html = self._fetch_docs_page()
        if not html:
            return []
        pricing_table = self._find_pricing_table(html)
        if not pricing_table:
            return []
        return self._parse_pricing_table(pricing_table)

    def _fetch_docs_page(self) -> str:
        req = urllib.request.Request(
            self.docs_url,
            headers={"User-Agent": _DOCS_UA, "Accept": "text/html"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _find_pricing_table(html: str) -> list[list[str]] | None:
        """Find the first table with Input/Output pricing column headers.
        
        Returns list of rows, each row being a list of raw cell HTML strings.
        Skips the header row. Returns None if no pricing table found.
        """
        for table_match in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
            table_html = table_match.group(1)
            all_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
            if not all_rows:
                continue

            header_cells = re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", all_rows[0], re.DOTALL
            )
            header_texts = [
                re.sub(r"<[^>]+>", " ", c).strip().lower() for c in header_cells
            ]

            # Must have "Input" and "Output" columns
            if "input" in header_texts and "output" in header_texts:
                rows: list[list[str]] = []
                for tr in all_rows[1:]:
                    cells = re.findall(
                        r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL
                    )
                    if cells:
                        rows.append(cells)
                return rows

        return None

    def _parse_pricing_table(self, rows: list[list[str]]) -> list[ModelInfo]:
        """Parse pricing table rows into ModelInfo list.

        Table columns: Model | Input | Output | Cached Read | Cached Write
        - Free models have "Free" in all price columns (Cached Write may be "-" or empty)
        - Paid models have "$X.XX" price values
        - "-" or empty = not available (treated as null)
        """
        models: list[ModelInfo] = []
        seen: set[str] = set()

        for cells in rows:
            if len(cells) < 2:
                continue

            display_name = self._clean_cell(cells[0])
            if not display_name or display_name.lower() in ("model", ""):
                continue

            model_id = self._display_to_id(display_name)
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)

            pricing = self._parse_pricing_row(cells)
            has_dollar = any(
                "$" in self._clean_cell(c) for c in cells[1:]
            )
            is_free = not has_dollar

            models.append(ModelInfo(
                id=model_id,
                normalized_id=model_id,
                is_free=is_free,
                context_length=None,
                metadata={"pricing": pricing},
            ))
        return models

    @staticmethod
    def _parse_pricing_row(cells: list[str]) -> dict:
        """Parse a pricing table row into structured pricing dict.
        
        Returns {"input": float|None, "output": float|None,
                 "cached_read": float|None, "cached_write": float|None}
        """
        price_keys = ["input", "output", "cached_read", "cached_write"]
        pricing: dict = {}
        for i, key in enumerate(price_keys):
            if i + 1 < len(cells):
                val = OpenCodeSync._parse_price(cells[i + 1])
            else:
                val = None
            pricing[key] = val
        return pricing

    @staticmethod
    def _parse_price(cell_html: str) -> float | None:
        """Parse a price cell to float or None.
        
        Handles: "$1.20" -> 1.20, "Free" -> 0.0, "-" -> None, "" -> None
        """
        text = re.sub(r"<[^>]+>", " ", cell_html)
        text = unescape(text)
        text = text.strip()
        if not text or text == "-":
            return None
        text = text.lower()
        if text == "free":
            return 0.0
        m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", text)
        if m:
            return float(m.group(1))
        return None

    @staticmethod
    def _clean_cell(cell_html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", cell_html)
        text = unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _display_to_id(display_name: str) -> str:
        name = display_name.lower().strip()
        # Strip trailing parenthetical context annotations like "(≤ 256K tokens)"
        name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
        # Strip trailing parenthetical that appears mid-name after "or"
        name = re.sub(r"\s*\([^)]*\)\s+", " ", name).strip()
        return name.replace(" ", "-")

    def filter_free_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        if self.variant == "go":
            return models
        return [m for m in models if m.is_free]

    def normalize_model_id(self, model_id: str) -> str:
        return model_id
