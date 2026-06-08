"""
roles.py - The Role enum, skeleton-prompt loader, and per-role merge logic.

# What lives here

Two concerns combined into one module because they're both "what does each
role MEAN at the system level":

  1. The closed `Role` StrEnum — the only six call shapes the system supports.
  2. `load_skeleton(role)` — reads role_<name>.md from prompts/ and returns
     the system-prompt template string with {{INSTRUCTIONS}}, {{INPUTS}},
     {{OUTPUT_RULES}} placeholders intact (the Orchestrator splices values).
  3. `merge(role, ...)` — per-role logic for "what do we do with this LLM's
     output once we have it?" Generator output replaces the body, extractor
     output is JSON-parsed and stored under stage.name, etc. This is the
     ROLE-SPECIFIC SHAPE VALIDATION that we discussed during design — we
     deliberately did NOT create a separate validate_shard() function;
     instead the per-role merge function is where shape parsing happens,
     because that's where modders adding a new role naturally write their
     extension code.

# Why a closed StrEnum

The Planner emits JSON containing a `role` field. We want to detect typos
("reviwer", "Generator", "tranformer") at the parse boundary, not silently
route the call into a wrong skeleton. StrEnum members compare equal to
their string values, so `Role("reviewer")` round-trips JSON cleanly and
`Role("reviwer")` raises ValueError immediately during _parse_and_validate.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any


# ---------- The closed role set ----------

class Role(StrEnum):
    """
    The six role archetypes. Each maps to a stable system-prompt skeleton
    in prompts/role_<value>.md and a slot-eligibility tag in providers.py.

      planner     - reads a prompt, emits a TaskSchematic JSON
      generator   - produces new content (long-form text, code, etc)
      reviewer    - emits structured critique bullets, no rewrite
      transformer - takes existing content + a directive, emits modified
      extractor   - pulls structured data (JSON) out of unstructured text
      verifier    - yes/no + reason JSON for one criterion (LLM-as-judge)
    """
    PLANNER = "planner"
    GENERATOR = "generator"
    REVIEWER = "reviewer"
    TRANSFORMER = "transformer"
    EXTRACTOR = "extractor"
    VERIFIER = "verifier"


# ---------- Skeleton prompt loader ----------

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Module-level cache so we don't re-read role files for every stage call.
# The skeleton files only change when the developer edits them, so caching
# for the lifetime of the process is safe and avoids hundreds of file IOs
# during a fanout run.
_SKELETON_CACHE: dict[Role, str] = {}


def load_skeleton(role: Role) -> str:
    """Return the raw skeleton text for a role, with placeholders intact.

    Placeholders the Orchestrator will splice:
      {{INSTRUCTIONS}}        the Planner's free-form per-stage instructions
      {{INPUTS_RENDERED}}     this stage's named inputs, formatted as text
      {{OUTPUT_RULES_RENDERED}} the schematic's output_rules as bullet list
      {{TEMPLATE_HINT}}       (planner role only) inlined template JSON
      {{EXPECTED_SCHEMA}}     (extractor role only) declared output schema
    """
    if role in _SKELETON_CACHE:
        return _SKELETON_CACHE[role]
    path = _PROMPTS_DIR / f"role_{role.value}.md"
    text = path.read_text(encoding="utf-8")
    _SKELETON_CACHE[role] = text
    return text


def render_skeleton(
    role: Role,
    instructions: str,
    output_rules_rendered: str,
    inputs_rendered: str = "",
    template_hint: str = "",
    expected_schema: str = "",
) -> str:
    """Render a role skeleton by substituting placeholders.

    Unknown placeholders (e.g. {{TEMPLATE_HINT}} on a non-planner role)
    get replaced with empty strings so the prompt never leaks
    `{{PLACEHOLDER}}` text to the model. We do plain string replacement
    rather than f-strings or Jinja so role files remain authorable as
    plain Markdown without escaping.
    """
    text = load_skeleton(role)
    return (
        text.replace("{{INSTRUCTIONS}}", instructions)
            .replace("{{INPUTS_RENDERED}}", inputs_rendered)
            .replace("{{OUTPUT_RULES_RENDERED}}", output_rules_rendered)
            .replace("{{TEMPLATE_HINT}}", template_hint)
            .replace("{{EXPECTED_SCHEMA}}", expected_schema)
    )


# ---------- Per-role merge logic ----------

class RoleMergeError(ValueError):
    """Raised by merge() when an LLM's output doesn't fit its declared role.

    Examples:
      - extractor returned prose instead of JSON
      - verifier returned text not matching {pass: bool, reason: str}
      - reviewer returned an empty body with no bullets

    The Orchestrator catches this, asks the scheduler to retry the stage
    with a different slot (different family, ideally), and on repeated
    failure either decomposes the stage (mid-run self-heal) or shelves
    the task.
    """


# Refusal-phrase regex used by GENERATOR/TRANSFORMER merges. If a model
# refuses to do the work ("I cannot help with that..."), we don't want
# that text smuggled into the body. Cheap regex up front; the scheduler
# treats a RoleMergeError as a model-quality failure and rotates.
_REFUSAL_RE = re.compile(
    r"^\s*(i\s+(cannot|can'?t|won'?t|am\s+unable|am\s+sorry)|"
    r"as\s+an\s+ai|i\s+do\s+not\s+have\s+access|"
    r"i\s+apologize,?\s+but)\b",
    re.IGNORECASE,
)


def _looks_like_refusal(text: str) -> bool:
    """True iff the first ~200 chars match a known refusal pattern."""
    head = text.strip()[:200]
    return bool(_REFUSAL_RE.match(head))


def merge(
    role: Role,
    stage_name: str,
    context: dict[str, Any],
    raw_output: str,
    *,
    shard_index: int | None = None,
) -> dict[str, Any]:
    """Apply a single LLM call's output to the run context.

    `context` is the Orchestrator's mutable run state. `raw_output` is the
    string the LLM returned. `shard_index` is set when this call is one
    shard of a fanout batch (so we know to store under context[name][i]
    instead of context[name]).

    Returns the (possibly mutated) context. Raises RoleMergeError on any
    role-specific shape failure so the Orchestrator can retry on a
    different slot.

    The merge function is the role's *behavior contract*. Modders adding
    a new role write a new merge case here (or a new module that subclasses
    a base merge protocol later). Until then this dispatch covers all
    six built-in roles.
    """
    if role is Role.GENERATOR:
        return _merge_generator(stage_name, context, raw_output, shard_index)
    if role is Role.TRANSFORMER:
        return _merge_transformer(stage_name, context, raw_output, shard_index)
    if role is Role.REVIEWER:
        return _merge_reviewer(stage_name, context, raw_output, shard_index)
    if role is Role.EXTRACTOR:
        return _merge_extractor(stage_name, context, raw_output, shard_index)
    if role is Role.VERIFIER:
        return _merge_verifier(stage_name, context, raw_output, shard_index)
    if role is Role.PLANNER:
        return _merge_planner(stage_name, context, raw_output, shard_index)
    raise RoleMergeError(f"unknown role: {role!r}")


def _merge_generator(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Generator output becomes the new body (or one shard of a fanout).

    Universal sanity: non-empty after strip, not a refusal phrase. If
    we have a fanout shard, store under context[stage_name][shard_index];
    otherwise replace context["body"].
    """
    text = raw.strip()
    if not text:
        raise RoleMergeError(f"generator '{stage_name}' returned empty content")
    if _looks_like_refusal(text):
        raise RoleMergeError(
            f"generator '{stage_name}' looks like a refusal: {text[:120]!r}"
        )

    if shard_index is not None:
        bucket = context.setdefault(stage_name, [])
        # Pad list so [i] = text even if shards finish out of order.
        while len(bucket) <= shard_index:
            bucket.append(None)
        bucket[shard_index] = text
    else:
        context["body"] = text
    return context


def _merge_transformer(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Transformer takes existing content + directive, emits a full revision.

    Same universal checks as generator. The transformer ALWAYS replaces
    the body (or shard slot) — it is by definition a "modify in place"
    operation. The previous body is preserved by the Orchestrator's
    history list if we ever want to roll back to it.
    """
    return _merge_generator(stage_name, context, raw, shard_index)


def _merge_reviewer(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Reviewer output is a critique to feed downstream stages.

    Stored under context["critique"] (or context[stage_name][i] for fanout
    reviewers). We require AT LEAST ONE bullet in the output — a reviewer
    that returns "looks fine" with no actionable items has failed its
    contract (the next transformer would have nothing to apply).

    A bullet is detected as a line starting with one of: "- ", "* ",
    or "<digit>." (numbered lists).
    """
    text = raw.strip()
    if not text:
        raise RoleMergeError(f"reviewer '{stage_name}' returned empty critique")
    has_bullet = any(
        line.lstrip().startswith(("- ", "* ", "+ ")) or
        (line.lstrip()[:2].rstrip(".").isdigit() and "." in line.lstrip()[:4])
        for line in text.splitlines()
    )
    if not has_bullet:
        raise RoleMergeError(
            f"reviewer '{stage_name}' produced no bulleted issues: {text[:120]!r}"
        )

    if shard_index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= shard_index:
            bucket.append(None)
        bucket[shard_index] = text
    else:
        context["critique"] = text
        context[stage_name] = text  # also under stage name for inputs= lookup
    return context


def _merge_extractor(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Extractor output is JSON; parse it and store under stage_name.

    The extractor role exists specifically to produce structured data
    downstream stages can read. If the JSON doesn't parse (or is the
    self-error sentinel `{"_error": "..."}`), we raise so the
    Orchestrator can retry with a different slot.

    Many free-tier models wrap JSON in ```json ... ``` fences. We
    strip those before parsing.
    """
    text = raw.strip()
    if not text:
        raise RoleMergeError(f"extractor '{stage_name}' returned empty output")

    # Strip a single fenced code block if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RoleMergeError(
            f"extractor '{stage_name}' did not return valid JSON: {e}; "
            f"first 200 chars: {text[:200]!r}"
        )
    if isinstance(parsed, dict) and "_error" in parsed:
        raise RoleMergeError(
            f"extractor '{stage_name}' self-reported error: {parsed['_error']!r}"
        )

    if shard_index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= shard_index:
            bucket.append(None)
        bucket[shard_index] = parsed
    else:
        context[stage_name] = parsed
    return context


def _merge_verifier(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Verifier output is {"pass": bool, "reason": str}.

    Verifier outputs do NOT live in the main body context — they're
    consumed by the Judge layer. Stored under context["__verdicts__"]
    (a list) so the LLM-judge orchestrator can collect them.
    """
    text = raw.strip()
    if not text:
        raise RoleMergeError(f"verifier '{stage_name}' returned empty output")

    # Same fence-stripping as extractor.
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Lenient fallback: if the model emitted prose containing the word
        # "pass: true/false", try to extract that. Many small models can't
        # reliably emit JSON but DO answer yes/no in prose.
        m = re.search(r'pass["\']?\s*[:=]\s*(true|false|yes|no)',
                      text, re.IGNORECASE)
        if m is None:
            raise RoleMergeError(
                f"verifier '{stage_name}' returned unparseable output: "
                f"{text[:200]!r}"
            )
        parsed = {
            "pass": m.group(1).lower() in ("true", "yes"),
            "reason": text[:200],
        }

    if not isinstance(parsed, dict) or "pass" not in parsed:
        raise RoleMergeError(
            f"verifier '{stage_name}' missing required 'pass' field: {parsed!r}"
        )
    parsed.setdefault("reason", "")

    verdicts = context.setdefault("__verdicts__", [])
    verdicts.append({"stage": stage_name, **parsed})
    return context


def _merge_planner(
    stage_name: str, context: dict, raw: str, shard_index: int | None,
) -> dict:
    """Planner sub-stages emit a TaskSchematic JSON.

    The Orchestrator handles the TOP-LEVEL Planner call (in planner.py),
    not via this merge function. This branch handles PLANNER stages
    that appear *inside* a schematic's stage list — used for nested
    planning (e.g. a research_plan stage that emits sub-questions per
    topic). For those, we just store the raw JSON under stage_name as
    a parsed dict; downstream stages reference it via inputs=["stage.field"].
    """
    text = raw.strip()
    if not text:
        raise RoleMergeError(f"planner '{stage_name}' returned empty output")

    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RoleMergeError(
            f"planner '{stage_name}' did not return valid JSON: {e}"
        )

    if shard_index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= shard_index:
            bucket.append(None)
        bucket[shard_index] = parsed
    else:
        context[stage_name] = parsed
    return context


# ---------- Helpers used by the Orchestrator ----------

def render_inputs(context: dict, input_paths: list[str]) -> str:
    """Resolve `inputs=["foo", "bar.0", "baz.*"]` against context, return text.

    Path syntax:
      "name"        - context["name"], rendered as text
      "name.{i}"    - context["name"][shard_index], used in fanout body
      "name.0"      - context["name"][0]
      "name.*"      - all elements of context["name"], joined with "\\n\\n---\\n\\n"
      "name.field"  - context["name"]["field"] (dict access)

    Output is a markdown-flavored block: each input is preceded by a
    `## <path>` header so the LLM can tell them apart in the prompt.
    """
    sections = []
    for path in input_paths:
        sections.append(f"## {path}")
        sections.append(_resolve_input(context, path))
    return "\n\n".join(sections)


def _resolve_input(context: dict, path: str) -> str:
    """Single-input resolver. See render_inputs() for path syntax."""
    if "." not in path:
        value = context.get(path, "")
    else:
        head, rest = path.split(".", 1)
        if rest == "*":
            items = context.get(head, [])
            return "\n\n---\n\n".join(_stringify(i) for i in items if i is not None)
        if rest.isdigit():
            items = context.get(head, [])
            i = int(rest)
            value = items[i] if 0 <= i < len(items) else ""
        else:
            value = context.get(head, {})
            for part in rest.split("."):
                if isinstance(value, dict):
                    value = value.get(part, "")
                else:
                    value = ""
                    break
    return _stringify(value)


def _stringify(value: Any) -> str:
    """Convert any context value to a string suitable for prompt embedding."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return str(value)


def render_output_rules(rules: dict) -> str:
    """Format an OutputRules TypedDict into a bullet list for the prompt."""
    if not rules:
        return "(no specific output rules)"
    lines = []
    for key, value in rules.items():
        if isinstance(value, list):
            lines.append(f"- **{key}**: {', '.join(map(str, value))}")
        else:
            lines.append(f"- **{key}**: {value}")
    return "\n".join(lines)
