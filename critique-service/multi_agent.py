"""Phase 5 — Multi-Agent via Strands Graph & Swarm.

Replaces the ad-hoc ``_run_sub_agent()`` in conscious_tools.py with proper
Strands multi-agent patterns:

* **Graph** — structured invoke/delegate with a single-node Graph per sub-agent.
  The sub-agent runs as a proper Strands ``Agent`` with its own model, tools,
  and ``invocation_state`` — no manual polling or ``AgentSession`` needed.
* **Swarm** — free-form collaboration where agents autonomously hand off via
  the auto-injected ``handoff_to_agent`` tool.

Usage::

    from multi_agent import run_invoke_graph, run_swarm

    # Invoke a single sub-agent
    result = run_invoke_graph(conscious_id, from_agent, to_agent, task, ...)

    # Run a collaborative swarm
    swarm_result = run_swarm(agent_configs, task, entry_point=...)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

import agent_sessions
import conscious_db
from conscious_tools import _list_worktree_files  # canonical version (Fix #2)

_log = logging.getLogger("multi_agent")

_AGENT_CACHE: dict[str, Any] = {}


def clear_cache() -> None:
    _AGENT_CACHE.clear()


class _SubSessionProxy:
    """Lightweight ``AgentSession`` proxy for sub-agents in a Graph/Swarm.

    Carries the sub-agent's ``conscious_id`` and ``agent_id`` through
    ``invocation_state`` so conscious tool handlers resolve the correct
    context without needing a full ``AgentSession`` instance.
    """
    __slots__ = ("conscious_id", "agent_id")

    def __init__(self, conscious_id: str, agent_id: str) -> None:
        self.conscious_id = conscious_id
        self.agent_id = agent_id


# ---------------------------------------------------------------------------
# make_sub_agent — dynamic Strands Agent factory
# ---------------------------------------------------------------------------

def make_sub_agent(
    agent_id: str,
    system_prompt: str,
    model: str | None = None,
) -> Any:
    """Create a Strands ``Agent`` instance for a sub-agent.

    The agent gets the standard tool suite (shell, file I/O, http, calculator)
    plus conscious tools.  No ``session_manager`` — only the Graph/Swarm
    orchestrator should hold one (Strands raises ``ValueError`` otherwise).
    Cached by ``agent_id`` so repeated invocations reuse the same instance.

    Args:
        agent_id: Unique identifier for this agent.
        system_prompt: Role-defining system prompt.
        model: LiteLLM model string (e.g. ``"groq/llama-3.3-70b-versatile"``).
               Falls back to ``agent_sessions._pick_open_llm()`` if ``None``.

    Returns:
        ``strands.Agent`` instance.
    """
    from strands import Agent
    from strands.models.litellm import LiteLLMModel

    cached = _AGENT_CACHE.get((agent_id, hash(system_prompt)))
    if cached is not None:
        return cached

    if model:
        key_env = agent_sessions._model_key_env(model) or os.environ.get("AGENT_OPEN_KEY_ENV", "")
        base_url = agent_sessions._model_base_url(model)
        if not key_env or not os.environ.get(key_env, "").strip():
            raise RuntimeError(f"no API key for model {model}")
    else:
        picked = agent_sessions._pick_open_llm()
        if picked is None:
            raise RuntimeError("no open-tier provider key available")
        key_env, model, base_url = picked

    client_args: dict = {"api_key": os.environ[key_env]}
    if base_url:
        client_args["api_base"] = base_url
    llm = LiteLLMModel(client_args=client_args, model_id=model)

    tools = _build_sub_tool_suite()

    try:
        import conscious_stubs
        tools += list(conscious_stubs.get_conscious_tools())
    except Exception:
        pass

    agent = Agent(
        name=agent_id,
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        context_manager="auto",
        callback_handler=None,
    )
    # cache key includes system_prompt hash so changing the task prompt
    # between invocations does not reuse a stale instance (Fix #6)
    _AGENT_CACHE[(agent_id, hash(system_prompt))] = agent
    return agent


def _build_sub_tool_suite() -> list:
    """Standard Strands tool list for a sub-agent (native shell, not guarded)."""
    tools = []
    for mod_name in ("file_read", "file_write", "editor",
                     "http_request", "python_repl", "calculator", "load_tool"):
        try:
            import importlib
            tools.append(importlib.import_module(f"strands_tools.{mod_name}"))
        except Exception:
            continue
    for grep_mod in ("grep", "search_files", "grep_code"):
        try:
            import importlib
            tools.append(importlib.import_module(f"strands_tools.{grep_mod}"))
            break
        except Exception:
            continue
    try:
        from strands_tools import shell
        import types as _types
        sm = _types.ModuleType("shell_sub")
        sm.tool = shell.tool
        tools.append(sm)
    except Exception:
        pass
    return tools


# ---------------------------------------------------------------------------
# system prompt builder
# ---------------------------------------------------------------------------

def build_sub_system_prompt(
    conscious_id: str,
    agent_id: str,
    task: str,
    workspace_path: str | None = None,
    inputs: dict | None = None,
) -> str:
    """Build the system prompt for a sub-agent being invoked via Graph.

    Mirrors the context-rich prompt from ``AgentSession.__init__()`` so the
    sub-agent understands its role, workspace, and conscious binding.
    """
    lines = [
        f"You are agent {agent_id} in the '{conscious_id}' conscious.",
        "",
        f"Your assigned task: {task}",
    ]
    if workspace_path:
        lines.append(f"Your workspace: {workspace_path}")
    if inputs:
        lines.append(f"Additional inputs: {inputs}")

    lines.extend([
        "",
        "Tools available:",
        "- shell: run bash commands (ls, cat, git, python, etc.)",
        "- file_read / file_write / editor: file operations",
        "- http_request: web access",
        "- python_repl: fallback for complex logic",
        "- calculator: math",
        "- conscious_context: read the conscious brain (goal, blackboard, events)",
        "- conscious_propose: propose blackboard writes for the orchestrator",
        "- conscious_drawer: read drawer entries",
        "- conscious_message: send messages to other agents",
        "",
        "Read the conscious blackboard via conscious_context first to understand",
        "current state. Use conscious_propose for brain mutations. Write your",
        "findings to the workspace. Report your complete results at the end.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run_invoke_graph — single sub-agent via Strands Graph
# ---------------------------------------------------------------------------

def run_invoke_graph(
    conscious_id: str,
    from_agent_id: str,
    to_agent_id: str,
    task: str,
    inputs: dict[str, Any] | None = None,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Invoke a sub-agent using a Strands single-node Graph.

    Creates a dynamic Strands Graph with one node (the sub-agent), runs the
    task through it synchronously, and returns the result.  The sub-agent is
    created from its ``conscious_db`` configuration with full tool access.
    ``invocation_state`` carries the conscious context so tools can resolve
    the blackboard without hitting ``conscious_db`` on every call.

    Fallback: if ``strands.multiagent`` is not importable, delegates to the
    simulated-agent path from ``conscious_tools._simulate_agent_work``.

    Returns:
        dict with keys: ``result``, ``files``, ``status``, ``error``,
        ``execution_time_ms``.
    """
    sub = conscious_db.get_agent(to_agent_id)
    if not sub or sub.get("conscious_id") != conscious_id:
        return {
            "result": f"error: agent {to_agent_id} not found in this conscious",
            "files": [], "status": "failed",
            "error": "agent not found",
        }

    ws_path = sub.get("worktree_path")
    system_prompt = build_sub_system_prompt(
        conscious_id, to_agent_id, task,
        workspace_path=ws_path, inputs=inputs,
    )

    try:
        agent = make_sub_agent(
            agent_id=to_agent_id,
            system_prompt=system_prompt,
            model=sub.get("model"),
        )
    except Exception as exc:
        return {
            "result": "", "files": [],
            "status": "failed", "error": f"agent creation failed: {exc}",
        }

    try:
        from strands.multiagent import GraphBuilder
    except ImportError:
        return _fallback_result(sub, task, inputs or {})

    graph_id = f"invoke-{conscious_id}-{to_agent_id}-{uuid.uuid4().hex[:8]}"
    builder = GraphBuilder()
    builder.add_node(agent, node_id=to_agent_id)
    builder.set_entry_point(to_agent_id)
    builder.set_execution_timeout(timeout_s)
    builder.set_graph_id(graph_id)
    graph = builder.build()

    inv_state: dict[str, Any] = {
        "agent_session": _SubSessionProxy(conscious_id, to_agent_id),
        "conscious_id": conscious_id,
        "agent_id": to_agent_id,
        "from_agent_id": from_agent_id,
        "inputs": inputs or {},
        "workspace_path": ws_path,
    }

    start = time.time()
    try:
        result = graph(task, invocation_state=inv_state)
        elapsed_ms = (time.time() - start) * 1000
        status_name = getattr(result, "status", None)
        status_str = status_name.name if status_name is not None else "COMPLETED"
        is_ok = status_str == "COMPLETED"
        node_res = (result.results or {}).get(to_agent_id, {})
        if is_ok:
            status = "done"
            error = None
            result_text = str(getattr(node_res, "result", str(node_res)))
        else:
            status = "failed"
            error = str(getattr(node_res, "result", getattr(node_res, "error", "graph execution failed")))
            result_text = ""
    except Exception as exc:
        elapsed_ms = (time.time() - start) * 1000
        result_text = ""
        status = "failed"
        error = str(exc)

    files = _list_worktree_files(ws_path) if ws_path else []
    return {
        "result": result_text,
        "files": files,
        "status": status,
        "error": error,
        "execution_time_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# run_delegate_graph — fire-and-forget via daemon thread
# ---------------------------------------------------------------------------

def run_delegate_graph(
    conscious_id: str,
    from_agent_id: str,
    to_agent_id: str,
    task: str,
    inputs: dict[str, Any] | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Fire-and-forget: run ``run_invoke_graph`` in a daemon thread.

    Returns immediately with an ``invoke_id`` and ``status="pending"``.
    The caller should poll via ``conscious_drawer`` to retrieve the result.

    Returns:
        dict with keys: ``invoke_id``, ``status`` (always ``"pending"``).
    """
    invoke_id = uuid.uuid4().hex[:16]

    def _run() -> None:
        try:
            result_data = run_invoke_graph(
                conscious_id, from_agent_id, to_agent_id,
                task, inputs, timeout_s,
            )
            conscious_db.complete_drawer_entry(
                invoke_id,
                result=result_data.get("result", ""),
                status=result_data.get("status", "failed"),
                error=result_data.get("error"),
            )
        except Exception as exc:
            try:
                conscious_db.complete_drawer_entry(
                    invoke_id, result="", status="failed", error=str(exc),
                )
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name=f"delegate-{invoke_id[:8]}").start()
    return {"invoke_id": invoke_id, "status": "pending"}


# ---------------------------------------------------------------------------
# run_swarm — free-form multi-agent collaboration
# ---------------------------------------------------------------------------

def run_swarm(
    agent_configs: list[dict[str, Any]],
    task: str,
    entry_point_id: str | None = None,
    timeout_s: int = 900,
    max_handoffs: int = 20,
    conscious_id: str | None = None,
) -> dict[str, Any]:
    """Run a Strands Swarm for free-form multi-agent collaboration.

    Each ``agent_config`` dict requires::

        {
            "agent_id": str,           # unique id
            "system_prompt": str,      # role prompt
            "model": str | None,       # optional LiteLLM model string
        }

    Args:
        agent_configs: List of agent configurations.
        task: The task description for the swarm.
        entry_point_id: Which agent starts (default: first in list).
        timeout_s: Max total execution time (default 15 min).
        max_handoffs: Max handoffs before forced termination (default 20).
        conscious_id: If set, passed in ``invocation_state`` so sub-agent
                      tools can resolve conscious context.

    Returns:
        dict with keys: ``status``, ``results`` (by agent_id),
        ``node_history`` (list of agent ids in execution order),
        ``execution_count``, ``execution_time_ms``, ``error``.
    """
    try:
        from strands.multiagent import Swarm
    except ImportError:
        return {
            "status": "error",
            "error": "Strands multiagent module not available",
            "results": {}, "node_history": [],
            "execution_count": 0, "execution_time_ms": 0,
        }

    agents = []
    entry_agent = None
    for cfg in agent_configs:
        try:
            agent = make_sub_agent(
                agent_id=cfg["agent_id"],
                system_prompt=cfg["system_prompt"],
                model=cfg.get("model"),
            )
            agents.append(agent)
            if entry_point_id and cfg["agent_id"] == entry_point_id:
                entry_agent = agent
        except Exception as exc:
            _log.warning("swarm agent %s skipped: %s", cfg["agent_id"], exc)

    if not agents:
        return {
            "status": "error", "error": "no agents could be created",
            "results": {}, "node_history": [],
            "execution_count": 0, "execution_time_ms": 0,
        }

    inv_state: dict[str, Any] = {}
    if conscious_id:
        inv_state["conscious_id"] = conscious_id
        inv_state["agent_session"] = _SubSessionProxy(
            conscious_id,
            entry_point_id or (agent_configs[0]["agent_id"] if agent_configs else "swarm"),
        )

    try:
        swarm = Swarm(
            agents,
            entry_point=entry_agent,
            max_handoffs=max_handoffs,
            execution_timeout=timeout_s,
            # stability: break infinite handoff loops (agent A → B → A → B ...)
            repetitive_handoff_detection_window=5,
            repetitive_handoff_min_unique_agents=2,
            id=f"swarm-{uuid.uuid4().hex[:8]}",
        )
        start = time.time()
        result = swarm(task, invocation_state=inv_state)
        elapsed_ms = (time.time() - start) * 1000

        status_name = getattr(result, "status", None)
        node_hist = [str(n.node_id) for n in getattr(result, "node_history", [])]
        results_dict: dict[str, str] = {}
        for nid, node_res in (result.results or {}).items():
            if hasattr(node_res, "result"):
                results_dict[nid] = str(node_res.result)
            else:
                results_dict[nid] = str(node_res)

        return {
            "status": status_name.name if status_name is not None else "done",
            "results": results_dict,
            "node_history": node_hist,
            "execution_count": getattr(result, "execution_count", 0),
            "execution_time_ms": elapsed_ms,
            "error": None,
        }
    except Exception as exc:
        return {
            "status": "failed", "error": str(exc),
            "results": {}, "node_history": [],
            "execution_count": 0, "execution_time_ms": 0,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fallback_result(sub: dict, task: str, inputs: dict) -> dict[str, Any]:
    """Fallback when Strands multiagent module is not importable.

    Uses the same simulation logic as ``conscious_tools._simulate_agent_work``.
    """
    if not sub.get("worktree_path"):
        return {
            "result": f"[stub] would invoke agent {sub.get('id', '?')} with task: {task}",
            "files": [], "status": "done", "error": None,
        }
    try:
        from conscious_tools import _simulate_agent_work
        result_text, files, status, error = _simulate_agent_work(sub, task, inputs)
    except Exception as exc:
        return {"result": "", "files": [], "status": "failed", "error": str(exc)}
    return {"result": result_text, "files": files, "status": status, "error": error}
