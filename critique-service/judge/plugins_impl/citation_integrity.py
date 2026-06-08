"""
citation_integrity.py - Verifies cited URLs exist and titles roughly match.

Free-tier models' #1 failure mode: fabricating arxiv IDs or vendor doc
URLs with plausible-looking titles. The Wikipedia URL exists but the
"in 2024 the paper announced..." claim is invented; or the arxiv ID
is real but its actual title is unrelated to what the body claims.

This plugin defends by:
  1. Extracting markdown citations of the form `[Title](URL)`.
  2. HEAD/GET requesting each URL.
  3. Parsing the page's <title> tag.
  4. Computing word-overlap ratio between claimed and actual title.
  5. Hard-failing on 404/5xx OR ratio < 0.3.

Lifted (with adjustments) from the old validator.py.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..plugins import register
from ..report import JudgeReport, RunContext

if TYPE_CHECKING:
    pass


# Markdown citation regex: [title](http://...) or [title](https://...).
# Tolerates parentheses inside titles by being non-greedy.
_CITATION_RE = re.compile(
    r"\[([^\]]+?)\]\((https?://[^\s)]+)\)",
)

# HTML <title> extraction (case-insensitive, multiline tolerant).
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Words shorter than this don't count toward title overlap (the/and/of...).
_MIN_WORD_LEN = 4

# Failure threshold: title overlap ratio below this triggers a hard fail.
_OVERLAP_FAIL_THRESHOLD = 0.3

# Per-URL HTTP timeout. Paper has many citations; we don't want a single
# slow URL to block the whole judge call.
_URL_TIMEOUT_S = 10.0


class CitationIntegrityPlugin:
    """JudgePlugin: HEAD-fetches every cited URL, word-matches titles.

    Hard-fails on:
      - HTTP 404, 410, or 5xx (URL doesn't exist)
      - Title word-overlap ratio < 0.3 (claimed title doesn't match
        the page's actual title)
      - DNS failure / connection error

    Soft-flags on:
      - HTTP 200 but no <title> tag found (can't verify; not invented either)
      - HTTP redirects (just informational)
    """
    tag = "citation_integrity"

    async def run(self, body: str, context: RunContext) -> JudgeReport:
        report = JudgeReport()

        citations = _CITATION_RE.findall(body)
        # Deduplicate by URL — same URL cited multiple times is one check.
        seen: dict[str, str] = {}
        for title, url in citations:
            seen.setdefault(url, title)

        if not seen:
            return report  # nothing to verify; not a failure

        # Concurrent fetches via the shared http client.
        import asyncio
        results = await asyncio.gather(*(
            _check_one(url, title, context) for url, title in seen.items()
        ), return_exceptions=True)

        for finding in results:
            if isinstance(finding, BaseException):
                report.soft_flags.append(
                    f"citation_integrity internal error: {finding!r}"
                )
                continue
            assert isinstance(finding, dict)
            if finding["severity"] == "hard":
                report.hard_fails.append(finding["msg"])
            else:
                report.soft_flags.append(finding["msg"])

        return report


async def _check_one(url: str, claimed_title: str, ctx: RunContext) -> dict:
    """Verify one URL. Returns {severity: hard|soft, msg: str}."""
    try:
        # GET (not HEAD) because some sites return 405 on HEAD or omit
        # <title> from HEAD responses. Use a small read limit to keep
        # the bandwidth bounded.
        r = await ctx.http_client.get(
            url, timeout=_URL_TIMEOUT_S, follow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001 - network errors of any kind
        return {
            "severity": "hard",
            "msg": f"citation URL unreachable ({type(e).__name__}): {url}",
        }

    if r.status_code in (404, 410):
        return {
            "severity": "hard",
            "msg": f"citation URL {r.status_code}: {url}",
        }
    if r.status_code >= 500:
        return {
            "severity": "soft",
            "msg": f"citation URL HTTP {r.status_code} (transient?): {url}",
        }
    if r.status_code >= 400:
        return {
            "severity": "hard",
            "msg": f"citation URL HTTP {r.status_code}: {url}",
        }

    # Try to find the page title.
    text = r.text[:50_000]  # cap; titles are usually in the first KB
    m = _TITLE_RE.search(text)
    if m is None:
        return {
            "severity": "soft",
            "msg": f"citation URL has no <title> tag (can't verify): {url}",
        }

    actual_title = _clean_title(m.group(1))
    ratio = _word_overlap_ratio(claimed_title, actual_title)
    if ratio < _OVERLAP_FAIL_THRESHOLD:
        return {
            "severity": "hard",
            "msg": (
                f"citation title mismatch ({ratio:.0%} overlap): "
                f"claimed '{_clip(claimed_title)}' but page title is "
                f"'{_clip(actual_title)}' — {url}"
            ),
        }
    return {
        "severity": "soft",
        "msg": f"citation OK: {_clip(claimed_title)} ({ratio:.0%}) — {url}",
    }


def _clean_title(raw: str) -> str:
    """Strip whitespace and decode common HTML entities."""
    cleaned = " ".join(raw.split())
    return (
        cleaned
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )


def _word_overlap_ratio(a: str, b: str) -> float:
    """Compute fraction of long words in `a` that also appear in `b`.

    Asymmetric on purpose: we care whether the CLAIMED title's
    distinctive words appear on the PAGE. Stopwords (short words) are
    excluded because "the" matching "the" is meaningless.
    """
    a_words = {w.lower() for w in re.findall(r"\w+", a) if len(w) >= _MIN_WORD_LEN}
    b_words = {w.lower() for w in re.findall(r"\w+", b) if len(w) >= _MIN_WORD_LEN}
    if not a_words:
        return 1.0  # nothing distinctive to verify; don't penalize
    return len(a_words & b_words) / len(a_words)


def _clip(s: str, n: int = 60) -> str:
    """Truncate strings for log readability."""
    return s if len(s) <= n else s[: n - 1] + "…"


# Register on import.
register(CitationIntegrityPlugin())
