from __future__ import annotations
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

# seven roles. six map to a system-prompt skeleton in content/prompts/<role>.md
# and a slot eligibility tag (see providers.make_model_role). the seventh,
# `assembler`, is special: it never calls an llm. its `instructions` field is
# a template string with {key.path} placeholders resolved against context.
#
# planner     -> emits a Task json (sub-schematic for nested planning)
# parser      -> structured json extraction from messy text
# critiquer   -> reviews content, emits a bullet list of issues
# verifier    -> {pass: bool, reason: str} for one criterion
# generator   -> produces new content
# transformer -> takes content + directive, returns the FULL revised content
# assembler   -> deterministic code-driven concatenation. zero llm cost.
#                used for non-destructive stitching when you have N parts to combine.

class Roles(StrEnum):
    planner = "planner"
    parser = "parser"
    critiquer = "critiquer"
    verifier = "verifier"
    generator = "generator"
    transformer = "transformer"
    assembler = "assembler"


prompts_dir = Path(__file__).resolve().parent / "prompts"

#       cache skeleton md files for the lifetime of the process.
#       they only change when a developer edits them - re-reading every shard
#       call would be wasteful on a fanout run.
cached_roles: dict[Roles, str] = {}


def get_role(role: Roles) -> str:
    if role in cached_roles:
        return cached_roles[role]
    role_template = prompts_dir / f"{role.value}.md"
    text = role_template.read_text(encoding="utf-8")
    cached_roles[role] = text
    return text


def make_role(role: Roles, instructions: str, output_rules: str, inputs: str = "",
              template: str = "", required_schema: str = "") -> str:
    #   plain string substitution - skeletons stay authorable as plain markdown.
    #   unknown placeholders are replaced with empty strings.
    skeleton = get_role(role)
    return (
        skeleton.replace("{{INSTRUCTIONS}}", instructions)
        .replace("{{OUTPUT_RULES}}", output_rules)
        .replace("{{INPUTS}}", inputs)
        .replace("{{TEMPLATE}}", template)
        .replace("{{REQUIRED_SCHEMA}}", required_schema)
    )


# refusal regex - many models say "i cannot help with that..." instead of doing
# the work. we don't want that text smuggled into the body.
refusal_pattern = re.compile(
    r"^\s*(i\s+(cannot|can'?t|won'?t|am\s+unable|am\s+sorry)|"
    r"as\s+an\s+ai|i\s+do\s+not\s+have\s+access|"
    r"i\s+apologize,?\s+but)\b",
    re.IGNORECASE,
)


def looks_like_refusal(text: str) -> bool:
    return bool(refusal_pattern.match(text.strip()[:200]))


# per-role merge logic - each role decides how to apply its raw output to the
# orchestrator's run context. the orchestrator catches ValueError from any
# merge and rotates to a different slot.

def merge(role: Roles, stage_name: str, context: dict[str, Any], output: str,
          *, index: int | None = None) -> dict[str, Any]:
    if role is Roles.generator:
        return merge_generator(stage_name, context, output, index)
    if role is Roles.transformer:
        return merge_transformer(stage_name, context, output, index)
    if role is Roles.critiquer:
        return merge_critiquer(stage_name, context, output, index)
    if role is Roles.verifier:
        return merge_verifier(stage_name, context, output, index)
    if role is Roles.parser:
        return merge_parser(stage_name, context, output, index)
    if role is Roles.planner:
        return merge_planner(stage_name, context, output, index)
    raise ValueError(f"unknown role: {role}")


def merge_generator(stage_name: str, context: dict, content: str,
                    index: int | None) -> dict:
    #   non-empty, not a refusal. fanout shard -> context[stage_name][i].
    #   non-fanout -> replace context["body"].
    text = content.strip()
    if not text:
        raise ValueError(f"generator {stage_name} returned empty content")
    if looks_like_refusal(text):
        raise ValueError(f"generator {stage_name} refused: {text[:200]!r}")

    if index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= index:
            bucket.append(None)
        bucket[index] = text
    else:
        context["body"] = text
        context[stage_name] = text
    return context


def merge_transformer(stage_name: str, context: dict, content: str,
                      index: int | None) -> dict:
    #   transformer writes a full revision - same shape contract as generator.
    return merge_generator(stage_name, context, content, index)


def merge_critiquer(stage_name: str, context: dict, content: str,
                    index: int | None) -> dict:
    #   critique must be bulleted - a "looks fine" pass is a contract violation
    #   because the next transformer would have nothing actionable to apply.
    text = content.strip()
    if not text:
        raise ValueError(f"critiquer {stage_name} returned empty content")
    has_bullet = any(
        line.lstrip().startswith(("- ", "* ", "+ "))
        or (line.lstrip()[:2].rstrip(".").isdigit() and "." in line.lstrip()[:4])
        for line in text.splitlines()
    )
    if not has_bullet:
        raise ValueError(f"critiquer {stage_name} unformatted: {text[:120]!r}")

    if index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= index:
            bucket.append(None)
        bucket[index] = text
    else:
        #       store under both 'critique' (default downstream input) and stage name
        #       so explicit inputs=["<stage>"] also work.
        context["critique"] = text
        context[stage_name] = text
    return context


def merge_parser(stage_name: str, context: dict, content: str,
                 index: int | None) -> dict:
    #   parser output is json. tolerate ```json fences. self-error sentinels
    #   ({"_error": "..."}) get turned into a merge failure so we rotate.
    text = strip_fence(content)
    if not text:
        raise ValueError(f"parser {stage_name} returned empty content")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"parser {stage_name} unparseable: {error}; first 200 chars: {text[:200]!r}"
        )
    if isinstance(parsed, dict) and "_error" in parsed:
        raise ValueError(f"parser {stage_name} self-error: {parsed['_error']!r}")

    if index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= index:
            bucket.append(None)
        bucket[index] = parsed
    else:
        context[stage_name] = parsed
    return context


def merge_verifier(stage_name: str, context: dict, content: str,
                   index: int | None) -> dict:
    #   verifier output is {"pass": bool, "reason": str}.
    #   lenient prose fallback for small models that can't do strict json.
    text = strip_fence(content)
    if not text:
        raise ValueError(f"verifier {stage_name} returned empty content")

    parsed: dict | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(
            r'pass["\']?\s*[:=]\s*(true|false|yes|no)', text, re.IGNORECASE,
        )
        if match is None:
            raise ValueError(f"verifier {stage_name} unparseable: {text[:200]!r}")
        parsed = {"pass": match.group(1).lower() in ("true", "yes"), "reason": text[:420]}

    if not isinstance(parsed, dict) or "pass" not in parsed:
        raise ValueError(f"verifier {stage_name} missing 'pass' field: {parsed!r}")
    parsed.setdefault("reason", "")

    if index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= index:
            bucket.append(None)
        bucket[index] = parsed
    else:
        context[stage_name] = parsed
    return context


def merge_planner(stage_name: str, context: dict, content: str,
                  index: int | None) -> dict:
    #   planner-as-stage emits sub-schematic json (used for nested planning,
    #   e.g. one planner shard per topic). top-level planning lives in planner.py.
    text = strip_fence(content)
    if not text:
        raise ValueError(f"planner {stage_name} returned empty content")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"planner {stage_name} unparseable: {error}")

    if index is not None:
        bucket = context.setdefault(stage_name, [])
        while len(bucket) <= index:
            bucket.append(None)
        bucket[index] = parsed
    else:
        context[stage_name] = parsed
    return context


# helpers

def strip_fence(text: str) -> str:
    #   strip a single ```lang ... ``` block if present. no-op otherwise.
    text = text.strip()
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    last_fence = text.rfind("```")
    if first_nl == -1 or last_fence <= first_nl:
        return text
    return text[first_nl + 1: last_fence].strip()


def build_input_context(context: dict, input_paths: list[str]) -> str:
    #   render each requested context path as a "## <path>\n<value>" block.
    sections = []
    for path in input_paths:
        sections.append(f"## {path}")
        sections.append(resolve_path(context, path))
    return "\n\n".join(sections)


def resolve_path(context: dict, path: str) -> str:
    #   path syntax:
    #     "name"        -> context["name"]
    #     "name.0"      -> context["name"][0]              (list index)
    #     "name.*"      -> all elements of context["name"], joined with separator
    #     "name.field"  -> context["name"]["field"]        (dict access)
    #     "a.b.c.0"     -> nested walk; mixes dict and list nodes
    #
    # never raises - returns "" on any unresolvable path. orchestrator passes
    # planner-emitted paths verbatim so we have to be tolerant.
    if "." not in path:
        return stringify(context.get(path, ""))

    head, rest = path.split(".", 1)

    #   "name.*" - splat join all elements of a list.
    if rest == "*":
        items = context.get(head, [])
        if not isinstance(items, list):
            return ""
        return "\n\n---\n\n".join(stringify(item) for item in items if item is not None)

    #   walk each remaining segment, switching between dict/list as we go.
    value: Any = context.get(head, "")
    for part in rest.split("."):
        if part == "*":
            if isinstance(value, list):
                return "\n\n---\n\n".join(stringify(item) for item in value if item is not None)
            return ""
        if isinstance(value, dict):
            value = value.get(part, "")
        elif isinstance(value, list):
            #       allow integer indexing into a list segment.
            if part.isdigit():
                idx = int(part)
                value = value[idx] if 0 <= idx < len(value) else ""
            else:
                value = ""
        else:
            value = ""
            break
    return stringify(value)


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return str(value)


def build_output_context(rules: dict) -> str:
    #   format output_rules as a bullet list for splicing into prompt skeletons.
    if not rules:
        return "(no specific output rules)"
    lines = []
    for key, value in rules.items():
        if isinstance(value, list):
            lines.append(f"- **{key}**: {', '.join(map(str, value))}")
        else:
            lines.append(f"- **{key}**: {value}")
    return "\n".join(lines)
