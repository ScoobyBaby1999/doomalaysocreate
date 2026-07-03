"""Phase 3 — Hooks: CostCeiling, GitIntercept, Telemetry.

Each function returns a Strands hook callback (``Callable[[Event], None]``)
that can be registered via ``agent.add_hook(callback, EventType)``.

Usage in StrandsAdapter.open()::

    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
    from agent_hooks import make_cost_hook, make_git_intercept_hook

    if sess.conscious_id:
        agent.add_hook(make_cost_hook(sess.conscious_id, adapter), AfterToolCallEvent)
    agent.add_hook(make_git_intercept_hook(adapter), BeforeToolCallEvent)
    agent.add_hook(make_telemetry_hook(), AfterToolCallEvent)
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_log = logging.getLogger("agent.hooks")

# ---------------------------------------------------------------------------
# Cost hook
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf /*", "mkfs.", "dd if=", "> /dev/sda", "format",
    ":(){", "shutdown", "reboot", "init 0", "poweroff", "halt",
    "chmod 777 /", "wget -O-", "curl.*| sh", "curl.*| bash",
]


def make_cost_hook(conscious_id: str, adapter: Any) -> Callable[[Any], None]:
    """Return a hook callback for AfterToolCallEvent that enforces cost ceiling.

    The callback checks ``adapter._session_ref().conscious_id`` cost after
    each tool invocation.  If exceeded it emits a status event and relies on
    the session's turn-boundary check to refuse further turns.
    """
    def _after_tool(event: Any) -> None:  # noqa: ARG001
        try:
            sess = adapter._session_ref()
            if sess is None:
                return
            from agent_sessions import _cost_turn_ok, _cost_figures
            if not _cost_turn_ok(conscious_id):
                spent, ceiling = _cost_figures(conscious_id)
                sess.emit({
                    "type": "status", "state": "idle",
                    "detail": f"cost ceiling exceeded (spent=${spent:.4f}, ceiling=${ceiling:.4f})",
                })
                try:
                    import conscious_db as _cdb
                    _cdb.append_event(
                        conscious_id, "cost.exceeded",
                        f"after-tool hook: spent=${spent:.4f} ceiling=${ceiling:.4f}",
                        author=getattr(sess, "agent_id", None))
                except Exception:
                    pass
        except Exception:
            _log.exception("cost hook error")

    return _after_tool


# ---------------------------------------------------------------------------
# Git intercept + safety hook
# ---------------------------------------------------------------------------

def make_git_intercept_hook(adapter: Any) -> Callable[[Any], None]:
    """Return a hook callback for BeforeToolCallEvent.

    On shell/bash tool calls:
    1. Injects ``workdir`` from the adapter's workspace (replaces thread-local).
    2. Blocks dangerous command patterns.
    3. Routes git push/pull/fetch through the backend.
    """
    def _before_tool(event: Any) -> None:
        tu = getattr(event, "tool_use", None)
        if not isinstance(tu, dict):
            return
        name = tu.get("name", "")
        if name not in ("shell", "bash"):
            return
        inp = tu.get("input", {})
        cmd = (inp.get("command") or "").strip()
        if not cmd:
            return

        # 1. Force workdir
        inp["workdir"] = str(adapter.workspace)

        # 2. Block dangerous patterns
        for pat in _DANGEROUS_PATTERNS:
            if pat in cmd:
                event.cancel_tool = f"Blocked dangerous command pattern: {pat}"
                return

        # 3. Route network git operations
        sess = adapter._session_ref()
        workspace_id = getattr(sess, "workspace_id", None) if sess else None
        if workspace_id and (
            cmd.startswith("git push")
            or cmd.startswith("git pull")
            or cmd.startswith("git fetch")
        ):
            is_force = cmd.startswith("git push") and (
                "-f " in cmd or "--force" in cmd
            )
            if not is_force:
                from agent_sessions import _route_network_git
                result = _route_network_git(cmd, workspace_id)
                event.cancel_tool = str(
                    (result.get("content") or [{}])[0].get("text", "git operation done")
                )
                return

    return _before_tool


# ---------------------------------------------------------------------------
# Telemetry hook
# ---------------------------------------------------------------------------

def make_telemetry_hook(logger: logging.Logger | None = None) -> Callable[[Any], None]:
    """Return a hook callback for AfterToolCallEvent that logs tool usage."""
    log = logger or _log

    def _after_tool(event: Any) -> None:
        tu = getattr(event, "tool_use", None) or {}
        dur = getattr(event, "duration_ms", 0) / 1000 if hasattr(event, "duration_ms") else 0
        log.info("tool=%s duration=%.3fs", tu.get("name", "?"), dur)

    return _after_tool
