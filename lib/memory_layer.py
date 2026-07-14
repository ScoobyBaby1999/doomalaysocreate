"""Memory layer — the .pied sanity log.

A local, file-based memory system that acts as the sanity layer keeping all
agents working coherently. Inspired by the .pied architecture:

  - Local Memory (V1): No telemetry. File-based. Technically impossible to
    know vault contents from outside the vault.
  - Structured as a brain: hippocampus (hot index) → cortex (cold vault)
  - Salience tagging (amygdala): priority + recency
  - Replay/consolidation: nightly compaction of hot → cold

Implementation for doomalaysocreate:
  - Each workspace gets a .pied directory with:
    - _state.json: current state (goal, plan, active agents)
    - _log.jsonl: append-only event log (all agents write here)
    - blackboard/: shared knowledge (decisions, findings, artifacts)
    - agents/: per-agent logs
  - The memory is injected into each agent turn as context
  - Agents can read/write the memory via the memory tool
  - The log is the single source of truth — nothing gets stale or lost
"""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any


def memory_dir(workspace: Path) -> Path:
    """Get the .pied memory directory for a workspace."""
    d = workspace / ".pied"
    d.mkdir(parents=True, exist_ok=True)
    (d / "blackboard").mkdir(exist_ok=True)
    (d / "agents").mkdir(exist_ok=True)
    return d


def init_memory(workspace: Path, goal: str = "", plan: str = "") -> None:
    """Initialize the memory layer for a workspace."""
    d = memory_dir(workspace)
    state_file = d / "_state.json"
    if not state_file.exists():
        state = {
            "goal": goal,
            "plan": plan,
            "status": "active",
            "created_at": time.time(),
            "updated_at": time.time(),
            "active_agents": [],
            "completed_tasks": [],
            "pending_tasks": [],
        }
        state_file.write_text(json.dumps(state, indent=2))
    log_file = d / "_log.jsonl"
    if not log_file.exists():
        log_file.write_text("")


def log_event(workspace: Path, agent_id: str, kind: str, data: dict | None = None) -> None:
    """Append an event to the memory log. This is the sanity layer —
    every agent action is logged here so nothing gets lost."""
    d = memory_dir(workspace)
    entry = {
        "ts": time.time(),
        "agent": agent_id,
        "kind": kind,  # e.g. "tool_call", "decision", "finding", "error", "task_complete"
        "data": data or {},
    }
    with open(d / "_log.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Also update the state's updated_at
    try:
        state = read_state(workspace)
        state["updated_at"] = time.time()
        write_state(workspace, state)
    except Exception:
        pass


def read_state(workspace: Path) -> dict:
    """Read the current memory state."""
    d = memory_dir(workspace)
    state_file = d / "_state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def write_state(workspace: Path, state: dict) -> None:
    """Write the memory state."""
    d = memory_dir(workspace)
    state["updated_at"] = time.time()
    (d / "_state.json").write_text(json.dumps(state, indent=2))


def update_goal(workspace: Path, goal: str) -> None:
    """Update the workspace goal."""
    state = read_state(workspace)
    state["goal"] = goal
    write_state(workspace, state)
    log_event(workspace, "orchestrator", "goal_update", {"goal": goal})


def update_plan(workspace: Path, plan: str) -> None:
    """Update the workspace plan."""
    state = read_state(workspace)
    state["plan"] = plan
    write_state(workspace, state)
    log_event(workspace, "orchestrator", "plan_update", {"plan": plan})


def add_task(workspace: Path, task: str, agent_id: str = "orchestrator") -> None:
    """Add a pending task."""
    state = read_state(workspace)
    state.setdefault("pending_tasks", []).append({"task": task, "assigned_to": agent_id, "status": "pending"})
    write_state(workspace, state)
    log_event(workspace, agent_id, "task_assigned", {"task": task})


def complete_task(workspace: Path, task: str, agent_id: str = "orchestrator") -> None:
    """Mark a task as complete."""
    state = read_state(workspace)
    state.setdefault("completed_tasks", []).append({"task": task, "agent": agent_id, "completed_at": time.time()})
    state["pending_tasks"] = [t for t in state.get("pending_tasks", []) if t.get("task") != task]
    write_state(workspace, state)
    log_event(workspace, agent_id, "task_complete", {"task": task})


def post_blackboard(workspace: Path, section: str, key: str, value: str, agent_id: str = "orchestrator") -> None:
    """Post a blackboard entry (decision, finding, artifact, etc.)."""
    import re
    # Sanitize section and key for filesystem safety
    if not re.match(r'^[A-Za-z0-9_-]+$', section):
        raise ValueError(f"invalid section: {section!r}")
    if not re.match(r'^[A-Za-z0-9_-]+$', key):
        raise ValueError(f"invalid key: {key!r}")
    d = memory_dir(workspace)
    bb_file = d / "blackboard" / f"{section}.jsonl"
    entry = {
        "ts": time.time(),
        "agent": agent_id,
        "key": key,
        "value": value,
    }
    with open(bb_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log_event(workspace, agent_id, "blackboard_post", {"section": section, "key": key})


def read_blackboard(workspace: Path, section: str | None = None) -> list[dict]:
    """Read blackboard entries (optionally filtered by section)."""
    import re
    d = memory_dir(workspace)
    bb_dir = d / "blackboard"
    entries = []
    if section:
        if not re.match(r'^[A-Za-z0-9_-]+$', section):
            raise ValueError(f"invalid section: {section!r}")
        files = [bb_dir / f"{section}.jsonl"]
    else:
        files = list(bb_dir.glob("*.jsonl"))
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return entries


def get_context_for_agent(workspace: Path, max_chars: int = 4000) -> str:
    """Build a context string from the memory layer to inject into the agent's
    system prompt. This is how the sanity layer keeps agents up to date."""
    state = read_state(workspace)
    d = memory_dir(workspace)

    parts = []
    parts.append("=== MEMORY LAYER (.pied) ===")
    parts.append(f"Goal: {state.get('goal', '(not set)')}")
    parts.append(f"Plan: {state.get('plan', '(not set)')}")
    parts.append(f"Status: {state.get('status', 'active')}")

    # Recent log entries (last 10)
    log_file = d / "_log.jsonl"
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        recent = lines[-10:] if len(lines) > 10 else lines
        parts.append(f"\nRecent events ({len(recent)} of {len(lines)}):")
        for line in recent:
            try:
                ev = json.loads(line)
                ts_str = time.strftime("%H:%M:%S", time.gmtime(ev.get("ts", 0)))
                parts.append(f"  [{ts_str}] {ev.get('agent','?')}: {ev.get('kind','?')} {str(ev.get('data',''))[:100]}")
            except Exception:
                pass

    # Pending tasks
    pending = state.get("pending_tasks", [])
    if pending:
        parts.append(f"\nPending tasks ({len(pending)}):")
        for t in pending:
            parts.append(f"  - {t.get('task','?')} (assigned: {t.get('assigned_to','?')})")

    # Completed tasks
    completed = state.get("completed_tasks", [])
    if completed:
        parts.append(f"\nCompleted tasks ({len(completed)}):")
        for t in completed[-5:]:  # last 5
            parts.append(f"  + {t.get('task','?')}")

    # Blackboard highlights (last entry per section)
    bb_dir = d / "blackboard"
    if bb_dir.exists():
        for f in sorted(bb_dir.glob("*.jsonl")):
            lines = f.read_text().splitlines()
            if lines:
                try:
                    last = json.loads(lines[-1])
                    section_name = f.stem
                    parts.append(f"\n[{section_name}] {last.get('key','?')}: {str(last.get('value',''))[:200]}")
                except Exception:
                    pass

    result = "\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (truncated)"
    return result


def get_recent_log(workspace: Path, since_ts: float = 0, limit: int = 50) -> list[dict]:
    """Get recent log entries since a timestamp. Used for polling."""
    d = memory_dir(workspace)
    log_file = d / "_log.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text().splitlines():
        try:
            ev = json.loads(line)
            if ev.get("ts", 0) >= since_ts:
                entries.append(ev)
        except Exception:
            pass
    return entries[-limit:]


def agent_log(workspace: Path, agent_id: str, entry: str) -> None:
    """Write to an agent's personal log."""
    import re
    if not re.match(r'^[A-Za-z0-9_-]+$', agent_id):
        raise ValueError(f"invalid agent_id: {agent_id!r}")
    d = memory_dir(workspace)
    log_file = d / "agents" / f"{agent_id}.log"
    with open(log_file, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}] {entry}\n")
