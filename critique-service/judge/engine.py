"""
judge/engine.py - Layer 1: declarative rule engine.

Every rule type maps to a tiny pure-Python checker function. No I/O,
no LLM, no async. Runs in milliseconds even on a 10K-word body.

To add a new rule type: write one checker function below, register it
in CHECKERS. That's it. The schematic format already supports unknown
rule types as soft flags (so a typo in a user-authored template won't
block the pipeline).
"""

from __future__ import annotations

import re
from typing import Any

from .report import JudgeReport


# ---------- Public entry point ----------

def run_rules(
    body: str,
    rules: list[dict[str, Any]],
    output_rules: dict[str, Any] | None = None,
) -> JudgeReport:
    """Evaluate every rule in `rules` against `body`. Return JudgeReport.

    `output_rules` is the schematic's cross-cutting OutputRules dict —
    some checkers (no_banned_phrases) read defaults from here when the
    rule itself doesn't specify a value. Optional.
    """
    output_rules = output_rules or {}
    report = JudgeReport()

    for i, rule in enumerate(rules):
        rule_type = rule.get("type")
        if rule_type is None:
            report.soft_flags.append(f"rule[{i}] missing 'type' field")
            continue

        checker = _CHECKERS.get(rule_type)
        if checker is None:
            # Unknown rule type → soft flag so a typo doesn't block.
            report.soft_flags.append(f"unknown rule type: {rule_type!r}")
            continue

        try:
            failure = checker(body, rule.get("value"), output_rules)
        except Exception as e:  # noqa: BLE001 - rule checkers must be defensive
            report.soft_flags.append(
                f"rule {rule_type} crashed: {e!r}"
            )
            continue

        if failure is not None:
            report.hard_fails.append(failure)

    return report


# ---------- Individual checkers ----------
#
# Signature contract: checker(body, value, output_rules) -> str | None.
# Return None on pass, a short reason string on fail.
# Never raise — wrap dangerous logic; failed checkers become soft flags.

def _check_min_words(body: str, value: Any, _: dict) -> str | None:
    target = int(value)
    wc = len(body.split())
    if wc < target:
        return f"word count {wc} below minimum {target}"
    return None


def _check_max_words(body: str, value: Any, _: dict) -> str | None:
    target = int(value)
    wc = len(body.split())
    if wc > target:
        return f"word count {wc} above maximum {target}"
    return None


def _check_section_present(body: str, value: Any, _: dict) -> str | None:
    """Check for an H1/H2/H3 heading whose text matches `value`.

    Match is case-insensitive on the heading text only. Allows H1, H2,
    or H3 levels (so "## Sources" and "### Sources" both satisfy a
    section_present rule for "Sources").
    """
    section = str(value)
    pattern = re.compile(
        r"^\s*#{1,3}\s+" + re.escape(section) + r"\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    if not pattern.search(body):
        return f"missing required section: '{section}'"
    return None


def _check_no_banned_phrases(
    body: str, value: Any, output_rules: dict,
) -> str | None:
    """Check that the body contains none of the banned phrases.

    `value` is a list of phrases. If empty/None, falls back to
    output_rules["banned_phrases"]. Match is case-insensitive whole-word
    substring (so "obviously" matches but "obviousness" doesn't).
    """
    phrases = value if isinstance(value, list) else None
    if not phrases:
        phrases = output_rules.get("banned_phrases", [])
    if not phrases:
        return None

    found = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            continue
        # Word-boundary match, case-insensitive.
        pattern = re.compile(
            r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE,
        )
        if pattern.search(body):
            found.append(phrase)
    if found:
        return f"banned phrases present: {found}"
    return None


def _check_max_lines(body: str, value: Any, _: dict) -> str | None:
    target = int(value)
    n = body.count("\n") + 1
    if n > target:
        return f"line count {n} above maximum {target}"
    return None


def _check_regex_required(body: str, value: Any, _: dict) -> str | None:
    """Body must contain at least one match for `value` (a regex string)."""
    pattern_str = str(value)
    try:
        pattern = re.compile(pattern_str, re.MULTILINE)
    except re.error as e:
        return f"regex_required: invalid pattern {pattern_str!r}: {e}"
    if not pattern.search(body):
        return f"required regex not found: {pattern_str!r}"
    return None


def _check_preface_required(body: str, value: Any, _: dict) -> str | None:
    """YAML frontmatter required at top of body (between --- fences).

    `value` is bool. When true, the body must start with `---\\n`,
    contain at least one `key: value` line, and have a closing `---`.
    """
    if not bool(value):
        return None
    text = body.lstrip()
    if not text.startswith("---"):
        return "missing YAML preface (frontmatter) at top of body"
    # Find the closing fence on its own line.
    after_first = text[3:]
    if "\n---" not in after_first:
        return "YAML preface has no closing '---'"
    block = after_first.split("\n---", 1)[0]
    if not re.search(r"\w+\s*:", block):
        return "YAML preface block has no key:value pairs"
    return None


# Dispatch table. Add new rule types here.
_CHECKERS = {
    "min_words":            _check_min_words,
    "max_words":            _check_max_words,
    "section_present":      _check_section_present,
    "no_banned_phrases":    _check_no_banned_phrases,
    "max_lines":            _check_max_lines,
    "regex_required":       _check_regex_required,
    "preface_required":     _check_preface_required,
}
