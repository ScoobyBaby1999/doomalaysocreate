from __future__ import annotations
import contextvars
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

# structured operations log - one jsonl line per notable event in the pipeline.
# stage starts, llm calls, fanout shards, cooldowns, retries..
# log is the source of truth for post-run analysis.

# fire-and-forget: log_event never raises. observability shouldn't break the run.

log_path = Path(__file__).resolve().parent / "outputs" / "_orchestrator.jsonl"

#       run_id lives in a contextvar so deeply nested async calls
#       can include it without threading it through every signature.
#       set once at the top of a run, read anywhere.
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("oplog_run_id", default="")


def new_run_id() -> str:
    #   fresh 8-char id bound to the current context. called once per runner pass.
    rid = secrets.token_hex(4)
    run_id_var.set(rid)
    return rid


def log_event(kind: str, **fields: Any) -> None:
    #   append one structured event to the operation log.
    #   kind is the event classifier; **fields are anything else to capture.
    #   ts and run_id are added automatically.
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id_var.get(),
        "kind": kind,
    }
    record.update(fields)

    #       silently swallow disk/encoding errors. logging is non-critical.
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # append mode so concurrent fanout shards don't truncate each other.
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except (OSError, TypeError, ValueError):
        pass


def json_default(obj: Any) -> Any:
    #   best-effort serializer for things json doesn't natively know.
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "__fspath__"):
        return os.fspath(obj)
    return repr(obj)


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    #   write to <path>.tmp then rename - never leaves a half-written file
    #   if the process is killed mid-write. used for state.json, checkpoints,
    #   and anything else where partial reads would corrupt downstream state.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=indent, ensure_ascii=False, default=json_default),
        encoding="utf-8",
    )
    tmp.replace(path)
