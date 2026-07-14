"""
url_health.py - Verifies all URLs in the body resolve (HEAD check).

Lighter-weight than citation_integrity — doesn't word-match titles.
Useful when you want to catch dead links without paying for the
title-extraction cost. Schematics that already use citation_integrity
don't need url_health (it's a strict subset).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ..plugins import register
from ..report import JudgeReport, RunContext

if TYPE_CHECKING:
    pass


# Match bare URLs and markdown-cited URLs.
_URL_RE = re.compile(r"(?:\]\(|\s|^)(https?://[^\s)]+)", re.MULTILINE)

_HEAD_TIMEOUT_S = 8.0


class UrlHealthPlugin:
    """JudgePlugin: HEAD-checks every URL in the body for 404/5xx.

    Hard-fail on 404/410.
    Soft-flag on 5xx (transient).
    """
    tag = "url_health"

    async def run(self, body: str, context: RunContext) -> JudgeReport:
        report = JudgeReport()
        urls = list(set(_URL_RE.findall(body)))
        if not urls:
            return report

        results = await asyncio.gather(
            *(_check_url(u, context) for u in urls),
            return_exceptions=True,
        )
        for finding in results:
            if isinstance(finding, BaseException):
                report.soft_flags.append(f"url_health internal error: {finding!r}")
                continue
            if finding is None:
                continue
            severity, msg = finding
            if severity == "hard":
                report.hard_fails.append(msg)
            else:
                report.soft_flags.append(msg)
        return report


async def _check_url(url: str, ctx: RunContext) -> tuple[str, str] | None:
    """Returns (severity, msg) on issue, None on healthy."""
    try:
        # HEAD first; many servers reject HEAD with 405, in which case
        # we fall back to a tiny GET.
        r = await ctx.http_client.head(
            url, timeout=_HEAD_TIMEOUT_S, follow_redirects=True,
        )
        if r.status_code == 405:
            r = await ctx.http_client.get(
                url, timeout=_HEAD_TIMEOUT_S, follow_redirects=True,
            )
    except Exception as e:  # noqa: BLE001
        return ("hard", f"URL unreachable ({type(e).__name__}): {url}")

    if r.status_code in (404, 410):
        return ("hard", f"URL {r.status_code}: {url}")
    if r.status_code >= 500:
        return ("soft", f"URL HTTP {r.status_code}: {url}")
    if r.status_code >= 400:
        return ("hard", f"URL HTTP {r.status_code}: {url}")
    return None  # OK


register(UrlHealthPlugin())
