from __future__ import annotations
import dataclasses
import json
import os
from pathlib import Path

import artifacts
from oplog import log_event
from orchestrator.schematic import (
    SchemaValidationError,
    TaskSchematic,
    from_json_text,
    parse_and_validate,
)

# Service-side glue for the multi-stage orchestrator: discover built-in templates,
# resolve a request into a validated TaskSchematic, and serialize a RunResult for
# the HTTP layer. Reuses orchestrator.schematic (the template contract) and
# artifacts (the file protocol). The orchestrator itself runs in jobs.py /
# run_sync against the shared scheduler + metrics.

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", HERE / "orchestrator" / "templates"))


class OrchestrateError(ValueError):
    """invalid /api/run request (bad template id or schematic) -> 400."""


def load_template_paths() -> dict[str, Path]:
    #   built-in templates keyed by filename stem (e.g. "research_paper").
    out: dict[str, Path] = {}
    if TEMPLATES_DIR.is_dir():
        for p in sorted(TEMPLATES_DIR.glob("*.json")):
            out[p.stem] = p
    return out


def get_template(template_id: str) -> TaskSchematic:
    paths = load_template_paths()
    path = paths.get(template_id)
    if path is None:
        raise OrchestrateError(
            f"unknown template {template_id!r}; available: {sorted(paths)}")
    try:
        return from_json_text(path.read_text(encoding="utf-8"))
    except SchemaValidationError as e:
        raise OrchestrateError(f"template {template_id!r} is invalid: {e}")


def list_templates() -> list[dict]:
    rows = []
    for tid, path in load_template_paths().items():
        try:
            schem = from_json_text(path.read_text(encoding="utf-8"))
        except SchemaValidationError:
            continue
        jc = schem.judge_config
        rows.append({
            "id": tid,
            "task_type": schem.task_type,
            "stages": [{"name": s.name, "role": s.role.value} for s in schem.stages],
            "output_format": schem.output_rules.get("format", "markdown"),
            "judge": {"rules": len(jc.get("rules", [])),
                      "plugins": jc.get("plugins", []),
                      "llm_judges": len(jc.get("llm_judges", []))},
            "max_rounds": schem.max_rounds,
        })
    return rows


# minimal built-in fallback when a request gives a prompt but no template/schematic.
_FREEFORM = {
    "task_type": "freeform", "task": "freeform",
    "stages": [{"name": "write", "role": "generator",
                "instructions": "Produce exactly what the user asked for. No preamble, "
                                "no meta-commentary; begin with the content directly.",
                "inputs": ["prompt"], "max_tokens": 4000}],
    "output_rules": {"format": "markdown"},
    "judge_config": {"rules": [], "plugins": [], "llm_judges": []},
    "max_rounds": 1,
}


def resolve_schematic(prompt: str, *, template_id: str | None,
                      schematic_obj: dict | None, label: str = "") -> tuple[TaskSchematic, str]:
    #   precedence: inline schematic > built-in template > freeform fallback.
    #   returns (schematic, nonce). nonce is non-empty iff output is files.
    if schematic_obj is not None:
        try:
            schem = parse_and_validate(schematic_obj)
        except SchemaValidationError as e:
            raise OrchestrateError(f"invalid 'schematic': {e}")
    elif template_id:
        schem = get_template(template_id)
    else:
        schem = parse_and_validate(_FREEFORM)

    #   stamp a human label if the template left the placeholder in.
    if schem.task.startswith("<FILL") or not schem.task:
        schem = dataclasses.replace(schem, task=(label or prompt[:60]).strip() or schem.task_type)

    nonce = artifacts.make_nonce() if schem.output_rules.get("format") == "files" else ""
    return schem, nonce


def serialize_run_result(res, *, task: str) -> dict:
    #   shape a RunResult for the HTTP response / job snapshot.
    report = res.final_report
    return {
        "ok": res.ok,
        "task": task,
        "body": res.body,
        "artifacts": list(res.artifacts or []),
        "rounds": res.rounds,
        "decompositions": res.decompositions,
        "judge": {
            "ok": (report.ok if report is not None else None),
            "hard_fails": (report.hard_fails if report is not None else []),
            "soft_flags": (report.soft_flags if report is not None else []),
        },
        "stages": [{"name": s.name, "role": s.role.value, "provider": s.provider,
                    "family": s.family, "ok": s.ok, "duration_s": s.duration_s,
                    "error": s.error} for s in res.stages],
        "providers_used": res.providers_used,
        "error": res.error,
    }
