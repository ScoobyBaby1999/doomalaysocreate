"""Agentic orchestrator sessions — the "agent" tab's backend.

Two tiers behind one adapter interface, auto-selected from the env *and* from
which SDK is actually installed:

  * "claude" — the official Claude Agent SDK (``claude-agent-sdk`` pip package,
    which bundles the Claude Code CLI inside the wheel — no Node needed).
    Active when ANTHROPIC_API_KEY is set and the SDK is importable.
  * "open"   — the OpenHands Agent SDK (``openhands-sdk`` + ``openhands-tools``),
    the open-source analog: terminal + file-editor tools, any OpenAI-compatible
    provider via LiteLLM. Active when a free-tier provider key is set and the
    SDK is importable. (Currently ships disabled — see the Dockerfile note —
    because OpenHands 1.28.x has an unresolvable opentelemetry/lmnr dependency
    conflict; this code activates automatically once a resolvable pair is added
    to requirements.txt. No code change needed.)

Sessions mirror the service's existing job pattern: POST starts/continues a
session, the client polls for an append-only event transcript. One daemon
worker thread per session; tool execution happens in-process/subprocess —
the user's own Space container IS the sandbox (bearer-gated, single-tenant).

stdlib-only at import time; SDK imports are deferred into the adapters so the
service still runs when neither SDK is installed (tier reports as None).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

AGENT_ROOT = Path(os.environ.get("AGENT_ROOT", "/tmp/agent"))
SESSION_TTL_S = int(os.environ.get("AGENT_SESSION_TTL_S", "7200"))   # 2h idle
MAX_SESSIONS = int(os.environ.get("AGENT_MAX_SESSIONS", "4"))        # RAM bound
MAX_EVENT_CHARS = int(os.environ.get("AGENT_MAX_EVENT_CHARS", "4000"))
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "50"))

HERE = Path(__file__).resolve().parent
SKILLS_SRC = HERE / "agent_skills"   # seeded into each workspace's .claude/skills

AGENT_SYSTEM_PROMPT = (
    "You are loom's agent — an agentic orchestrator running inside the user's "
    "own private Space container. Your workspace directory is your sandbox: "
    "create files, run shell commands, pack/unpack zips and repos there. "
    "Artifacts you write to the workspace are listed for the user to download. "
    "The disk is ephemeral — remind the user to download anything important. "
    "Be direct and concise; lead with outcomes."
)

#   free-tier model routing: first env var present wins. each entry is
#   (env key, litellm model string, base_url or None). overridable via
#   AGENT_OPEN_MODEL / AGENT_OPEN_BASE_URL / AGENT_OPEN_KEY_ENV.
_OPEN_LLMS: list[tuple[str, str, str | None]] = [
    ("GROQ_API_KEY",       "groq/llama-3.3-70b-versatile", None),
    ("OPENROUTER_API_KEY", "openrouter/qwen/qwen3-coder",  None),
    ("CEREBRAS_API_KEY",   "cerebras/qwen-3-coder-480b",   None),
    ("GEMINI_API_KEY",     "gemini/gemini-2.5-flash",      None),
    ("GOOGLE_API_KEY",     "gemini/gemini-2.5-flash",      None),
    ("NVIDIA_API_KEY",     "openai/moonshotai/kimi-k2-instruct",
     "https://integrate.api.nvidia.com/v1"),
]


def _installed(module: str) -> bool:
    """True if a module is importable, without importing it (cheap)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _pick_open_llm() -> tuple[str, str, str | None] | None:
    """(env_key, model, base_url) for the free tier, or None if no key is set."""
    key_env = os.environ.get("AGENT_OPEN_KEY_ENV", "").strip()
    if key_env and os.environ.get(key_env, "").strip():
        return (key_env,
                os.environ.get("AGENT_OPEN_MODEL", "groq/llama-3.3-70b-versatile"),
                os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or None)
    for env_key, model, base_url in _OPEN_LLMS:
        if os.environ.get(env_key, "").strip():
            model = os.environ.get("AGENT_OPEN_MODEL", "").strip() or model
            base_url = os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or base_url
            return (env_key, model, base_url)
    return None


def agent_tier() -> str | None:
    """Which agent tier this Space can actually run: "claude" | "open" | None.

    Requires BOTH a key and the matching SDK installed — so /health never
    advertises a tier the worker can't start.
    """
    forced = os.environ.get("AGENT_FORCE_TIER", "").strip().lower()
    if forced in ("claude", "open", "mock"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _installed("claude_agent_sdk"):
        return "claude"
    if _pick_open_llm() is not None and _installed("openhands.sdk"):
        return "open"
    return None


def _clip(text: object, limit: int = MAX_EVENT_CHARS) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"\n… [truncated {len(s) - limit} chars]"


class CapacityError(RuntimeError):
    """Raised when MAX_SESSIONS concurrent sessions already exist."""


# --------------------------------------------------------------------------
# adapters: one interface, three implementations
# --------------------------------------------------------------------------

class BaseAdapter:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def open(self) -> None: ...
    def turn(self, user_msg: str, emit) -> None:
        raise NotImplementedError
    def close(self) -> None: ...


class MockAdapter(BaseAdapter):
    """Plumbing test double: echoes and fakes one tool round-trip."""

    def turn(self, user_msg: str, emit) -> None:
        emit({"type": "tool_use", "name": "Echo", "summary": user_msg[:80]})
        emit({"type": "tool_result", "tool": "Echo", "text": f"echo: {user_msg}",
              "is_error": False})
        emit({"type": "assistant", "text": f"(mock) you said: {user_msg}"})


def _summarize_tool_input(name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return _clip(tool_input, 200)
    if name.lower() == "bash" and "command" in tool_input:
        return str(tool_input["command"])[:200]
    for key in ("file_path", "path", "pattern", "url", "query"):
        if key in tool_input:
            return str(tool_input[key])[:200]
    return _clip(tool_input, 200)


class ClaudeAdapter(BaseAdapter):
    """Official Claude Agent SDK; the bundled CLI subprocess does the work.

    The SDK is async; each session's worker thread owns a private event loop
    so the client survives across turns on the same thread.
    """

    def __init__(self, workspace: Path):
        super().__init__(workspace)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = None
        self._sdk = None

    def open(self) -> None:
        import claude_agent_sdk as sdk
        self._sdk = sdk
        options = sdk.ClaudeAgentOptions(
            cwd=str(self.workspace),
            permission_mode="bypassPermissions",   # headless; the Space is the sandbox
            setting_sources=["project"],           # load <workspace>/.claude/skills
            model=os.environ.get("AGENT_MODEL", "claude-opus-4-8"),
            system_prompt=AGENT_SYSTEM_PROMPT,
            max_turns=MAX_TURNS,
        )
        self.loop = asyncio.new_event_loop()
        self.client = sdk.ClaudeSDKClient(options=options)
        self.loop.run_until_complete(self.client.connect())

    def turn(self, user_msg: str, emit) -> None:
        assert self.loop is not None
        self.loop.run_until_complete(self._turn(user_msg, emit))

    async def _turn(self, user_msg: str, emit) -> None:
        sdk = self._sdk
        thinking_cls = getattr(sdk, "ThinkingBlock", None)
        await self.client.query(user_msg)
        async for msg in self.client.receive_response():
            if isinstance(msg, sdk.AssistantMessage):
                for block in msg.content:
                    if isinstance(block, sdk.TextBlock):
                        emit({"type": "assistant", "text": block.text})
                    elif thinking_cls is not None and isinstance(block, thinking_cls):
                        emit({"type": "thinking", "text": _clip(block.thinking)})
                    elif isinstance(block, sdk.ToolUseBlock):
                        emit({"type": "tool_use", "name": block.name,
                              "summary": _summarize_tool_input(block.name, block.input)})
            elif isinstance(msg, sdk.UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, sdk.ToolResultBlock):
                        emit({"type": "tool_result", "text": _clip(block.content),
                              "is_error": bool(getattr(block, "is_error", False))})
            elif isinstance(msg, sdk.ResultMessage):
                emit({"type": "status", "state": "idle",
                      "usage": getattr(msg, "usage", None),
                      "cost_usd": getattr(msg, "total_cost_usd", None)})

    def close(self) -> None:
        if self.loop is None:
            return
        try:
            if self.client is not None:
                self.loop.run_until_complete(self.client.disconnect())
        except Exception:
            pass
        finally:
            self.loop.close()


class OpenHandsAdapter(BaseAdapter):
    """OpenHands Agent SDK (free tier) — synchronous Conversation.run()."""

    def __init__(self, workspace: Path):
        super().__init__(workspace)
        self.convo = None
        self._emit = None

    def open(self) -> None:
        from openhands.sdk import LLM, Agent, Conversation, Tool

        picked = _pick_open_llm()
        if picked is None:
            raise RuntimeError("no free-tier provider key available")
        env_key, model, base_url = picked
        llm_kwargs: dict = {"model": model, "api_key": os.environ[env_key]}
        if base_url:
            llm_kwargs["base_url"] = base_url
        llm = LLM(**llm_kwargs)

        #   tool registration: import paths have churned across 1.x releases —
        #   prefer the names re-exported from openhands.tools, then fall back to
        #   submodule paths. Register whatever this install actually provides.
        import importlib
        tools = []
        seen: set[str] = set()
        try:
            import openhands.tools as ohtools
            for attr in ("TerminalTool", "BashTool", "FileEditorTool", "TaskTrackerTool"):
                cls = getattr(ohtools, attr, None)
                if cls is not None:
                    name = getattr(cls, "name", attr)
                    if name not in seen:
                        seen.add(name)
                        tools.append(Tool(name=name))
        except Exception:
            pass
        if not tools:
            for mod_name, cls_name in [
                ("openhands.tools.terminal", "TerminalTool"),
                ("openhands.tools.execute_bash", "BashTool"),
                ("openhands.tools.file_editor", "FileEditorTool"),
                ("openhands.tools.task_tracker", "TaskTrackerTool"),
            ]:
                try:
                    mod = importlib.import_module(mod_name)
                    cls = getattr(mod, cls_name)
                    name = getattr(cls, "name", cls_name)
                    if name not in seen:
                        seen.add(name)
                        tools.append(Tool(name=name))
                except Exception:
                    continue
        if not tools:
            raise RuntimeError("no OpenHands tools could be registered "
                               "(openhands-tools missing or incompatible)")

        agent = Agent(llm=llm, tools=tools)
        self.convo = Conversation(agent=agent, workspace=str(self.workspace),
                                  callbacks=[self._on_event])

    def _on_event(self, event) -> None:
        if self._emit is None:
            return
        emit = self._emit
        cls = type(event).__name__
        try:
            if cls == "MessageEvent":
                if getattr(event, "source", "") != "agent":
                    return  # the user echo; the session loop already emitted it
                msg = getattr(event, "llm_message", None)
                text = ""
                content = getattr(msg, "content", None)
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(getattr(c, "text", "") or str(c) for c in content)
                if text.strip():
                    emit({"type": "assistant", "text": _clip(text)})
            elif cls == "ActionEvent":
                action = getattr(event, "action", None)
                name = type(action).__name__ if action is not None else "Action"
                emit({"type": "tool_use", "name": name,
                      "summary": _clip(action, 200) if action is not None else ""})
            elif cls == "ObservationEvent":
                obs = getattr(event, "observation", None)
                emit({"type": "tool_result", "text": _clip(obs if obs is not None else event),
                      "is_error": False})
            elif cls == "AgentErrorEvent":
                emit({"type": "tool_result", "text": _clip(getattr(event, "error", event)),
                      "is_error": True})
        except Exception:
            pass  # transcript mapping must never kill the agent loop

    def turn(self, user_msg: str, emit) -> None:
        self._emit = emit
        try:
            self.convo.send_message(user_msg)
            self.convo.run()
        finally:
            self._emit = None

    def close(self) -> None:
        closer = getattr(self.convo, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def _make_adapter(tier: str, workspace: Path) -> BaseAdapter:
    if tier == "claude":
        return ClaudeAdapter(workspace)
    if tier == "open":
        return OpenHandsAdapter(workspace)
    return MockAdapter(workspace)


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

class AgentSession:
    def __init__(self, tier: str):
        self.id = uuid.uuid4().hex[:16]
        self.tier = tier
        self.created = time.time()
        self.updated = self.created
        self.status = "starting"
        self.closed = False
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.workspace = AGENT_ROOT / self.id
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._seed_skills()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"agent-{self.id}")
        self.thread.start()

    def _seed_skills(self) -> None:
        #   ship loom's skill folders into the workspace so the Claude tier
        #   picks them up via setting_sources=["project"].
        if SKILLS_SRC.is_dir():
            dest = self.workspace / ".claude" / "skills"
            try:
                shutil.copytree(SKILLS_SRC, dest, dirs_exist_ok=True)
            except Exception:
                pass

    # -- transcript ---------------------------------------------------------
    def emit(self, ev: dict) -> None:
        with self.lock:
            self.events.append({"i": len(self.events), "ts": time.time(), **ev})
            self.updated = time.time()

    def _set_status(self, state: str, **extra) -> None:
        self.status = state
        self.emit({"type": "status", "state": state, **extra})

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            events = self.events[max(0, since):]
            return {"session_id": self.id, "tier": self.tier, "status": self.status,
                    "events": events, "next": len(self.events)}

    # -- lifecycle ----------------------------------------------------------
    def submit(self, message: str) -> None:
        self.updated = time.time()
        self.inbox.put(message)

    def close(self) -> None:
        self.closed = True
        self.inbox.put(None)

    def _run(self) -> None:
        adapter = _make_adapter(self.tier, self.workspace)
        try:
            adapter.open()
        except Exception as exc:
            self._set_status("error", detail=f"agent init failed: {_clip(str(exc), 300)}")
            return
        self._set_status("idle")
        while not self.closed:
            try:
                msg = self.inbox.get(timeout=30)
            except queue.Empty:
                if time.time() - self.updated > SESSION_TTL_S:
                    break
                continue
            if msg is None:
                break
            self._set_status("running")
            self.emit({"type": "user", "text": msg})
            try:
                adapter.turn(msg, self.emit)
                self.status = "idle"
                self._set_status("idle")
            except Exception as exc:
                self._set_status("error", detail=_clip(str(exc), 300))
        adapter.close()


_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()


def _sweep_locked() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items()
               if now - s.updated > SESSION_TTL_S or s.closed]
    for sid in expired:
        s = _sessions.pop(sid)
        s.close()
        shutil.rmtree(s.workspace, ignore_errors=True)


def get_session(session_id: str) -> AgentSession | None:
    with _sessions_lock:
        _sweep_locked()
        return _sessions.get(session_id)


def get_or_create(session_id: str | None = None) -> AgentSession:
    """Reuse a live session by id, or start a new one (CapacityError if full)."""
    tier = agent_tier()
    if tier is None:
        raise RuntimeError("no agent tier available")
    with _sessions_lock:
        _sweep_locked()
        if session_id and session_id in _sessions:
            return _sessions[session_id]
        if len(_sessions) >= MAX_SESSIONS:
            raise CapacityError(f"max {MAX_SESSIONS} concurrent agent sessions")
        s = AgentSession(tier)
        _sessions[s.id] = s
        return s
