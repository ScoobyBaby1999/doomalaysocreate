"""
json_parses.py - For JSON-output tasks, validate body parses as JSON.

Used when output_rules.format == "json". The body should be a single
parseable JSON document; anything else (prose preamble, fenced code
blocks, partial JSON) hard-fails.
"""

from __future__ import annotations

import json

from ..plugins import register
from ..report import JudgeReport, RunContext


class JsonParsesPlugin:
    """JudgePlugin: body must be a single valid JSON document.

    Strips fenced code blocks before parsing (some models can't help
    themselves with ```json fences). Hard-fails on anything else.
    """
    tag = "json_parses"

    async def run(self, body: str, context: RunContext) -> JudgeReport:
        report = JudgeReport()
        text = body.strip()

        if text.startswith("```"):
            first_nl = text.find("\n")
            last_fence = text.rfind("```")
            if first_nl != -1 and last_fence > first_nl:
                text = text[first_nl + 1 : last_fence].strip()

        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            report.hard_fails.append(
                f"body is not valid JSON: {e}; first 200 chars: {text[:200]!r}"
            )

        return report


register(JsonParsesPlugin())
