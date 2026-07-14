from __future__ import annotations
import re
from enum import StrEnum
from pathlib import Path

# ported (trimmed) from doomalaysocreate's backend/content/roles.py. the critique service only
# needs: the Roles enum (so the ported scheduler/providers stay drop-in), the
# prompt-skeleton templating (get_role/make_role), and the refusal/fence helpers.
# the full per-role context-merge machinery from doomalaysocreate is intentionally left out -
# this service does its own validation in critique_service.py.


class Roles(StrEnum):
    planner = "planner"
    parser = "parser"
    critiquer = "critiquer"
    verifier = "verifier"
    generator = "generator"
    transformer = "transformer"
    assembler = "assembler"
    #   orchestrator roles (orchestrator/roles.py::Role). listed here so panel
    #   slots are eligible for every orchestrator stage role too (StrEnum members
    #   compare equal by string value across the two enums).
    reviewer = "reviewer"
    extractor = "extractor"


prompts_dir = Path(__file__).resolve().parent / "prompts"

#       cache skeleton md files for the lifetime of the process.
_cached_prompts: dict[str, str] = {}


def get_prompt(name: str) -> str:
    #   read prompts/<name>.md, cached. `name` is a bare rubric label such as
    #   "critiquer", "verifier", or "schematic_critiquer".
    if name in _cached_prompts:
        return _cached_prompts[name]
    text = (prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    _cached_prompts[name] = text
    return text


def get_role(role: Roles) -> str:
    return get_prompt(role.value)


def make_prompt(name: str, *, instructions: str, output_rules: str, inputs: str = "",
                template: str = "", required_schema: str = "") -> str:
    #   plain string substitution - skeletons stay authorable as plain markdown.
    #   unknown placeholders are replaced with empty strings.
    skeleton = get_prompt(name)
    return (
        skeleton.replace("{{INSTRUCTIONS}}", instructions)
        .replace("{{OUTPUT_RULES}}", output_rules)
        .replace("{{INPUTS}}", inputs)
        .replace("{{TEMPLATE}}", template)
        .replace("{{REQUIRED_SCHEMA}}", required_schema)
    )


def make_role(role: Roles, instructions: str, output_rules: str, inputs: str = "",
              template: str = "", required_schema: str = "") -> str:
    return make_prompt(role.value, instructions=instructions, output_rules=output_rules,
                       inputs=inputs, template=template, required_schema=required_schema)


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
