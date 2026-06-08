"""
schematic.py - The TaskSchematic dataclass and its parse/validate logic.

# Why this lives in its own module

The TaskSchematic is the *contract* between three layers:
  1. Planner (LLM)        produces a TaskSchematic JSON
  2. Orchestrator         consumes it to drive stages
  3. Judge                consumes the judge_config sub-block

Anything that touches schematic shape goes through this file. If we ever
need to migrate to schematic v2 we'll add a v2 dataclass alongside and
write a converter — but the v1 contract is frozen here.

# Why frozen dataclasses + TypedDict

Frozen dataclasses for the top-level structure: a schematic is built once
per task and never mutated mid-run. Python's @dataclass(frozen=True) gives
us hashable, immutable, type-checked instances.

TypedDict for sub-blocks (output_rules, judge_config, individual rules):
the JSON we get from the Planner has these as plain dicts; converting to
nested dataclasses would mean two layers of conversion noise. TypedDicts
let the type checker enforce shape at every call site without runtime
conversion overhead.

# What's NOT in this file

- Skeleton prompts (those live in prompts/role_*.md)
- Per-role merge logic (lives in roles.py)
- Templates (those are JSON files in templates/, parsed via from_json())

This file is pure data structure + a parser. No I/O, no LLM calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from .roles import Role


# ---------- Sub-block types ----------

class Rule(TypedDict):
    """One declarative validation rule, evaluated by judge/engine.py.

    `type` selects which checker function runs; `value` is the parameter.
    The checker map is defined in judge/engine.py — adding a new rule
    type means writing one function there and registering it.
    """
    type: Literal[
        "min_words", "max_words", "section_present",
        "no_banned_phrases", "max_lines", "regex_required",
        "preface_required",
    ]
    value: int | str | list[str]


class JudgeConfig(TypedDict):
    """Judge configuration block of a TaskSchematic.

    rules:        declarative rule list (judge engine layer 1)
    plugins:      plugin tags to invoke (judge plugin layer 2);
                  e.g. ["citation_integrity", "url_health"]
    llm_judges:   natural-language criteria for LLM-as-judge (layer 3);
                  each becomes one VERIFIER call. Soft-flag verdicts only.
    """
    rules: list[Rule]
    plugins: list[str]
    llm_judges: list[str]


class OutputRules(TypedDict, total=False):
    """Cross-cutting output constraints, spliced into every role skeleton.

    All keys are optional (total=False); the renderer only emits bullets
    for keys that are present. Examples:
      format            "markdown" | "text" | "json" | "html" | "yaml"
      min_words         minimum word count (also enforced by Judge layer 1)
      max_words         maximum word count
      required_sections list of H2/H3 names that must appear
      banned_phrases    case-insensitive substrings the body must not contain
      tone              free-form style directive ("rigorous", "playful", etc)
    """
    format: Literal["markdown", "text", "json", "html", "yaml"]
    min_words: int
    max_words: int
    required_sections: list[str]
    banned_phrases: list[str]
    tone: str


# ---------- Stage shape ----------

@dataclass(frozen=True)
class FanoutSpec:
    """Marks a stage to run N times — once per item in `over`.

    `over` is a context path resolved against the run state. The most
    common form is "<extractor_name>.<list_field>" — e.g. the outline
    extractor produces context["outline"] = {"topics": [...]}, and a
    section_draft stage with fanout={"over": "outline.topics"} runs
    once per topic with shard_index = 0..len(topics)-1.

    `max_parallel` caps concurrent in-flight shards. The Orchestrator
    enforces "different provider per concurrent shard" — every running
    shard's provider is added to the scheduler's exclude_providers set
    until that shard finishes.
    """
    over: str
    max_parallel: int = 3


@dataclass(frozen=True)
class StageDef:
    """One step in the pipeline. Planner emits a list of these.

    Fields:
      name          free-form label, snake_case. Used for logging and as
                    the context key where this stage's output is stored.
                    Must be unique within a schematic.

      role          one of the six Role enum values. Drives slot selection
                    via scheduler.pick_slot(role) and prompt skeleton via
                    roles.render_skeleton(role).

      instructions  free-form natural-language directive the Planner writes.
                    Spliced into the role skeleton's {{INSTRUCTIONS}}
                    placeholder verbatim. Be specific — "draft sections 1-3"
                    not "draft some content".

      inputs        named context paths this stage consumes. Each path is
                    resolved by roles.render_inputs() and rendered as a
                    "## <path>\\n<value>" block in the user message. Empty
                    list = stage uses default inputs for its role
                    (generator reads "prompt", reviewer reads "body", etc).

      fanout        when set, run this stage once per item in fanout.over,
                    storing outputs at context[name][i]. Downstream stages
                    glob via inputs=["<name>.*"].

      max_tokens    override the role's default token budget. None = use
                    role default (16000 for generator/transformer, 4000
                    for reviewer, 2000 for extractor/planner, 200 for
                    verifier).

      on_judge_fail  policy for when the post-stage Judge call rejects
                     this stage's output:
                       "retry"  inject (reviewer, transformer) pair onto
                                queue, loop up to max_rounds (DEFAULT)
                       "abort"  fail the whole task immediately
                       "ignore" log soft warn, continue
    """
    name: str
    role: Role
    instructions: str
    inputs: tuple[str, ...] = ()
    fanout: FanoutSpec | None = None
    max_tokens: int | None = None
    on_judge_fail: Literal["retry", "abort", "ignore"] = "retry"


# ---------- The full schematic ----------

@dataclass(frozen=True)
class TaskSchematic:
    """The full plan. Planner emits one of these per user prompt.

    Fields:
      task_type      coarse classification used for telemetry and template
                     selection on future runs. Standard values:
                       "research_paper" | "lesson_plan" | "code_spec"
                       | "summary" | "translation" | "creative_writing"
                       | "freeform" | "custom"
                     Free-form strings allowed; classifier may produce
                     novel types.

      task           short human-readable label of what's being made.
                     Goes in output frontmatter and logs.

      stages         pipeline. Run sequentially. Judge can inject more
                     (reviewer, transformer) pairs on hard fails.

      output_rules   cross-cutting constraints. Spliced into every role
                     skeleton's {{OUTPUT_RULES_RENDERED}} placeholder.

      judge_config   Judge layer configuration. Empty {} means "no
                     validation, accept first generator output".

      max_rounds     cap on Judge-driven retry loops. Past this the task
                     accepts the best draft so far (or shelves to _failed/
                     if Judge still rejects all rounds).

      committee_size bob_the_builders fan-out for the PLANNER call itself.
                     Default 1 (solo Planner). Set to 3-5 for weak-model
                     setups where a single Planner output is unreliable;
                     N parallel Planner calls then a synthesizer call
                     merge into one schematic. Costs N+1 calls instead of 1.
    """
    task_type: str
    task: str
    stages: tuple[StageDef, ...]
    output_rules: OutputRules
    judge_config: JudgeConfig
    max_rounds: int = 3
    committee_size: int = 1


# ---------- JSON parsing & validation ----------

class SchemaValidationError(ValueError):
    """Raised when JSON doesn't satisfy the TaskSchematic contract.

    Exact failure detail is in the message. Used by planner.py to
    splice the error back into the next Planner attempt's prompt
    so the model knows what to fix.
    """


_VALID_RULE_TYPES = {
    "min_words", "max_words", "section_present",
    "no_banned_phrases", "max_lines", "regex_required",
    "preface_required",
}

_VALID_JUDGE_FAIL = {"retry", "abort", "ignore"}


def _parse_stage(d: dict, idx: int) -> StageDef:
    """Validate one stage dict from the Planner's JSON. Raises on any issue."""
    if not isinstance(d, dict):
        raise SchemaValidationError(f"stages[{idx}] is not an object")

    for required in ("name", "role", "instructions"):
        if required not in d:
            raise SchemaValidationError(
                f"stages[{idx}] missing required field '{required}'"
            )

    name = d["name"]
    if not isinstance(name, str) or not name:
        raise SchemaValidationError(f"stages[{idx}].name must be non-empty string")

    role_str = d["role"]
    try:
        role = Role(role_str)
    except ValueError:
        raise SchemaValidationError(
            f"stages[{idx}].role={role_str!r} not in "
            f"{[r.value for r in Role]}"
        )

    instructions = d["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise SchemaValidationError(
            f"stages[{idx}].instructions must be non-empty string"
        )

    inputs_raw = d.get("inputs", [])
    if not isinstance(inputs_raw, list) or any(
        not isinstance(x, str) for x in inputs_raw
    ):
        raise SchemaValidationError(
            f"stages[{idx}].inputs must be a list of strings"
        )
    inputs = tuple(inputs_raw)

    fanout: FanoutSpec | None = None
    if d.get("fanout") is not None:
        fo = d["fanout"]
        if not isinstance(fo, dict) or "over" not in fo:
            raise SchemaValidationError(
                f"stages[{idx}].fanout must be {{over: <path>, max_parallel: <int>}}"
            )
        if not isinstance(fo["over"], str):
            raise SchemaValidationError(
                f"stages[{idx}].fanout.over must be a string"
            )
        max_parallel = fo.get("max_parallel", 3)
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise SchemaValidationError(
                f"stages[{idx}].fanout.max_parallel must be positive int"
            )
        fanout = FanoutSpec(over=fo["over"], max_parallel=max_parallel)

    max_tokens = d.get("max_tokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int) or max_tokens < 1
    ):
        raise SchemaValidationError(
            f"stages[{idx}].max_tokens must be positive int or null"
        )

    on_judge_fail = d.get("on_judge_fail", "retry")
    if on_judge_fail not in _VALID_JUDGE_FAIL:
        raise SchemaValidationError(
            f"stages[{idx}].on_judge_fail={on_judge_fail!r} not in "
            f"{sorted(_VALID_JUDGE_FAIL)}"
        )

    return StageDef(
        name=name,
        role=role,
        instructions=instructions,
        inputs=inputs,
        fanout=fanout,
        max_tokens=max_tokens,
        on_judge_fail=on_judge_fail,
    )


def _parse_judge_config(d: Any) -> JudgeConfig:
    """Validate the judge_config sub-block. Allow it to be missing entirely."""
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise SchemaValidationError("judge_config must be an object")

    rules_raw = d.get("rules", [])
    if not isinstance(rules_raw, list):
        raise SchemaValidationError("judge_config.rules must be a list")
    rules: list[Rule] = []
    for ri, r in enumerate(rules_raw):
        if not isinstance(r, dict) or "type" not in r or "value" not in r:
            raise SchemaValidationError(
                f"judge_config.rules[{ri}] must be {{type, value}}"
            )
        if r["type"] not in _VALID_RULE_TYPES:
            raise SchemaValidationError(
                f"judge_config.rules[{ri}].type={r['type']!r} not in "
                f"{sorted(_VALID_RULE_TYPES)}"
            )
        rules.append(cast(Rule, {"type": r["type"], "value": r["value"]}))

    plugins = d.get("plugins", [])
    if not isinstance(plugins, list) or any(not isinstance(p, str) for p in plugins):
        raise SchemaValidationError("judge_config.plugins must be list of strings")

    llm_judges = d.get("llm_judges", [])
    if not isinstance(llm_judges, list) or any(
        not isinstance(j, str) for j in llm_judges
    ):
        raise SchemaValidationError(
            "judge_config.llm_judges must be list of strings"
        )

    return cast(JudgeConfig, {
        "rules": rules, "plugins": plugins, "llm_judges": llm_judges,
    })


def _parse_output_rules(d: Any) -> OutputRules:
    """Validate the output_rules sub-block. All keys optional."""
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise SchemaValidationError("output_rules must be an object")
    # We don't strictly type-check each key — TypedDict total=False means
    # extra keys are tolerated. The renderer in roles.py emits whatever
    # is present; missing keys just don't appear in the prompt.
    return cast(OutputRules, dict(d))


def parse_and_validate(data: Any) -> TaskSchematic:
    """Convert a parsed JSON dict into a validated TaskSchematic.

    `data` is the result of json.loads(planner_output) — a plain dict.
    Returns a frozen TaskSchematic on success; raises
    SchemaValidationError with a specific reason on failure.

    The error messages are designed to be useful both for the developer
    and for the Planner LLM (planner.py splices the error into its
    next-attempt prompt verbatim).
    """
    if not isinstance(data, dict):
        raise SchemaValidationError("top-level value must be an object")

    for required in ("task_type", "task", "stages"):
        if required not in data:
            raise SchemaValidationError(f"missing required top-level field: {required!r}")

    task_type = data["task_type"]
    if not isinstance(task_type, str) or not task_type:
        raise SchemaValidationError("task_type must be non-empty string")

    task = data["task"]
    if not isinstance(task, str) or not task:
        raise SchemaValidationError("task must be non-empty string")

    stages_raw = data["stages"]
    if not isinstance(stages_raw, list) or not stages_raw:
        raise SchemaValidationError("stages must be a non-empty list")
    stages = tuple(_parse_stage(s, i) for i, s in enumerate(stages_raw))

    # Stage-name uniqueness: outputs go in context[name], so duplicates
    # would silently overwrite each other. Catch this at validate-time.
    names = [s.name for s in stages]
    if len(set(names)) != len(names):
        dupes = [n for n in names if names.count(n) > 1]
        raise SchemaValidationError(
            f"stage names must be unique; duplicates: {sorted(set(dupes))}"
        )

    output_rules = _parse_output_rules(data.get("output_rules", {}))
    judge_config = _parse_judge_config(data.get("judge_config", {}))

    max_rounds = data.get("max_rounds", 3)
    if not isinstance(max_rounds, int) or max_rounds < 0:
        raise SchemaValidationError("max_rounds must be non-negative int")

    committee_size = data.get("committee_size", 1)
    if not isinstance(committee_size, int) or committee_size < 1:
        raise SchemaValidationError("committee_size must be positive int")

    return TaskSchematic(
        task_type=task_type,
        task=task,
        stages=stages,
        output_rules=output_rules,
        judge_config=judge_config,
        max_rounds=max_rounds,
        committee_size=committee_size,
    )


def from_json_text(text: str) -> TaskSchematic:
    """Parse JSON text → TaskSchematic. Strips a fenced code block if present.

    Free-tier models often wrap JSON in ```json ... ``` despite being
    asked not to; this is an annoying-but-recoverable failure mode that
    we handle quietly here rather than retrying. Real validation
    failures still raise SchemaValidationError.
    """
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SchemaValidationError(
            f"output is not valid JSON: {e}; first 200 chars: {text[:200]!r}"
        )
    return parse_and_validate(data)


def from_json_file(path: Path) -> TaskSchematic:
    """Load a shipped template (or cached schematic) from disk."""
    return from_json_text(path.read_text(encoding="utf-8"))
