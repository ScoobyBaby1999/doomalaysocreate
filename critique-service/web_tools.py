from __future__ import annotations
import html
import os
import re
import urllib.parse

import httpx

from oplog import log_event

# Provider-agnostic web tools for the research agent: web_search + web_fetch.
# Search uses Tavily when TAVILY_API_KEY is set, else keyless DuckDuckGo HTML.
# Fetch is httpx + a stdlib HTML->text strip (no heavy deps). Under MOCK_MODE/
# WEB_TOOLS_MOCK both return canned data so the agent loop tests offline.

MOCK = (os.environ.get("MOCK_MODE", "0").strip() in ("1", "true", "yes")
        or os.environ.get("WEB_TOOLS_MOCK", "0").strip() in ("1", "true", "yes"))
_UA = {"User-Agent": "Mozilla/5.0 (compatible; loom-research/1.0)"}
_FETCH_MAX = int(os.environ.get("WEB_FETCH_MAX_CHARS", "8000"))

TOOLS_PROTOCOL = (
    "\n\n## Research tools\n"
    "You can search and read the web. To use a tool, output EXACTLY one line and then STOP:\n"
    "  ACTION: web_search {\"query\": \"<search terms>\"}\n"
    "  ACTION: web_fetch {\"url\": \"<https url>\"}\n"
    "After each action you will receive an `OBSERVATION:` with the result. Use tools to gather "
    "evidence, then write your FINAL answer with no ACTION line. Cite sources as [title](url). "
    "Be efficient: only search when it genuinely helps."
)

_ACTION_RE = re.compile(r"ACTION:\s*(web_search|web_fetch)\s*(\{.*?\})", re.IGNORECASE | re.DOTALL)


def parse_action(text: str):
    import json
    m = _ACTION_RE.search(text or "")
    if not m:
        return None
    tool = m.group(1).lower()
    try:
        args = json.loads(m.group(2))
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    return tool, args


async def web_search(query: str, *, http_client: httpx.AsyncClient, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "web_search error: empty query"
    if MOCK:
        return (f"1. Mock Result A - https://example.com/a\n   Canned snippet about '{query}'.\n"
                f"2. Mock Result B - https://example.org/b\n   Second canned snippet.")
    try:
        results = await _tavily(query, http_client, max_results) if _tavily_key() \
            else await _ddg(query, http_client, max_results)
    except Exception as e:  # noqa: BLE001
        log_event("web_search_error", error=repr(e)[:160])
        return f"web_search error: {type(e).__name__}"
    if not results:
        return "web_search: no results"
    lines = []
    for i, r in enumerate(results[:max_results], 1):
        lines.append(f"{i}. {r['title']} - {r['url']}\n   {r['snippet'][:300]}")
    return "\n".join(lines)


async def web_fetch(url: str, *, http_client: httpx.AsyncClient, max_chars: int = _FETCH_MAX) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "web_fetch error: url must start with http(s)://"
    if MOCK:
        return f"[mock page text for {url}] Lorem ipsum content used for offline testing."
    try:
        r = await http_client.get(url, headers=_UA, timeout=12.0, follow_redirects=True)
        if r.status_code >= 400:
            return f"web_fetch error: HTTP {r.status_code}"
        text = _html_to_text(r.text)
        return text[:max_chars] if text else "web_fetch: page had no extractable text"
    except Exception as e:  # noqa: BLE001
        log_event("web_fetch_error", url=url[:120], error=repr(e)[:160])
        return f"web_fetch error: {type(e).__name__}"


def _tavily_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


async def _tavily(query: str, client: httpx.AsyncClient, n: int) -> list[dict]:
    r = await client.post("https://api.tavily.com/search", timeout=20.0, json={
        "api_key": _tavily_key(), "query": query, "max_results": n,
        "search_depth": "basic", "include_answer": False,
    })
    r.raise_for_status()
    data = r.json()
    out = []
    for item in (data.get("results") or []):
        out.append({"title": item.get("title", "")[:160],
                    "url": item.get("url", ""),
                    "snippet": (item.get("content") or "")[:400]})
    return out


_DDG_A = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


async def _ddg(query: str, client: httpx.AsyncClient, n: int) -> list[dict]:
    r = await client.get("https://html.duckduckgo.com/html/", headers=_UA, timeout=15.0,
                         params={"q": query}, follow_redirects=True)
    r.raise_for_status()
    hrefs = _DDG_A.findall(r.text)
    snips = _DDG_SNIP.findall(r.text)
    out = []
    for i, (href, title) in enumerate(hrefs[:n]):
        real = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            real = urllib.parse.unquote(m.group(1))
        snippet = _strip_tags(snips[i]) if i < len(snips) else ""
        out.append({"title": _strip_tags(title)[:160], "url": real, "snippet": snippet[:400]})
    return out


_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n\s*\n\s*\n+")


def _strip_tags(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def _html_to_text(page: str) -> str:
    page = _SCRIPT_STYLE.sub(" ", page or "")
    page = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", page, flags=re.IGNORECASE)
    text = html.unescape(_TAG.sub(" ", page))
    text = re.sub(r"[ \t]+", " ", text)
    return _WS.sub("\n\n", text).strip()
