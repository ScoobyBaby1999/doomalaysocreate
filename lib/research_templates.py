"""Research templates + deep-research modes — provider-agnostic, SSE-friendly.

This module ports the four research templates (Breadth Search, Deep Dive,
Compare & Contrast, Fact Check) and the three deep-research modes (default,
react, extended_thinking) from the Next.js design to the Python backend.

Every template/mode is an ``async`` function that takes a question + a picked
``slot`` + an ``emit`` callback and yields structured SSE events:

    emit({"type": "status",  "stage": "searching", "detail": "..."})
    emit({"type": "sources", "items": [{title,url,snippet}, ...]})
    emit({"type": "thinking","text": "..."})        # optional reasoning
    emit({"type": "delta",   "text": "..."})        # synthesis deltas
    emit({"type": "done",    "output": "...", "sources": [...],
                            "usage": {...}, "cost_usd": 0.0})

All HTTP work goes through the existing ``scheduler.call_slot`` (which already
handles provider pacing, retries, and reasoning-body merge from
``reasoning_catalog.json``) and the existing ``web_tools.web_search`` /
``web_tools.web_fetch``. Never raises — failures land in an ``error`` event.

Designed to be called from:
  * ``agent_sessions.ResearchAdapter.turn`` (the chat endpoint's research path)
  * ``chat_jobs.run_job``                 (the queue/monitor runner)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable

import httpx

import web_tools
from oplog import log_event
from scheduler import ProviderError, call_slot

# Reasoning catalog is loaded lazily so this module stays stdlib-only at import
# time (matches the rest of the lib/ convention).
_reasoning_catalog: dict[str, dict] | None = None


def _rcat() -> dict[str, dict]:
    global _reasoning_catalog
    if _reasoning_catalog is None:
        try:
            from providers import load_reasoning_catalog
            _reasoning_catalog = load_reasoning_catalog() or {}
        except Exception:  # noqa: BLE001
            _reasoning_catalog = {}
    return _reasoning_catalog


def _resolve_body(logical: str | None, who: str | None, family: str | None) -> dict:
    """Pick the most-specific reasoning-catalog body for this model.

    Mirrors ``providers.resolve_reasoning_body`` without the import-time
    dependency (this module is imported by the agent path which is hot).
    """
    cat = _rcat()
    for key in (who, logical, family, "*"):
        if key and key in cat:
            body = cat[key].get("body")
            return dict(body) if isinstance(body, dict) else {}
    return {}


Emit = Callable[[dict], None]
"""Synchronous emit callback. Research helpers are async but emit is sync
(the AgentSession.emit / chat_jobs emit are both sync, writing to a queue)."""


# Effort levels → token budget + max steps. Aligned with the chat endpoint's
# effort param so a single dial drives panel judges AND research depth.
EFFORT_BUDGETS: dict[str, dict] = {
    "low":  {"max_tokens": 4096,  "max_steps": 3, "max_hops": 2, "max_subtopics": 3},
    "med":  {"max_tokens": 8192,  "max_steps": 5, "max_hops": 3, "max_subtopics": 4},
    "high": {"max_tokens": 16384, "max_steps": 7, "max_hops": 3, "max_subtopics": 5},
    "max":  {"max_tokens": 32768, "max_steps": 9, "max_hops": 4, "max_subtopics": 6},
}

DEFAULT_TIMEOUT_S = 1500.0
MAX_SUBTOPICS = 6
MIN_SUBTOPICS = 3
MAX_FACT_CLAIMS = 8
MAX_COMPARE_PERSPECTIVES = 4
DEEP_DIVE_HOPS = 3
TOP_K_PAGES = 3
MAX_FETCH_CHARS = 12000


# ---------------------------------------------------------------------------
# Small async helpers
# ---------------------------------------------------------------------------

async def _safe_call_slot(client: httpx.AsyncClient, picked, messages: list[dict],
                          *, max_tokens: int, timeout_s: float,
                          extra_body: dict | None) -> tuple[str, dict, str | None]:
    """Wrap call_slot so a ProviderError becomes (error_text, {}, code)."""
    try:
        content, usage = await call_slot(
            client, picked, messages=messages,
            max_tokens=max_tokens, timeout_s=timeout_s,
            extra_body=extra_body)
        return content, usage, None
    except ProviderError as e:
        msg = str(e)
        return "", {}, msg.split(":", 1)[0]
    except Exception as e:  # noqa: BLE001
        return "", {}, f"exc:{type(e).__name__}"


async def _search(query: str, client: httpx.AsyncClient, *, max_results: int = 5) -> list[dict]:
    """Run a web_search and parse it into [{title,url,snippet}, ...]."""
    raw = await web_tools.web_search(query, http_client=client, max_results=max_results)
    if not raw or raw.startswith("web_search"):
        return []
    items: list[dict] = []
    for m in re.finditer(r"^\d+\.\s+(.+?)\s+-\s+(https?://\S+)\s*\n\s*(.+?)$",
                         raw, re.MULTILINE | re.DOTALL):
        items.append({"title": m.group(1).strip(), "url": m.group(2).strip(),
                      "snippet": m.group(3).strip()[:400]})
    return items[:max_results]


async def _fetch(url: str, client: httpx.AsyncClient, *, max_chars: int = MAX_FETCH_CHARS) -> str:
    return await web_tools.web_fetch(url, http_client=client, max_chars=max_chars)


async def _llm(client: httpx.AsyncClient, picked, *, system: str, user: str,
               max_tokens: int, timeout_s: float,
               extra_body: dict | None = None) -> tuple[str, dict, str | None]:
    """One chat-completion call. Returns (content, usage, error_code_or_None)."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return await _safe_call_slot(client, picked, msgs,
                                 max_tokens=max_tokens, timeout_s=timeout_s,
                                 extra_body=extra_body)


# ---------------------------------------------------------------------------
# Template 1 — Breadth Search
# ---------------------------------------------------------------------------

async def run_breadth_search(*, question: str, client: httpx.AsyncClient,
                             picked, extra_body: dict | None,
                             max_tokens: int, timeout_s: float, emit: Emit,
                             effort: str = "med") -> dict:
    """Decompose → parallel search each sub-topic → read top pages → synthesize."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    n_sub = max(MIN_SUBTOPICS, min(MAX_SUBTOPICS, budget["max_subtopics"]))
    t0 = time.monotonic()
    in_tok = out_tok = 0
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    emit({"type": "status", "stage": "decompose",
          "detail": f"Decomposing into {n_sub} sub-topics..."})

    sys_p = ("You are a research planner. Decompose the user's question into "
             f"{n_sub} independent sub-topics for parallel web search. "
             "Output a JSON array of short search queries (max 80 chars each), "
             "no commentary. Example: [\"query 1\", \"query 2\"].")
    content, usage, err = await _llm(client, picked, system=sys_p,
        user=question, max_tokens=512, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"decompose failed: {err}"})
        return _result(False, error=f"decompose failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=[])
    sub_queries = _parse_json_list(content, max_items=n_sub)
    if not sub_queries:
        sub_queries = [question[:80]]
    emit({"type": "thinking", "text": f"Sub-topics: {sub_queries}"})

    emit({"type": "status", "stage": "searching",
          "detail": f"Searching {len(sub_queries)} sub-topics in parallel..."})
    search_results = await asyncio.gather(*[
        _search(q, client, max_results=5) for q in sub_queries])
    for q, items in zip(sub_queries, search_results):
        for it in items:
            if it["url"] not in seen_urls:
                seen_urls.add(it["url"])
                it["query"] = q
                all_sources.append(it)
    emit({"type": "sources", "items": all_sources[:30]})

    emit({"type": "status", "stage": "reading",
          "detail": f"Reading top {TOP_K_PAGES} pages per sub-topic..."})
    fetch_tasks: list[dict] = []
    for items in search_results:
        for it in items[:TOP_K_PAGES]:
            fetch_tasks.append(it)
    fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in fetch_tasks])
    top_pages: list[dict] = []
    for it, text in zip(fetch_tasks, fetched):
        if text and not text.startswith("web_fetch"):
            top_pages.append({"url": it["url"], "title": it["title"], "text": text[:MAX_FETCH_CHARS]})

    emit({"type": "status", "stage": "synthesizing",
          "detail": "Synthesizing answer with citations..."})
    sys_s = ("You are a research synthesizer. Use ONLY the provided sources to "
             "answer the user's question. Cite each claim as [title](url). "
             "If sources are insufficient, say so explicitly. Be thorough but concise.")
    ctx = _format_pages(top_pages)
    user_s = f"## Sources\n{ctx}\n\n## Question\n{question}\n\n## Answer (with citations)"
    content, usage, err = await _llm(client, picked, system=sys_s, user=user_s,
        max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"synthesize failed: {err}"})
        return _result(False, error=f"synthesize failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=all_sources)

    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=all_sources)
    emit({"type": "done", **result})
    return result


# ---------------------------------------------------------------------------
# Template 2 — Deep Dive
# ---------------------------------------------------------------------------

async def run_deep_dive(*, question: str, client: httpx.AsyncClient,
                        picked, extra_body: dict | None,
                        max_tokens: int, timeout_s: float, emit: Emit,
                        effort: str = "med") -> dict:
    """Initial search → for each of N hops, read page → generate follow-up → search."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    hops = max(2, min(DEEP_DIVE_HOPS, budget["max_hops"]))
    t0 = time.monotonic()
    in_tok = out_tok = 0
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    emit({"type": "status", "stage": "initial_search",
          "detail": f"Deep dive: {hops} hops. Initial search..."})

    current_query = question[:120]
    pages_read: list[dict] = []

    for hop in range(hops):
        emit({"type": "status", "stage": "hop",
              "detail": f"Hop {hop+1}/{hops}: searching '{current_query[:60]}'"})
        items = await _search(current_query, client, max_results=5)
        for it in items:
            if it["url"] not in seen_urls:
                seen_urls.add(it["url"])
                it["query"] = current_query
                all_sources.append(it)
        emit({"type": "sources", "items": all_sources[-10:]})

        if not items:
            break

        top = items[:2]
        fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in top])
        for it, text in zip(top, fetched):
            if text and not text.startswith("web_fetch"):
                pages_read.append({"url": it["url"], "title": it["title"], "text": text[:MAX_FETCH_CHARS]})

        if hop < hops - 1:
            emit({"type": "status", "stage": "followup",
                  "detail": f"Hop {hop+1}: generating follow-up query..."})
            sys_f = ("You are a research analyst. Based on the pages read so far "
                     "for the user's question, generate ONE focused follow-up "
                     "search query that would deepen the investigation. "
                     "Output ONLY the query string, no quotes, no commentary.")
            ctx = _format_pages(pages_read[-2:])
            user_f = (f"## Question\n{question}\n\n## Pages read so far (excerpt)\n{ctx}\n\n"
                      "## Follow-up query")
            content, usage, err = await _llm(client, picked, system=sys_f, user=user_f,
                max_tokens=120, timeout_s=timeout_s, extra_body=extra_body)
            in_tok += int(usage.get("prompt_tokens") or 0)
            out_tok += int(usage.get("completion_tokens") or 0)
            if err or not content.strip():
                break
            current_query = content.strip().strip('"').strip("'")[:120]
            emit({"type": "thinking", "text": f"Follow-up query: {current_query}"})

    emit({"type": "status", "stage": "synthesizing",
          "detail": f"Synthesizing deep-dive answer from {len(pages_read)} pages..."})
    sys_s = ("You are a research synthesizer doing a DEEP dive. Use ONLY the "
             "provided sources (which span multiple hops of investigation). "
             "Cite each claim as [title](url). Surface nuances, contradictions, "
             "and unanswered questions. Be thorough.")
    ctx = _format_pages(pages_read)
    user_s = f"## Sources (multi-hop)\n{ctx}\n\n## Question\n{question}\n\n## Deep-dive answer"
    content, usage, err = await _llm(client, picked, system=sys_s, user=user_s,
        max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"synthesize failed: {err}"})
        return _result(False, error=f"synthesize failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=all_sources)

    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=all_sources)
    emit({"type": "done", **result})
    return result


# ---------------------------------------------------------------------------
# Template 3 — Compare & Contrast
# ---------------------------------------------------------------------------

async def run_compare_contrast(*, question: str, client: httpx.AsyncClient,
                               picked, extra_body: dict | None,
                               max_tokens: int, timeout_s: float, emit: Emit,
                               effort: str = "med") -> dict:
    """Identify 3-4 perspectives → parallel search each → contrast → consensus + disagreement."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    n_persp = max(3, min(MAX_COMPARE_PERSPECTIVES, budget["max_subtopics"]))
    t0 = time.monotonic()
    in_tok = out_tok = 0
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    emit({"type": "status", "stage": "identify_perspectives",
          "detail": f"Identifying {n_persp} perspectives..."})

    sys_p = (f"You are a research planner. Identify {n_persp} distinct "
             "perspectives, schools of thought, or stakeholder positions on the "
             "user's question. Output a JSON array of objects with "
             "{'perspective': 'name', 'query': 'search query'}. No commentary.")
    content, usage, err = await _llm(client, picked, system=sys_p, user=question,
        max_tokens=512, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"perspectives failed: {err}"})
        return _result(False, error=f"perspectives failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=[])
    perspectives = _parse_json_objects(content, ["perspective", "query"])
    if not perspectives:
        perspectives = [{"perspective": "general", "query": question[:80]}]
    emit({"type": "thinking", "text": f"Perspectives: {[p['perspective'] for p in perspectives]}"})

    emit({"type": "status", "stage": "searching",
          "detail": f"Searching {len(perspectives)} perspectives in parallel..."})
    persp_results = await asyncio.gather(*[
        _search(p["query"], client, max_results=4) for p in perspectives])
    per_persp_pages: list[list[dict]] = []
    for p, items in zip(perspectives, persp_results):
        for it in items:
            if it["url"] not in seen_urls:
                seen_urls.add(it["url"])
                it["query"] = p["perspective"]
                all_sources.append(it)
        top = items[:1]
        fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in top])
        pages = [{"url": it["url"], "title": it["title"],
                  "text": text[:MAX_FETCH_CHARS], "perspective": p["perspective"]}
                 for it, text in zip(top, fetched)
                 if text and not text.startswith("web_fetch")]
        per_persp_pages.append(pages)
    emit({"type": "sources", "items": all_sources[:30]})

    emit({"type": "status", "stage": "synthesizing",
          "detail": "Contrasting perspectives..."})
    sys_s = ("You are a research synthesizer doing a COMPARE & CONTRAST. "
             "Surface both the CONSENSUS (where perspectives agree) and the "
             "DISAGREEMENT (where they differ). Cite each claim as [title](url). "
             "Structure your answer with '## Consensus' and '## Disagreement' "
             "sections, then a brief synthesis.")
    sections = []
    for p, pages in zip(perspectives, per_persp_pages):
        ctx = _format_pages(pages)
        sections.append(f"### Perspective: {p['perspective']}\n{ctx}")
    user_s = (f"## Sources by perspective\n" + "\n\n".join(sections) +
              f"\n\n## Question\n{question}\n\n## Compare & contrast answer")
    content, usage, err = await _llm(client, picked, system=sys_s, user=user_s,
        max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"synthesize failed: {err}"})
        return _result(False, error=f"synthesize failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=all_sources)

    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=all_sources)
    emit({"type": "done", **result})
    return result


# ---------------------------------------------------------------------------
# Template 4 — Fact Check
# ---------------------------------------------------------------------------

async def run_fact_check(*, question: str, client: httpx.AsyncClient,
                         picked, extra_body: dict | None,
                         max_tokens: int, timeout_s: float, emit: Emit,
                         effort: str = "med") -> dict:
    """Extract claims → verify each in parallel → rate TRUE/FALSE/MIXED/UNVERIFIED."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    max_claims = max(3, min(MAX_FACT_CLAIMS, budget["max_subtopics"] + 2))
    t0 = time.monotonic()
    in_tok = out_tok = 0
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    emit({"type": "status", "stage": "extract_claims",
          "detail": f"Extracting up to {max_claims} verifiable claims..."})

    sys_p = (f"You are a fact-checker. Extract up to {max_claims} distinct, "
             "verifiable factual claims from the user's text. Output a JSON "
             "array of objects: {'claim': '...', 'query': 'search query to verify'}. "
             "No commentary.")
    content, usage, err = await _llm(client, picked, system=sys_p, user=question,
        max_tokens=768, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"extract claims failed: {err}"})
        return _result(False, error=f"extract claims failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=[])
    claims = _parse_json_objects(content, ["claim", "query"])
    if not claims:
        emit({"type": "status", "stage": "no_claims",
              "detail": "No verifiable claims found."})
        result = _result(True, output="No verifiable claims found in the input.",
                         t0=t0, in_tok=in_tok, out_tok=out_tok, sources=[])
        emit({"type": "done", **result})
        return result
    emit({"type": "thinking", "text": f"Claims: {[c['claim'][:60] for c in claims]}"})

    emit({"type": "status", "stage": "verifying",
          "detail": f"Verifying {len(claims)} claims in parallel..."})
    verify_tasks = [_verify_one_claim(client, picked, c, extra_body, timeout_s)
                    for c in claims]
    verdicts = await asyncio.gather(*verify_tasks)
    for v in verdicts:
        in_tok += v.pop("_in_tok", 0)
        out_tok += v.pop("_out_tok", 0)
        for it in v.get("sources", []):
            if it["url"] not in seen_urls:
                seen_urls.add(it["url"])
                all_sources.append(it)
    emit({"type": "sources", "items": all_sources[:30]})

    emit({"type": "status", "stage": "synthesizing",
          "detail": "Rating claims and synthesizing..."})
    sys_s = ("You are a fact-check synthesizer. For each claim, output a verdict "
             "line: '### CLAIM: <text>' then '**Verdict:** TRUE|FALSE|MIXED|UNVERIFIED' "
             "(confidence: HIGH/MEDIUM/LOW) and 1-2 sentences of evidence with "
             "[title](url) citations. Be precise: TRUE means well-supported by "
             "sources, FALSE means contradicted, MIXED means partially supported, "
             "UNVERIFIED means sources were insufficient.")
    claims_block = "\n\n".join(
        f"### Claim {i+1}\n{v['claim']}\n\nSources found:\n{_format_pages(v.get('pages', []))}"
        for i, v in enumerate(verdicts))
    user_s = f"## Claims and evidence\n{claims_block}\n\n## Original text\n{question}\n\n## Fact-check verdicts"
    content, usage, err = await _llm(client, picked, system=sys_s, user=user_s,
        max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"synthesize failed: {err}"})
        return _result(False, error=f"synthesize failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=all_sources)

    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=all_sources, verdicts=verdicts)
    emit({"type": "done", **result})
    return result


async def _verify_one_claim(client: httpx.AsyncClient, picked, claim: dict,
                            extra_body: dict | None, timeout_s: float) -> dict:
    """Search + fetch + read for one claim. Returns pages + sources."""
    q = claim.get("query") or claim.get("claim") or ""
    items = await _search(q, client, max_results=3)
    top = items[:2]
    fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in top]) if top else []
    pages = [{"url": it["url"], "title": it["title"], "text": text[:MAX_FETCH_CHARS]}
             for it, text in zip(top, fetched)
             if text and not text.startswith("web_fetch")]
    return {"claim": claim.get("claim", ""), "query": q, "pages": pages,
            "sources": items, "_in_tok": 0, "_out_tok": 0}


# ---------------------------------------------------------------------------
# Deep research modes
# ---------------------------------------------------------------------------

async def run_deep_research(*, mode: str, question: str, client: httpx.AsyncClient,
                            picked, extra_body: dict | None,
                            max_tokens: int, timeout_s: float, emit: Emit,
                            effort: str = "med") -> dict:
    """Dispatch to the right deep-research mode."""
    mode = (mode or "default").strip().lower()
    if mode == "react":
        return await _deep_research_react(question=question, client=client, picked=picked,
                                          extra_body=extra_body, max_tokens=max_tokens,
                                          timeout_s=timeout_s, emit=emit, effort=effort)
    if mode == "extended_thinking":
        return await _deep_research_extended_thinking(question=question, client=client,
                                                      picked=picked, extra_body=extra_body,
                                                      max_tokens=max_tokens,
                                                      timeout_s=timeout_s, emit=emit, effort=effort)
    return await _deep_research_default(question=question, client=client, picked=picked,
                                        extra_body=extra_body, max_tokens=max_tokens,
                                        timeout_s=timeout_s, emit=emit, effort=effort)


async def _deep_research_default(*, question: str, client: httpx.AsyncClient,
                                 picked, extra_body: dict | None,
                                 max_tokens: int, timeout_s: float, emit: Emit,
                                 effort: str = "med") -> dict:
    """search → read top 5 → generate follow-up queries → search follow-ups → synthesize."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    n_followups = max(2, min(4, budget["max_subtopics"] - 1))
    t0 = time.monotonic()
    in_tok = out_tok = 0
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    emit({"type": "status", "stage": "initial_search",
          "detail": "Deep research: initial search..."})
    items = await _search(question[:120], client, max_results=8)
    for it in items:
        if it["url"] not in seen_urls:
            seen_urls.add(it["url"])
            it["query"] = question[:80]
            all_sources.append(it)
    emit({"type": "sources", "items": all_sources[:15]})

    emit({"type": "status", "stage": "reading",
          "detail": f"Reading top {min(5, len(items))} pages..."})
    top = items[:5]
    fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in top])
    pages = [{"url": it["url"], "title": it["title"], "text": text[:MAX_FETCH_CHARS]}
             for it, text in zip(top, fetched)
             if text and not text.startswith("web_fetch")]

    emit({"type": "status", "stage": "followups",
          "detail": f"Generating {n_followups} follow-up queries..."})
    sys_f = ("You are a deep-research planner. Based on the pages read, "
             f"generate {n_followups} focused follow-up search queries that "
             "would deepen the investigation. Output a JSON array of strings. "
             "No commentary.")
    ctx = _format_pages(pages)
    user_f = (f"## Question\n{question}\n\n## Pages read\n{ctx}\n\n"
              f"## {n_followups} follow-up queries")
    content, usage, err = await _llm(client, picked, system=sys_f, user=user_f,
        max_tokens=256, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    followups = _parse_json_list(content, max_items=n_followups) if not err else []
    emit({"type": "thinking", "text": f"Follow-ups: {followups}"})

    if followups:
        emit({"type": "status", "stage": "followup_search",
              "detail": f"Searching {len(followups)} follow-ups in parallel..."})
        fu_results = await asyncio.gather(*[
            _search(q, client, max_results=3) for q in followups])
        fu_top = [items[0] if items else None for items in fu_results]
        fu_top = [it for it in fu_top if it]
        for q, items in zip(followups, fu_results):
            for it in items:
                if it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    it["query"] = q
                    all_sources.append(it)
        emit({"type": "sources", "items": all_sources[-15:]})
        if fu_top:
            fetched = await asyncio.gather(*[_fetch(it["url"], client) for it in fu_top])
            for it, text in zip(fu_top, fetched):
                if text and not text.startswith("web_fetch"):
                    pages.append({"url": it["url"], "title": it["title"], "text": text[:MAX_FETCH_CHARS]})

    emit({"type": "status", "stage": "synthesizing",
          "detail": f"Synthesizing from {len(pages)} pages..."})
    sys_s = ("You are a deep-research synthesizer. Produce a thorough, "
             "well-structured answer using ONLY the provided sources. Cite each "
             "claim as [title](url). Include: key findings, supporting evidence, "
             "caveats/uncertainties, and suggested next steps if relevant.")
    ctx = _format_pages(pages)
    user_s = f"## Sources\n{ctx}\n\n## Question\n{question}\n\n## Deep research answer"
    content, usage, err = await _llm(client, picked, system=sys_s, user=user_s,
        max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
    in_tok += int(usage.get("prompt_tokens") or 0)
    out_tok += int(usage.get("completion_tokens") or 0)
    if err:
        emit({"type": "error", "error": f"synthesize failed: {err}"})
        return _result(False, error=f"synthesize failed: {err}", t0=t0,
                       in_tok=in_tok, out_tok=out_tok, sources=all_sources)

    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=all_sources)
    emit({"type": "done", **result})
    return result


async def _deep_research_react(*, question: str, client: httpx.AsyncClient,
                               picked, extra_body: dict | None,
                               max_tokens: int, timeout_s: float, emit: Emit,
                               effort: str = "med") -> dict:
    """Use the existing agent.research_call ReAct loop."""
    import agent as _agent
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    sys_p = ("You are a deep-research agent. Use web_search and web_fetch to "
             "investigate the user's question thoroughly. Search broadly, read "
             "multiple sources, cross-reference, and write a comprehensive "
             "final answer with [title](url) citations. Aim for 3-6 searches.")
    emit({"type": "status", "stage": "react_loop",
          "detail": "Deep research (ReAct): think -> search -> read -> answer..."})
    res = await _agent.research_call(
        client, picked, sys_p, user_msg=question,
        max_tokens=max(budget["max_tokens"], max_tokens),
        timeout_s=timeout_s, extra_body=extra_body)
    usage = res.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    reasoning = usage.get("reasoning_content") or ""
    if reasoning:
        emit({"type": "thinking", "text": reasoning[:4000]})
    if res.get("ok"):
        emit({"type": "delta", "text": res.get("output", "")})
        result = _result(True, output=res.get("output", ""), t0=0.0,
                         in_tok=in_tok, out_tok=out_tok, sources=[],
                         steps=res.get("steps"), searches=res.get("searches"),
                         tool_calls=res.get("tool_calls"))
        # Use the actual elapsed_s from the research_call result.
        result["elapsed_s"] = res.get("elapsed_s", 0.0)
        emit({"type": "done", **result})
        return result
    emit({"type": "error", "error": res.get("error", "react failed")})
    return _result(False, error=res.get("error", "react failed"),
                   t0=0.0, in_tok=in_tok, out_tok=out_tok, sources=[])


async def _deep_research_extended_thinking(*, question: str, client: httpx.AsyncClient,
                                            picked, extra_body: dict | None,
                                            max_tokens: int, timeout_s: float,
                                            emit: Emit, effort: str = "med") -> dict:
    """Single pass with the MAX reasoning budget from reasoning_catalog."""
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["max"])
    t0 = time.monotonic()
    emit({"type": "status", "stage": "extended_thinking",
          "detail": "Single pass with max reasoning budget..."})
    body = dict(extra_body or {})
    rcat_body = _resolve_body(None,
                              getattr(picked, "who", None),
                              getattr(picked, "model_family", None))
    if rcat_body:
        for k, v in rcat_body.items():
            body.setdefault(k, v)
    if effort == "max":
        if "reasoning_effort" in body or any("reasoning_effort" in str(v) for v in body.values()):
            body["reasoning_effort"] = "high"
        if isinstance(body.get("chat_template_kwargs"), dict):
            ctk = dict(body["chat_template_kwargs"])
            ctk.setdefault("reasoning_effort", "high")
            ctk.setdefault("enable_thinking", True)
            body["chat_template_kwargs"] = ctk

    sys_p = ("You are a deep-reasoning analyst. Think step-by-step about the "
             "user's question. Surface your reasoning, consider multiple angles, "
             "then produce a thorough, well-structured answer. If you can cite "
             "general knowledge, do so; if the question needs current info, say "
             "so and produce your best analysis from prior knowledge.")
    content, usage, err = await _llm(client, picked, system=sys_p, user=question,
        max_tokens=max(budget["max_tokens"], max_tokens),
        timeout_s=timeout_s, extra_body=body or None)
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    reasoning = usage.get("reasoning_content") or ""
    if reasoning:
        emit({"type": "thinking", "text": reasoning[:8000]})
    if err:
        emit({"type": "error", "error": f"extended_thinking failed: {err}"})
        return _result(False, error=f"extended_thinking failed: {err}",
                       t0=t0, in_tok=in_tok, out_tok=out_tok, sources=[])
    emit({"type": "delta", "text": content})
    result = _result(True, output=content, t0=t0, in_tok=in_tok, out_tok=out_tok,
                     sources=[], reasoning_chars=len(reasoning))
    emit({"type": "done", **result})
    return result


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, Any] = {
    "breadth": run_breadth_search,
    "deep_dive": run_deep_dive,
    "compare": run_compare_contrast,
    "fact_check": run_fact_check,
}

DEEP_RESEARCH_MODES = ("default", "react", "extended_thinking")


async def run_template(*, template: str, question: str, client: httpx.AsyncClient,
                       picked, extra_body: dict | None = None,
                       max_tokens: int | None = None, timeout_s: float | None = None,
                       emit: Emit, effort: str = "med") -> dict:
    """Run one of the 4 research templates."""
    fn = TEMPLATES.get(template)
    if fn is None:
        raise ValueError(f"unknown research template: {template!r}")
    budget = EFFORT_BUDGETS.get(effort, EFFORT_BUDGETS["med"])
    mt = max_tokens or budget["max_tokens"]
    ts = timeout_s or DEFAULT_TIMEOUT_S
    try:
        return await fn(question=question, client=client, picked=picked,
                        extra_body=extra_body, max_tokens=mt, timeout_s=ts,
                        emit=emit, effort=effort)
    except Exception as e:  # noqa: BLE001
        log_event("research_template_error", template=template, error=repr(e)[:200])
        emit({"type": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
        return _result(False, error=f"{type(e).__name__}: {str(e)[:200]}",
                       t0=0.0, in_tok=0, out_tok=0, sources=[])


# ---------------------------------------------------------------------------
# Small parsing/formatting utilities
# ---------------------------------------------------------------------------

def _parse_json_list(text: str, *, max_items: int = 6) -> list[str]:
    """Extract a JSON string-array from a model response. Tolerant of prose."""
    if not text:
        return []
    m = re.search(r"\[\s*(?:\".*?\"\s*,?\s*)+\]", text, re.DOTALL)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return [str(x).strip()[:120] for x in arr if x][:max_items]
        except ValueError:
            pass
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip().lstrip("0123456789.-) ").strip().strip('"').strip("'")
        if s and len(s) < 200 and not s.lower().startswith(("here", "sure", "ok")):
            out.append(s)
    return out[:max_items]


def _parse_json_objects(text: str, required_keys: list[str]) -> list[dict]:
    """Extract a list of objects from a model JSON-array response."""
    if not text:
        return []
    m = re.search(r"\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]", text, re.DOTALL)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                out = []
                for o in arr:
                    if isinstance(o, dict) and all(k in o for k in required_keys):
                        out.append({k: str(o[k])[:200] for k in o})
                return out
        except ValueError:
            pass
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return [o for o in arr if isinstance(o, dict) and all(k in o for k in required_keys)]
    except ValueError:
        pass
    return []


def _format_pages(pages: list[dict]) -> str:
    if not pages:
        return "(no pages fetched)"
    parts = []
    for p in pages:
        url = p.get("url", "")
        title = p.get("title", "")
        text = (p.get("text") or "")[:MAX_FETCH_CHARS]
        persp = p.get("perspective")
        header = f"### {title}" + (f" [{persp}]" if persp else "")
        parts.append(f"{header}\nURL: {url}\n{text}")
    return "\n\n---\n\n".join(parts)


def _result(ok: bool, *, output: str | None = None, error: str | None = None,
            t0: float, in_tok: int, out_tok: int,
            sources: list[dict] | None = None, **extra) -> dict:
    usage = {"prompt_tokens": in_tok, "completion_tokens": out_tok}
    res: dict[str, Any] = {
        "ok": ok,
        "code": "ok" if ok else (error.split(":", 1)[0] if error else "error"),
        "usage": usage,
        "elapsed_s": round(time.monotonic() - t0, 1) if t0 else 0.0,
        "sources": sources or [],
        "searches": len(sources or []),
    }
    if output is not None:
        res["output"] = output
    if error is not None:
        res["error"] = error
    res.update({k: v for k, v in extra.items() if v is not None})
    return res
