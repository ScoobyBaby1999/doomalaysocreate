"""
oplog.py - Structured operation log for the orchestrator pipeline.

# What this is

A single JSONL file (`vault/research/_orchestrator.jsonl`) where every
notable event in the pipeline writes one line. One run produces hundreds
to thousands of lines — Planner attempts, every LLM call, every fanout
shard start/finish, every judge layer outcome, every cooldown, every
retry injection.

The log is the source of truth for post-run analysis: "which provider
won today", "which family failed citation_integrity most", "did the
planner ever fall back to freeform". Earlier code wrote a smaller
_rate_log.jsonl with just HTTP-call events; this replaces and extends
that. _rate_log.jsonl is kept around (still written by scheduler) for
backward compatibility but new analysis should read _orchestrator.jsonl.

# Common envelope

Every line is a JSON object with at minimum:
  ts          ISO8601 UTC, second-precision, written by us at log time
  run_id      8-char random ID set once per `runner.main_async()` call;
              lets you grep one run out of a multi-run file
  kind        event classifier (run_start, stage_attempt, etc)
  prompt_stem stem of the prompt being processed, or null for
              run-level events (run_start/run_end)

Per-kind fields are documented below at each call site.

# Why contextvars

`run_id` and `prompt_stem` are set high in the call stack (runner,
_process_one) but every leaf function — every shard call, every
judge layer — needs to include them. Threading them through every
function signature would be invasive. ContextVar gives us implicit
inheritance through async calls: set once at the top, read anywhere.

# Fire-and-forget

`log_event` never raises. If the disk is full or the file's locked
or anything else goes wrong, we silently swallow the error rather
than break the pipeline. Logging is observability, not load-bearing
for correctness.
"""

from __future__ import annotations

import contextvars
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any


# ---------- File locations ----------

_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _ROOT / "vault" / "research" / "_orchestrator.jsonl"


# ---------- Context vars ----------

# Set at runner.main_async() entry; inherited by every nested call.
_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "oplog_run_id", default=""
)

# Set at runner._process_one() entry; reset at exit. Lets shard-level
# events tag which prompt they belong to without explicit threading.
_prompt_stem_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "oplog_prompt_stem", default=""
)


def new_run_id() -> str:
    """Generate a fresh 8-char random run ID and bind it to the context.

    Called once at runner.main_async() entry. Returns the ID so the caller
    can also include it in the initial log message.
    """
    rid = secrets.token_hex(4)
    _run_id_var.set(rid)
    return rid


def set_prompt_stem(stem: str) -> None:
    """Bind a prompt stem to the current context. Call at _process_one entry.

    Pair with `clear_prompt_stem()` at exit so subsequent run-level events
    don't carry a stale stem.
    """
    _prompt_stem_var.set(stem)


def clear_prompt_stem() -> None:
    """Unbind the prompt stem. Call at _process_one exit."""
    _prompt_stem_var.set("")


# ---------- The event writer ----------

def log_event(kind: str, **fields: Any) -> None:
    """Append one structured event to the operation log.

    `kind` is the event classifier; `**fields` are anything else worth
    capturing. The function adds `ts`, `run_id`, and `prompt_stem`
    automatically from the common envelope.

    Fire-and-forget: never raises. Logging failures should not break the
    pipeline, so this catches and silently swallows OSError, JSON encoding
    errors, and anything else that comes up.

    Examples:
      log_event("stage_start", stage="draft", role="generator", fanout=True)
      log_event("call_ok", provider="groq", model="llama-3.3-70b-versatile",
                family="llama-3.3-70b", duration_s=2.4, in_tokens=1500,
                out_tokens=800)
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": _run_id_var.get(),
        "kind": kind,
    }
    stem = _prompt_stem_var.get()
    if stem:
        record["prompt_stem"] = stem
    record.update(fields)

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode so concurrent fanout shards don't
        # truncate each other's writes. JSONL with one line per
        # call is the safe-under-concurrency format.
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    except (OSError, TypeError, ValueError):
        # Don't even bother re-raising — observability is non-critical.
        pass


def _json_default(obj: Any) -> Any:
    """Best-effort serializer for objects json can't natively handle.

    Falls back to repr() so structured logs still capture *something*
    rather than crashing on, say, a Path or set value.
    """
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "__fspath__"):
        return os.fspath(obj)
    return repr(obj)


# ---------- Convenience for run-end summary ----------

def get_run_id() -> str:
    """Return the current run_id (empty string if none set)."""
    return _run_id_var.get()
