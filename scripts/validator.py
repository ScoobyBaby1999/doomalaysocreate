#!/usr/bin/env python3
"""
validator.py - Structural + epistemic checks for Replay research drafts.

Goal: refuse to let the pipeline produce confident-sounding bullshit.

The validator parses a markdown draft and returns a ValidatorReport with:
- hard failures (must be fixed or draft is rerolled)
- soft flags (feed into the critique stage, don't block)

It does NOT edit the draft. The reviser applies fixes.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

# ---- Heuristic rule tables ----

# Phrases that inflate authority without evidence. Flagged unless the same
# sentence contains an inline URL (treated as "cited in place").
CONFIDENCE_BOOSTERS = [
    r"\bstate-?of-?the-?art\b",
    r"\bdefinitively\b",
    r"\bundoubtedly\b",
    r"\brevolutionary\b",
    r"\bindustry[- ]standard\b",
    r"\bclearly the best\b",
    r"\bclearly superior\b",
    r"\bproven to\b",
    r"\bthe best\b",
    r"\bworld[- ]class\b",
    r"\bbreakthrough\b",
    r"\bgame[- ]changing\b",
]
CONFIDENCE_RE = re.compile("|".join(CONFIDENCE_BOOSTERS), re.IGNORECASE)

REFUSAL_PATTERNS = [
    r"\bI (cannot|can't|am unable to)\b",
    r"\bas an AI\b",
    r"\bI don't have access to\b",
    r"\bI'm just an AI\b",
]
REFUSAL_RE = re.compile("|".join(REFUSAL_PATTERNS), re.IGNORECASE)

# Source-tier classification by hostname.
T1_HOSTS = {"arxiv.org", "acm.org", "ieee.org", "nature.com", "science.org",
            "openreview.net", "proceedings.mlr.press", "aclweb.org", "aclanthology.org"}
T1_SUFFIXES = (".edu",)
T2_HOSTS = {"github.com", "openai.com", "anthropic.com", "meta.com",
            "deepmind.com", "googleblog.com", "ai.google", "huggingface.co",
            "pytorch.org", "tensorflow.org", "llamaindex.ai", "langchain.com"}
T3_HOSTS = {"simonwillison.net", "eugeneyan.com", "jaykmody.com",
            "lilianweng.github.io", "karpathy.github.io", "gwern.net"}
T4_HOSTS = {"reddit.com", "news.ycombinator.com", "medium.com",
            "stackoverflow.com", "stackexchange.com", "twitter.com", "x.com"}

URL_RE = re.compile(r"https?://[^\s\)\]]+")

# Year regex for staleness detection. Current year driven by today's date.
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Claims that likely need attribution: numbers with units, version strings,
# benchmark-ish words near numbers.
NUMERIC_CLAIM_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(%|ms|s|GB|MB|KB|B|M|B tokens|tokens|params|parameters|req/s|qps|fps)\b",
    re.IGNORECASE,
)


@dataclass
class ValidatorReport:
    ok: bool = True
    hard_fails: list[str] = field(default_factory=list)
    soft_flags: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"ok={self.ok}", f"stats={self.stats}"]
        if self.hard_fails:
            lines.append("HARD FAILS:")
            lines.extend(f"  - {f}" for f in self.hard_fails)
        if self.soft_flags:
            lines.append("SOFT FLAGS:")
            lines.extend(f"  - {f}" for f in self.soft_flags)
        return "\n".join(lines)

    def to_critique_feed(self) -> str:
        """Formatted for handing to the critique-stage LLM."""
        parts = []
        if self.hard_fails:
            parts.append("HARD (must fix):")
            parts.extend(f"- {f}" for f in self.hard_fails)
        if self.soft_flags:
            parts.append("\nSOFT (consider):")
            parts.extend(f"- {f}" for f in self.soft_flags)
        return "\n".join(parts) if parts else "(no issues found by validator)"


# ---- Checks ----

def _extract_headings(md: str) -> tuple[list[str], list[str]]:
    h1 = re.findall(r"^#\s+(.+)$", md, re.MULTILINE)
    h2 = re.findall(r"^##\s+(.+)$", md, re.MULTILINE)
    return h1, h2


def _word_count(md: str) -> int:
    return len(re.findall(r"\S+", md))


def _extract_urls(md: str) -> list[str]:
    return [u.rstrip(".,;:") for u in URL_RE.findall(md)]


def _tier(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "T5"
    host = host.lower().lstrip("www.")
    if host in T1_HOSTS or host.endswith(T1_SUFFIXES):
        return "T1"
    if host in T2_HOSTS:
        return "T2"
    # github.com/<org>/<repo> is T2, but gist.github.com is T4-ish; keep simple.
    if host in T3_HOSTS:
        return "T3"
    if host in T4_HOSTS or any(host.endswith("." + h) for h in T4_HOSTS):
        return "T4"
    return "T5"


def _sentences(md: str) -> list[str]:
    # Cheap sentence splitter; good enough for proximity checks.
    return re.split(r"(?<=[.!?])\s+", md)


def _check_structure(md: str, prompt_toc: list[str]) -> ValidatorReport:
    rep = ValidatorReport()
    h1, h2 = _extract_headings(md)
    wc = _word_count(md)
    rep.stats["word_count"] = wc
    rep.stats["h2_count"] = len(h2)

    if not h1:
        rep.hard_fails.append("Missing H1 title.")
    if wc < 3000:
        rep.hard_fails.append(f"Word count {wc} below 3000 floor — draft truncated or collapsed.")
    elif wc < 5000:
        rep.soft_flags.append(
            f"Word count {wc} below 5000 target — expand thin sections. "
            f"(Free models with output-token caps may genuinely cap around here; "
            f"don't grind past 2 rounds just on this.)"
        )

    # Topic coverage: each prompt TOC entry should appear substring-wise in some H2.
    h2_joined = " | ".join(h.lower() for h in h2)
    for topic in prompt_toc:
        # topic is already lowercased key phrase
        if topic.lower() not in h2_joined:
            rep.soft_flags.append(f"Prompt topic not clearly covered in any H2: `{topic}`")

    # Required trailing sections.
    if not re.search(r"^##\s+Sources\s*&\s*Confidence", md, re.MULTILINE | re.IGNORECASE):
        rep.hard_fails.append("Missing required `## Sources & Confidence` section.")
    if not re.search(r"^##\s+What I'?m not sure about", md, re.MULTILINE | re.IGNORECASE):
        rep.hard_fails.append("Missing required `## What I'm not sure about` section.")

    if REFUSAL_RE.search(md):
        rep.hard_fails.append("Contains refusal / meta-AI language (e.g. 'as an AI', 'I cannot').")

    return rep


def _check_confidence(md: str, rep: ValidatorReport) -> None:
    for sent in _sentences(md):
        m = CONFIDENCE_RE.search(sent)
        if m and not URL_RE.search(sent):
            phrase = sent.strip()
            snippet = phrase[:140] + ("…" if len(phrase) > 140 else "")
            rep.soft_flags.append(
                f"Overconfident phrase `{m.group(0)}` without in-sentence citation: \"{snippet}\""
            )


def _check_unsourced_claims(md: str, rep: ValidatorReport) -> None:
    sents = _sentences(md)
    flagged = 0
    for i, sent in enumerate(sents):
        if not NUMERIC_CLAIM_RE.search(sent):
            continue
        # Attribution window: this sentence + one before + one after.
        window = " ".join(sents[max(0, i - 1): i + 2])
        if URL_RE.search(window):
            continue
        if re.search(r"\b(per|according to|from the|in the paper|in the repo|cited in)\b",
                     window, re.IGNORECASE):
            continue
        flagged += 1
        if flagged <= 10:
            snippet = sent.strip()[:160]
            rep.soft_flags.append(f"Numeric claim without nearby source: \"{snippet}\"")
    if flagged > 10:
        rep.soft_flags.append(f"(+{flagged - 10} more unsourced numeric claims not shown)")
    rep.stats["unsourced_claims"] = flagged


def _check_staleness(md: str, rep: ValidatorReport, today: datetime) -> None:
    cutoff = today.year - 2
    stale = set()
    for m in YEAR_RE.finditer(md):
        y = int(m.group(0))
        if 2000 <= y <= cutoff:
            stale.add(y)
    if stale:
        rep.soft_flags.append(
            f"Years referenced that are ≥2 yr old: {sorted(stale)} — confirm facts aren't outdated."
        )


def _tier_urls(urls: list[str], rep: ValidatorReport) -> dict[str, list[str]]:
    tiers: dict[str, list[str]] = {"T1": [], "T2": [], "T3": [], "T4": [], "T5": []}
    for u in urls:
        tiers[_tier(u)].append(u)
    rep.stats["urls_total"] = len(urls)
    for k, v in tiers.items():
        rep.stats[f"urls_{k}"] = len(v)
    if len(urls) < 5:
        rep.hard_fails.append(f"Only {len(urls)} URLs — below 5 minimum.")
    # Soft flag if ratio of T4/T5 is dominant.
    low = len(tiers["T4"]) + len(tiers["T5"])
    if urls and low / len(urls) > 0.5:
        rep.soft_flags.append(
            f"Over half of sources are T4/T5 (forums/unknown) — "
            "strengthen with peer-reviewed or primary docs where possible."
        )
    return tiers


# ---- Citation integrity ----

# Generic stopwords so we don't count "the", "with" etc. as matching signals.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "their", "his", "her", "our",
    "using", "via", "toward", "towards", "into", "onto", "over", "under",
    "about", "across", "through", "between", "among", "per", "vs",
    "et", "al", "paper", "article", "post", "report",
}

# Pairs where an *italic* or "quoted" title sits within ~300 chars of a URL.
# Deliberately excludes **bold** — bold is typically terminology/emphasis in
# this codebase's drafts, not citation titles. Bibliography convention uses
# italic for titles. Title content must not begin or end with whitespace
# (markdown italics don't permit surrounding spaces).
_TITLE_PIECE = (
    r'(?:\*(?!\*)(?P<it>[^\s*][^*\n]{3,158}[^\s*])\*(?!\*)'
    r'|"(?P<q1>[^"\n]{5,160})"'
    r"|'(?P<q2>[^'\n]{5,160})')"
)
_URL_PIECE = r'(?P<url>https?://[^\s\)\]\|]+)'

_CITE_WITH_TITLE_RE = re.compile(
    rf"(?:{_TITLE_PIECE}[^\n]{{0,300}}?{_URL_PIECE})"
    rf"|(?:{_URL_PIECE.replace('url', 'url2')}[^\n]{{0,300}}?"
    rf"{_TITLE_PIECE.replace('it','it2').replace('q1','q3').replace('q2','q4')})",
    re.VERBOSE,
)


def _title_signals(title: str) -> list[str]:
    """Extract distinctive words (lowercase, len≥5, non-stopword) from a title."""
    cleaned = re.sub(r"[*\"'`]", "", title).strip()
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", cleaned)
    return [t.lower() for t in tokens if t.lower() not in _STOPWORDS]


def _strip_html(html: str) -> str:
    # Drop script/style blocks, then all tags. Cheap; good enough for a substring check.
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _collect_cite_pairs(md: str) -> list[tuple[str, str]]:
    """Return unique (title, url) pairs found via proximity matching."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _CITE_WITH_TITLE_RE.finditer(md):
        gd = m.groupdict()
        title = (gd.get("it") or gd.get("q1") or gd.get("q2")
                 or gd.get("it2") or gd.get("q3") or gd.get("q4") or "")
        url = (gd.get("url") or gd.get("url2") or "").rstrip(".,;:")
        if not title or not url:
            continue
        key = (title.strip(), url)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


async def _check_citation_integrity(
    md: str,
    rep: ValidatorReport,
    *,
    timeout_s: float = 15.0,
    concurrency: int = 6,
    max_body_chars: int = 80_000,
) -> None:
    """Fetch each (title, URL) pair and verify the title phrase actually
    appears on the page. Catches the weak-LLM failure mode of confidently
    pairing a real-looking URL with a wrong/invented title."""
    pairs = _collect_cite_pairs(md)
    rep.stats["cite_pairs_checked"] = len(pairs)
    if not pairs:
        return

    # Cache page bodies by URL.
    unique_urls = list({u for _, u in pairs})
    bodies: dict[str, str | None] = {}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s,
                                 headers={"User-Agent": "Mozilla/5.0 .md-replay-validator"}) as client:
        async def fetch(u: str) -> None:
            async with sem:
                try:
                    r = await client.get(u)
                    if r.status_code >= 400:
                        bodies[u] = None
                        return
                    text = r.text[:max_body_chars]
                    bodies[u] = _strip_html(text)
                except (httpx.HTTPError, OSError):
                    bodies[u] = None

        await asyncio.gather(*[fetch(u) for u in unique_urls])

    suspect = 0
    for title, url in pairs:
        body = bodies.get(url)
        if body is None:
            continue  # unfetchable; url-liveness check already flagged
        signals = _title_signals(title)
        if len(signals) < 2:
            continue  # not enough distinctive words to judge
        hits = sum(1 for w in signals if w in body)
        ratio = hits / len(signals)
        # Thresholds: <30% → clearly fabricated; 30-60% → soft flag.
        if ratio < 0.3:
            suspect += 1
            rep.hard_fails.append(
                f"Citation likely fabricated: claimed title `{title.strip()[:80]}` "
                f"does not appear on {url} ({hits}/{len(signals)} distinctive words matched)."
            )
        elif ratio < 0.6:
            rep.soft_flags.append(
                f"Weak citation match: `{title.strip()[:80]}` ↔ {url} "
                f"({hits}/{len(signals)} distinctive words). Verify author/title."
            )
    rep.stats["cite_fabrications"] = suspect


async def _check_urls_live(
    urls: list[str],
    rep: ValidatorReport,
    timeout_s: float = 10.0,
    concurrency: int = 8,
) -> None:
    """HEAD-check each URL. Flag 404 / DNS / timeout."""
    if not urls:
        return
    sem = asyncio.Semaphore(concurrency)
    broken: list[str] = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
        async def check(u: str) -> None:
            async with sem:
                try:
                    r = await client.head(u)
                    # Some hosts 405 HEAD; retry GET on 4xx except 404.
                    if r.status_code == 405:
                        r = await client.get(u)
                    if r.status_code >= 400:
                        broken.append(f"{u}  (HTTP {r.status_code})")
                except (httpx.HTTPError, OSError) as e:
                    broken.append(f"{u}  ({type(e).__name__})")

        await asyncio.gather(*[check(u) for u in urls])

    rep.stats["urls_broken"] = len(broken)
    for b in broken[:15]:
        rep.soft_flags.append(f"Broken URL: {b}")
    if len(broken) > 15:
        rep.soft_flags.append(f"(+{len(broken) - 15} more broken URLs not shown)")
    # Many broken URLs is a hard fail — the model is hallucinating links.
    if urls and len(broken) / len(urls) > 0.3:
        rep.hard_fails.append(
            f"{len(broken)}/{len(urls)} URLs broken — model is likely fabricating citations."
        )


# ---- Public entry point ----

async def validate(
    md: str,
    prompt_toc: list[str],
    *,
    check_urls: bool = True,
    check_citations: bool = True,
    today: datetime | None = None,
) -> ValidatorReport:
    today = today or datetime.utcnow()
    rep = _check_structure(md, prompt_toc)
    _check_confidence(md, rep)
    _check_unsourced_claims(md, rep)
    _check_staleness(md, rep, today)

    urls = _extract_urls(md)
    _tier_urls(urls, rep)
    if check_urls:
        await _check_urls_live(urls, rep)
    if check_citations:
        await _check_citation_integrity(md, rep)

    rep.ok = not rep.hard_fails
    return rep


def validate_sync(md: str, prompt_toc: list[str], **kw) -> ValidatorReport:
    return asyncio.run(validate(md, prompt_toc, **kw))


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    rep = validate_sync(text, prompt_toc=[], check_urls=False)
    print(rep.summary())
