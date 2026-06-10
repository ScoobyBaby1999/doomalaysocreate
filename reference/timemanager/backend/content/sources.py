from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import yaml

from oplog import atomic_write_json, log_event

# source fetching: parse seed_urls from prompt yaml frontmatter, fetch each
# url, extract readable article text with trafilatura, cache to disk.
# the fetched content goes into initial_context["fetched_sources"] so
# section_draft shards can ground their citations in real content.

_user_agent = "senior-research-bot/1.0 (https://github.com/senior; firstdoobievault@gmail.com)"
_timeout_s = 30.0
_max_retries = 3


def parse_frontmatter(prompt_text: str) -> dict[str, Any]:
    #   extract yaml frontmatter from a prompt md file.
    #   returns empty dict if no frontmatter block is found.
    if not prompt_text.startswith("---"):
        return {}
    end = prompt_text.find("\n---", 3)
    if end == -1:
        return {}
    raw_yaml = prompt_text[3:end].strip()
    try:
        parsed = yaml.safe_load(raw_yaml)
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


async def _fetch_one(client: httpx.AsyncClient, url: str) -> tuple[str, str | None, str | None]:
    #   fetch a single url. returns (url, title_or_none, text_or_none).
    #   on any failure returns (url, None, None) and logs the reason.
    import trafilatura

    for attempt in range(_max_retries):
        wait = 2 ** attempt
        try:
            response = await client.get(
                url,
                headers={"User-Agent": _user_agent},
                timeout=_timeout_s,
                follow_redirects=True,
            )
            if response.status_code == 200:
                html = response.text
                result = trafilatura.extract(
                    html, url=url,
                    include_tables=True,
                    include_links=False,
                    output_format="txt",
                    with_metadata=True,
                )
                if result:
                    #   trafilatura returns a string; for metadata we re-parse.
                    meta = trafilatura.extract_metadata(html, default_url=url)
                    title = (meta.title if meta else None) or url
                    text = trafilatura.extract(
                        html, url=url,
                        include_tables=True,
                        include_links=False,
                        output_format="txt",
                    ) or ""
                    return url, title, text
                #       trafilatura returned nothing - page may be empty or JS-rendered.
                log_event("source_empty", url=url[:200])
                return url, None, None
            else:
                log_event("source_http_fail",
                          url=url[:200], status=response.status_code,
                          attempt=attempt + 1)
                if response.status_code < 500:
                    #   4xx won't improve with retries.
                    return url, None, None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as e:
            log_event("source_network_fail",
                      url=url[:200], reason=repr(e)[:200],
                      attempt=attempt + 1)
        if attempt < _max_retries - 1:
            await asyncio.sleep(wait)
    return url, None, None


async def fetch_sources(
    client: httpx.AsyncClient,
    seed_urls: list[str],
    cache_path: Path,
) -> list[dict[str, Any]]:
    #   fetch all seed_urls concurrently; extract article text with trafilatura.
    #   returns a list of {index, url, title, text} dicts (only successful fetches).
    #   results are cached to cache_path so re-runs skip the network entirely.

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list) and len(cached) > 0:
                log_event("sources_cache_hit",
                          path=str(cache_path), count=len(cached))
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    log_event("sources_fetch_start", count=len(seed_urls))
    results = await asyncio.gather(*(_fetch_one(client, url) for url in seed_urls))

    sources: list[dict[str, Any]] = []
    for url, title, text in results:
        if title is not None and text is not None:
            sources.append({
                "index": len(sources) + 1,
                "url": url,
                "title": title,
                "text": text,
            })
            log_event("source_ok",
                      url=url[:200], title=(title or "")[:100],
                      chars=len(text))
        else:
            log_event("source_dropped", url=url[:200])

    log_event("sources_fetch_done",
              total=len(seed_urls), ok=len(sources),
              dropped=len(seed_urls) - len(sources))

    if sources:
        try:
            atomic_write_json(cache_path, sources)
        except OSError:
            pass

    return sources


def render_sources_context(sources: list[dict[str, Any]]) -> str:
    #   format fetched_sources as the text block injected into section_draft prompts.
    #   each entry shows [N], url, title, then the full extracted text.
    if not sources:
        return "(no sources provided)"
    parts = []
    for src in sources:
        parts.append(
            f"[{src['index']}] {src['title']}\n"
            f"URL: {src['url']}\n\n"
            f"{src['text']}"
        )
    return "\n\n---\n\n".join(parts)
