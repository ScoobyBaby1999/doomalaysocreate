from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import hashlib

from oplog import atomic_write_json, log_event

# stage-level checkpointing. saves the running orchestrator context after every
# stage advances, so a crash or network pause can resume from the last completed
# stage instead of restarting from scratch.
#
# storage: outputs/cache/<stem>.json. plain json - greppable, ~50-100KB after stitch.
# matching: by stage NAME only (no schematic hash). if you edit a stage's
# instructions and resume, the resume will skip that stage as already-done.
# rm outputs/cache/<stem>.json before resuming if you want a fresh start.

cache_dir = Path(__file__).resolve().parent.parent / "outputs" / "cache"


@dataclass
class Checkpoint:
    stem: str
    context: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[str] = field(default_factory=list)
    credits: list[dict] = field(default_factory=list)
    schematic_hash: str = ""


def path_for(stem: str) -> Path:
    return cache_dir / f"{stem}.json"


def load(stem: str) -> Checkpoint | None:
    cache_file = path_for(stem)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log_event("checkpoint_load_fail", stem=stem, reason=repr(e)[:200])
        return None
    return Checkpoint(
        stem=stem,
        context=data.get("context", {}),
        completed_stages=list(data.get("completed_stages", [])),
        credits=list(data.get("credits", [])),
        schematic_hash=data.get("schematic_hash", ""),
    )


def save(stem: str, context: dict[str, Any], completed_stages: list[str],
         credits: list[dict], schematic_hash: str = "") -> None:
    payload = {
        "stem": stem,
        "completed_stages": list(completed_stages),
        "credits": list(credits),
        "context": context,
        "schematic_hash": schematic_hash,
    }
    try:
        atomic_write_json(path_for(stem), payload)
    except OSError as e:
        log_event("checkpoint_save_fail", stem=stem, reason=repr(e)[:200])


def clear(stem: str) -> None:
    cache_file = path_for(stem)
    if cache_file.exists():
        try:
            cache_file.unlink()
            log_event("checkpoint_cleared", stem=stem)
        except OSError as e:
            log_event("checkpoint_clear_fail", stem=stem, reason=repr(e)[:200])
