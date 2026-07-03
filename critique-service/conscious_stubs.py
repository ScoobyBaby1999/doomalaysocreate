"""Phase 2 — Tool Registration + Skills.

Wraps the dict-based CONSCIOUS_TOOLS registry (conscious_tools.py) into proper
Strands ``@tool``-decorated functions with ToolContext access.  The resulting
tools receive ``agent_session`` through ``tool_context.invocation_state`` (set
per-turn by StrandsAdapter).

Usage in StrandsAdapter.open()::

    from conscious_stubs import get_conscious_tools
    tools += get_conscious_tools()
    ...
    self.agent(user_msg, agent_session=self._session_ref())

Usage in ClaudeAdapter: unchanged — ClaudeAdapter still calls
``register_conscious_tools()`` which stores the dict-based registry on the
session for backward compatibility.
"""
from __future__ import annotations

import threading
from typing import Any

import conscious_tools as _ct

# ---------------------------------------------------------------------------
# Strands-wrapper tools  (Phase 2 pattern)
# ---------------------------------------------------------------------------

_SELF = threading.local()  # holds a back-ref to the owning AgentSession


def _get_session() -> Any:
    """Return the AgentSession for the current thread (set per-turn)."""
    return getattr(_SELF, "agent_session", None)


def _wrap_one(entry: dict):
    """Create a ``@tool``-decorated function from one CONSCIOUS_TOOLS entry.

    The wrapper is generated once at module load time; at runtime it reads
    the current thread's AgentSession from ``_SELF.agent_session`` so Strands
    thread-pool workers still find the correct session.
    """
    try:
        from strands import tool as _strands_tool
        from strands.types.tools import ToolContext
    except ImportError:
        return None

    name: str = entry["name"]
    handler: Any = entry["handler"]
    schema: dict = entry.get("input_schema", {})
    description: str = entry.get("description", "")

    @_strands_tool(
        name=name,
        description=description,
        inputSchema={"json": schema},
        context=True,
    )
    def wrapper(tool_context: ToolContext, **kwargs) -> dict:
        agent_session = tool_context.invocation_state.get("agent_session") or _get_session()
        if agent_session is None:
            return {"error": "no agent session available"}
        return handler(agent_session, kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    return wrapper


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CONSCIOUS_TOOL_FUNCTIONS: list | None = None


def get_conscious_tools() -> list:
    """Return a list of Strands ``@tool``-decorated functions.

    Each function wraps the corresponding handler from ``conscious_tools.CONSCIOUS_TOOLS``.
    The result is cached after first build.
    """
    global _CONSCIOUS_TOOL_FUNCTIONS
    if _CONSCIOUS_TOOL_FUNCTIONS is not None:
        return _CONSCIOUS_TOOL_FUNCTIONS

    fns: list = []
    for entry in getattr(_ct, "CONSCIOUS_TOOLS", []):
        wrapped = _wrap_one(entry)
        if wrapped is not None:
            fns.append(wrapped)
    _CONSCIOUS_TOOL_FUNCTIONS = fns
    return fns


def register_conscious_tools(session: Any, adapter: Any) -> None:  # noqa: ARG001
    """Legacy hook — kept for ClaudeAdapter backward compatibility.

    Stores the dict-based ``CONSCIOUS_TOOLS`` on the session (Phase 1
    behaviour).  The Strands adapter no longer calls this — it uses
    ``get_conscious_tools()`` instead.
    """
    try:
        session.conscious_tools = getattr(_ct, "CONSCIOUS_TOOLS", [])
    except Exception:
        session.conscious_tools = []
