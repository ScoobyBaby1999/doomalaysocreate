from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from .roles import Roles


class OutputRules(TypedDict, total=False):
    format: Literal["markdown", "text", "json", "html", "yaml"]
    min_words: int
    max_words: int
    required_sections: list[str]
    banned_phrases: list[str]
    tone: str


@dataclass(frozen=True)
class Fanout:                  # run a stage once for every item in a list, in parallel
    over: str                  # context path that points to the list
    max_parallel: int = 3


@dataclass(frozen=True)
class Stage:
    name: str
    role: Roles
    instructions: str
    inputs: tuple[str, ...] = ()
    fanout: Fanout | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class Task:
    task_type: str
    task: str
    stages: tuple[Stage, ...]
    output_rules: OutputRules


class SchemaError(ValueError):
    """raised when planner output doesn't satisfy the task contract."""


def stage_inspect(payload: dict, idx: int) -> Stage:
    if not isinstance(payload, dict):
        raise SchemaError(f"stages[{idx}] is not an object")

    for required in ("name", "role", "instructions"):
        if required not in payload:
            raise SchemaError(f"stages[{idx}] missing required field '{required}'")

    name = payload["name"]
    if not isinstance(name, str) or not name:
        raise SchemaError(f"stages[{idx}].name must not be empty")

    role_str = payload["role"]
    try:
        role = Roles(role_str)
    except ValueError:
        raise SchemaError(
            f"stages[{idx}].role={role_str!r} not in {[r.value for r in Roles]}"
        )

    instructions = payload["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise SchemaError(f"stages[{idx}].instructions must be non-empty string")

    inputs_raw = payload.get("inputs", [])
    if not isinstance(inputs_raw, list) or any(not isinstance(k, str) for k in inputs_raw):
        raise SchemaError(f"stages[{idx}].inputs must be a list of strings")
    inputs = tuple(inputs_raw)

    fanout: Fanout | None = None
    if payload.get("fanout") is not None:
        fanout_raw = payload["fanout"]
        if not isinstance(fanout_raw, dict) or "over" not in fanout_raw:
            raise SchemaError(
                f"stages[{idx}].fanout must be {{over: <path>, max_parallel: <int>}}"
            )
        if not isinstance(fanout_raw["over"], str):
            raise SchemaError(f"stages[{idx}].fanout.over must be a string")
        max_parallel = fanout_raw.get("max_parallel", 3)
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise SchemaError(f"stages[{idx}].fanout.max_parallel must be positive int")
        fanout = Fanout(over=fanout_raw["over"], max_parallel=max_parallel)

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
        raise SchemaError(f"stages[{idx}].max_tokens must be positive int or null")

    return Stage(
        name=name,
        role=role,
        instructions=instructions,
        inputs=inputs,
        fanout=fanout,
        max_tokens=max_tokens,
    )


def output_inspect(payload: Any) -> OutputRules:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise SchemaError("output_rules must be an object")
    return cast(OutputRules, dict(payload))


def task_inspect(data: Any) -> Task:
    if not isinstance(data, dict):
        raise SchemaError("top-level value must be an object")

    for required in ("task_type", "task", "stages"):
        if required not in data:
            raise SchemaError(f"missing required task field: {required!r}")

    task_type = data["task_type"]
    if not isinstance(task_type, str) or not task_type:
        raise SchemaError("task_type must not be empty")

    task = data["task"]
    if not isinstance(task, str) or not task:
        raise SchemaError("task must not be empty")

    stages_raw = data["stages"]
    if not isinstance(stages_raw, list) or not stages_raw:
        raise SchemaError("stages must be a non-empty list")
    stages = tuple(stage_inspect(stage, idx) for idx, stage in enumerate(stages_raw))

    names = [stage.name for stage in stages]
    if len(set(names)) != len(names):
        dupes = [n for n in names if names.count(n) > 1]
        raise SchemaError(f"stage names must be unique; duplicates: {sorted(set(dupes))}")

    output_rules = output_inspect(data.get("output_rules", {}))

    return Task(
        task_type=task_type,
        task=task,
        stages=stages,
        output_rules=output_rules,
    )


def remove_header_footer(text: str) -> Task:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1: last_fence].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SchemaError(f"output is not valid JSON: {error}; first 200 chars: {text[:200]!r}")
    return task_inspect(data)


def load_json(path: Path) -> Task:
    return remove_header_footer(path.read_text(encoding="utf-8"))
