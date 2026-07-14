"""Phase 1 invoke/delegate stubs for the Conscious system (TIER3_PLAN.md §16.2).

Real worktree-per-agent execution lands in Phase 2. Phase 1 returns canned
results so the API contract is testable end-to-end via /api/conscious/* and
via the conscious_invoke / conscious_delegate tools.

This module is imported by ``agent_sessions.py`` when wiring up the conscious
tool registry in both adapters.
"""
from __future__ import annotations

import time
from typing import Any, Callable

# The tool registry constant exported by conscious_tools.py.
# Spelled out here as a string to avoid typos in the attribute access.
_TOOLS_ATTR = "CONSCIOUS_TOOLS"


def stub_invoke(to_agent_id: str, task: str, inputs: dict | None = None,
                timeout_s: int = 300) -> dict:
    """Synchronous stub: returns a canned 'done' result immediately.

    Mirrors the contract of ``conscious_invoke``:
    ``{"invoke_id": str, "status": "done|failed|timeout", "result": str, "files": []}``.
    """
    return {
        "invoke_id": _gen_id(),
        "status": "done",
        "result": (f"[stub] would invoke {to_agent_id} with: {task}\n"
                   f"inputs: {inputs or {}}\n"
                   f"(Phase 2 wires real worktree-per-agent execution)"),
        "files": [],
    }


def stub_delegate(to_agent_id: str, task: str, inputs: dict | None = None,
                  on_complete: Callable[[str, str], None] | None = None) -> dict:
    """Asynchronous stub: returns 'pending' immediately; completes after ~2s
    via a daemon thread that calls ``on_complete(invoke_id, result)`` if given.

    Mirrors the contract of ``conscious_delegate``:
    ``{"invoke_id": str, "status": "pending"}``.
    """
    invoke_id = _gen_id()

    def _finish() -> None:
        time.sleep(2.0)
        if on_complete:
            try:
                on_complete(invoke_id, f"[stub] delegated to {to_agent_id}: {task}")
            except Exception:
                pass

    import threading
    threading.Thread(target=_finish, daemon=True).start()
    return {"invoke_id": invoke_id, "status": "pending"}


def _gen_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def register_conscious_tools(session: Any, adapter: Any) -> None:
    """Attach the conscious tool registry to a session.

    Called by both ClaudeAdapter.open() and StrandsAdapter.open() after the
    adapter is initialized. The tools are made available to the agent via the
    adapter's native tool-registration mechanism (Phase 2 wires real SDK
    registration; Phase 1 stores them on the session for inspection/testing).
    """
    try:
        import conscious_tools
        session.conscious_tools = getattr(conscious_tools, _TOOLS_ATTR, [])
    except Exception:
        # conscious_tools import failure must never block adapter init
        session.conscious_tools = []
