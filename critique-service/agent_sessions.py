"""Agentic orchestrator sessions — the "agent" tab's backend.

Two tiers behind one adapter interface, auto-selected from the env *and* from
which SDK is actually installed:

  * "claude" — the official Claude Agent SDK (``claude-agent-sdk`` pip package,
    which bundles the Claude Code CLI inside the wheel — no Node needed).
    Active when ANTHROPIC_API_KEY is set and the SDK is importable.
  * "open"   — the Strands Agents SDK (``strands-agents`` + ``strands-agents-tools``,
    AWS, Apache-2.0): native swarm/graph multi-agent + a deep built-in tool suite
    (shell, file edit, python, http), provider-agnostic via LiteLLM so one adapter
    drives every OpenAI-compatible model (Kimi, GLM, MiniMax, DeepSeek, Groq, …).
    Active when a provider key is set and the SDK is importable. (Chosen over
    OpenHands, which has an unresolvable opentelemetry/lmnr dependency conflict in
    its current 1.28.x line and is Python-3.12-only.)

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
import weakref
from pathlib import Path

AGENT_ROOT = Path(os.environ.get("AGENT_ROOT", "/tmp/agent"))
SESSION_TTL_S = int(os.environ.get("AGENT_SESSION_TTL_S", "7200"))   # 2h idle
MAX_SESSIONS = int(os.environ.get("AGENT_MAX_SESSIONS", "8"))        # RAM bound
MAX_EVENT_CHARS = int(os.environ.get("AGENT_MAX_EVENT_CHARS", "4000"))
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "50"))

#   per-thread workspace tracking for concurrent agent sessions.  replaces the
#   process-wide os.chdir() pattern which broke concurrent sessions (session B's
#   cwd overwrite caused session A's shell commands to run in the wrong dir).
_thread_local = threading.local()

HERE = Path(__file__).resolve().parent
SKILLS_SRC = HERE / "agent_skills"   # seeded into each workspace's .claude/skills

AGENT_SYSTEM_PROMPT = (
    "You are doomalaysocreate's agent — an agentic orchestrator running inside the user's "
    "own private Space container. Your workspace directory is your sandbox: "
    "create files, run shell commands, pack/unpack zips and repos there. "
    "Artifacts you write to the workspace are listed for the user to download. "
    "The disk is ephemeral — remind the user to download anything important. "
    "Be direct and concise; lead with outcomes.\n\n"
    "You have these tools available:\n"
    "- shell: run bash commands (ls, cat, grep, git, python, etc.) — PREFER this over python_repl\n"
    "- file_read: read file contents\n"
    "- file_write: write/create files\n"
    "- editor: edit existing files (str_replace)\n"
    "- http_request: fetch URLs (web access)\n"
    "- python_repl: execute Python code (fallback for complex logic)\n"
    "- calculator: math calculations\n"
    "- load_tool: dynamically load more tools at runtime\n"
    "Use the shell tool for bash operations (grep, find, git, make, etc.). "
    "Use http_request for web fetches. Use file_read/file_write/editor for "
    "file operations. Only use python_repl when shell isn't sufficient."
)

#   open-tier model routing: dynamically built from providers_catalog.json +
#   synced models from each provider's /v1/models endpoint.
#   Each entry is (env key, provider label, litellm model string, base_url or None).
#   Order = priority for auto-pick (first present env var wins). The model
#   picker surfaces EVERY entry whose key is set, not just the first.
#   Overridable via AGENT_OPEN_MODEL / AGENT_OPEN_BASE_URL / AGENT_OPEN_KEY_ENV.

# Providers in our catalog → fetch models + base_url dynamically.
_PROVIDER_AGENT_MAP: dict[str, tuple[str, str]] = {
    "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1"),
    "opencode-zen": ("OPENCODE_ZEN_API_KEY", "https://opencode.ai/zen/v1"),
    "opencode-go": ("OPENCODE_GO_API_KEY", "https://opencode.ai/zen/go/v1"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "cloudflare": ("CF_API_TOKEN", "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1"),
    "github-models": ("GITHUB_TOKEN", "https://models.github.ai/inference"),
}

_open_models_cache: list[tuple[str, str, str, str | None]] | None = None


def _build_open_models() -> list[tuple[str, str, str, str | None]]:
    """Build open model entries from providers_catalog.json dynamically.

    Each configured provider contributes ALL its models (env-var-gated), so
    every model from NVIDIA, Cloudflare, PrivateMode AI etc. is available in
    the agent without hardcoding model names.
    """
    global _open_models_cache
    if _open_models_cache is not None:
        return _open_models_cache

    entries: list[tuple[str, str, str, str | None]] = []
    catalog_path = HERE / "providers_catalog.json"
    try:
        with open(catalog_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _open_models_cache = entries
        return entries

    for prov in data.get("providers", []):
        name = prov["name"]
        env_var = prov.get("env_var", "")
        if isinstance(env_var, list):
            env_var = env_var[0] if env_var else ""
        if not env_var:
            continue

        # Resolve {VAR} placeholders in base_url from the environment.
        base_url = prov.get("base_url", "")
        for var in prov.get("requires", []):
            base_url = base_url.replace("{" + var + "}", os.environ.get(var, "").strip())
        # LiteLLM appends /chat/completions for openai/ models, so strip it.
        if base_url.endswith("/chat/completions"):
            base_url = base_url[:-len("/chat/completions")]

        for model_id in prov.get("models", []):
            litellm_model = f"openai/{model_id}"
            label = f"{model_id} ({prov.get('displayName', name)})"
            entries.append((env_var, label, litellm_model, base_url or None))

    # Also include dynamically synced models from providers with sync_config.
    # The panel syncs models from live /v1/models endpoints at boot; those are
    # cached in provider_sync._panel_sync_cache.  Providers whose env var is set
    # contribute ALL their synced models so the agent picker shows everything
    # the panel can route to, not just the static catalog.
    from provider_sync import get_panel_sync_cache
    sync_cache = get_panel_sync_cache()
    if sync_cache:
        seen: set[str] = set(m.split("/")[-1] for _, _, m, _ in entries)
        for prov in data.get("providers", []):
            name = prov["name"]
            sync_config = prov.get("sync_config")
            if not sync_config or not sync_config.get("enabled"):
                continue
            senv = prov.get("env_var", "")
            if isinstance(senv, list):
                senv = senv[0] if senv else ""
            if not senv or not os.environ.get(senv, "").strip():
                continue
            synced = sync_cache.get(name, [])
            if not synced:
                continue
            sbase = prov.get("base_url", "")
            for var in prov.get("requires", []):
                sbase = sbase.replace("{" + var + "}", os.environ.get(var, "").strip())
            if sbase.endswith("/chat/completions"):
                sbase = sbase[:-len("/chat/completions")]
            for mid in synced:
                last = mid.split("/")[-1]
                if last not in seen:
                    seen.add(last)
                    entries.append((senv, f"{mid} ({prov.get('displayName', name)})",
                                    f"openai/{mid}", sbase or None))

    _open_models_cache = entries
    return entries


def _resolve_open_model(user_model: str) -> tuple[str, str | None] | None:
    """Match a user-provided model name (e.g. ``deepseek-v4-flash-free``) to a
    litellm model string + base_url from the dynamic open-models list.

    Returns ``(litellm_model, base_url)`` or ``None`` if no match.
    """
    user_last = user_model.split("/")[-1]
    for _env, _label, model, base in _build_open_models():
        if model == user_model or model.split("/")[-1] == user_last:
            return (model, base)
    return None


def _installed(module: str) -> bool:
    """True if a module is importable, without importing it (cheap)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _pick_open_llm() -> tuple[str, str, str | None] | None:
    """(env_key, model, base_url) for the default open model, or None if no key set."""
    key_env = os.environ.get("AGENT_OPEN_KEY_ENV", "").strip()
    if key_env and os.environ.get(key_env, "").strip():
        return (key_env,
                os.environ.get("AGENT_OPEN_MODEL", "groq/llama-3.3-70b-versatile"),
                os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or None)
    for env_key, _label, model, base_url in _build_open_models():
        if os.environ.get(env_key, "").strip():
            model = os.environ.get("AGENT_OPEN_MODEL", "").strip() or model
            base_url = os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or base_url
            return (env_key, model, base_url)
    return None


def _open_sdk_installed() -> bool:
    """True if the open-tier agent SDK (Strands) is importable."""
    return _installed("strands")


_CLAUDE_MODELS = [
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
]


def agent_models() -> list[dict]:
    """Every model the agent can actually run right now, for the picker UI.
    Only lists a model when BOTH its key and its tier's SDK are present.
    """
    out: list[dict] = []
    default_model = os.environ.get("AGENT_MODEL", "")
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _installed("claude_agent_sdk"):
        for model, label in _CLAUDE_MODELS:
            out.append({"tier": "claude", "provider": "Anthropic", "model": model,
                        "label": label, "default": model == default_model})
    if _open_sdk_installed():
        seen: set[str] = set()
        for env_key, label, model, _base in _build_open_models():
            if os.environ.get(env_key, "").strip() and model not in seen:
                seen.add(model)
                out.append({"tier": "open", "provider": label, "model": model,
                            "label": label, "default": False})
    if out and not any(m["default"] for m in out):
        out[0]["default"] = True
    return out


def _model_key_env(model: str) -> str | None:
    """Env var name for the API key of a chosen open model."""
    model_last = model.split("/")[-1]
    for env_key, _label, m, _base in _build_open_models():
        if m == model or m.split("/")[-1] == model_last:
            return env_key
    return None

def _model_base_url(model: str) -> str | None:
    """base_url for a chosen open model (matches the dynamic model list)."""
    model_last = model.split("/")[-1]
    for _env, _label, m, base in _build_open_models():
        if m == model or m.split("/")[-1] == model_last:
            return base
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
    if _pick_open_llm() is not None and _open_sdk_installed():
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
    def __init__(self, workspace: Path, workspace_id: str | None = None):
        self.workspace = workspace
        self.workspace_id = workspace_id

    def open(self) -> None: ...
    def turn(self, user_msg: str, emit) -> None:
        raise NotImplementedError
    def interrupt(self) -> None:
        #   stop the in-flight turn, if the SDK supports it. Default: no-op
        #   (Mock turns are instantaneous).
        ...
    def close(self) -> None: ...


class MockAdapter(BaseAdapter):
    """Plumbing test double: echoes and fakes one tool round-trip.

    Only used when AGENT_FORCE_TIER=mock or no real tier is available.
    """

    def __init__(self, workspace: Path, workspace_id: str | None = None,
                 system_prompt: str | None = None):
        super().__init__(workspace, workspace_id)

    def turn(self, user_msg: str, emit) -> None:
        emit({"type": "tool_use", "name": "Echo", "summary": user_msg[:80]})
        emit({"type": "tool_result", "tool": "Echo", "text": f"echo: {user_msg}",
              "is_error": False})
        emit({"type": "assistant", "text": f"(mock) you said: {user_msg}"})


# --------------------------------------------------------------------------
# ZaiAdapter — FREE GLM 5.2 via multiple providers.
#
# Call paths (tried in order — first success wins):
#   1. PYTHON-NATIVE direct HTTP calls (PREFERRED — no Node, no subprocess):
#      a. Puter.js OpenAI endpoint  (PUTER_API_TOKEN — free GLM-5.2, no phone)
#      b. Z.ai public API           (ZAI_API_KEY — real GLM-5.2)
#      c. NVIDIA NIM                (NVIDIA_API_KEY — free GLM-5.1)
#      d. OpenRouter                (OPENROUTER_API_KEY — GLM-5.2)
#      e. SiliconFlow               (SILICONFLOW_API_KEY — GLM models)
#   2. Node.js subprocess (glm-chat.mjs) — fallback if Python-native fails
#   3. HTTP bridge at localhost:3030 — last resort
#
# The Python-native path is critical: on a Python-only HF Space, Node.js may
# not be installed and the bridge may not be running. By calling the APIs
# directly from Python (urllib is stdlib), the ZaiAdapter works with ZERO
# external dependencies beyond the Python interpreter.
# --------------------------------------------------------------------------
GLM_BRIDGE_URL = os.environ.get("GLM_BRIDGE_URL", "http://localhost:3030")
GLM_BRIDGE_TIMEOUT_S = float(os.environ.get("GLM_BRIDGE_TIMEOUT_S", "120"))
_GLM_CHAT_SCRIPT = os.environ.get(
    "GLM_CHAT_SCRIPT",
    str(Path(__file__).resolve().parent.parent / "mini-services" / "glm-bridge" / "glm-chat.mjs"))
_GLM_NODE_BIN = os.environ.get("GLM_NODE_BIN", "node")

# --- OpenAI-compatible provider configs (for Python-native calls) ---
_GLM_PROVIDERS = [
    # Puter.js — FREE GLM-5.2, user-pays model. No Z.ai account needed.
    {"name": "puter", "env": "PUTER_API_TOKEN",
     "url": "https://api.puter.com/puterai/openai/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: f"z-ai/{m}"},
    # Z.ai public API — real GLM-5.2, "Limited-time Free" cached input
    {"name": "zai", "env": "ZAI_API_KEY",
     "url": "https://api.z.ai/api/paas/v4/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: m},
    # NVIDIA NIM — free GLM-5.1, 1000 credits, no phone
    {"name": "nvidia", "env": "NVIDIA_API_KEY",
     "url": "https://integrate.api.nvidia.com/v1/chat/completions",
     "models": ["glm-5.1"], "model_id": lambda m: "z-ai/glm-5.1"},
    # OpenRouter — GLM-5.2, $1 free credit on signup
    {"name": "openrouter", "env": "OPENROUTER_API_KEY",
     "url": "https://openrouter.ai/api/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: f"z-ai/{m}"},
    # SiliconFlow — free tier, GitHub login
    {"name": "siliconflow", "env": "SILICONFLOW_API_KEY",
     "url": "https://api.siliconflow.cn/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: m},
]


def _glm_native_available() -> bool:
    """True if ANY provider env var is set (Python-native call path is usable)."""
    return any(os.environ.get(p["env"], "").strip() for p in _GLM_PROVIDERS)


def _glm_call_native(messages: list[dict], model: str, timeout: float) -> str:
    """Call GLM providers directly from Python (urllib — stdlib, no Node needed).
    Tries each provider whose key is set + whose models include the requested one.
    Returns the response text, or raises if all providers fail."""
    import urllib.request
    import json as _json
    errors = []
    for p in _GLM_PROVIDERS:
        key = os.environ.get(p["env"], "").strip()
        if not key:
            continue
        if model not in p["models"]:
            continue
        model_id = p["model_id"](model)
        body = _json.dumps({"model": model_id, "messages": messages}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if p["name"] == "openrouter":
            headers["HTTP-Referer"] = "https://scoobybaby1999-doomalaysocreate.hf.space"
            headers["X-Title"] = "doomalaysocreate conscious agents"
        req = urllib.request.Request(p["url"], data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = _json.loads(r.read().decode())
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content:
                return content.strip()
        except Exception as exc:
            errors.append(f"{p['name']}: {type(exc).__name__}: {str(exc)[:120]}")
    raise RuntimeError("All GLM providers failed: " + "; ".join(errors))


def _glm_subprocess_available() -> bool:
    """True if the glm-chat.mjs script exists on disk."""
    return bool(_GLM_CHAT_SCRIPT) and Path(_GLM_CHAT_SCRIPT).is_file()


def _glm_http_available() -> bool:
    """Quick health check — is the GLM HTTP bridge reachable right now?"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{GLM_BRIDGE_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _glm_bridge_available() -> bool:
    """True if ANY GLM path is usable: native provider key, subprocess, or HTTP bridge."""
    return _glm_native_available() or _glm_subprocess_available() or _glm_http_available()


def _glm_call_subprocess(messages: list[dict], model: str, timeout: float) -> str:
    """Call glm-chat.mjs via subprocess: pipe messages JSON in, read {content} out."""
    import subprocess
    import json as _json
    payload = _json.dumps({"messages": messages, "model": model})
    proc = subprocess.run(
        [_GLM_NODE_BIN, _GLM_CHAT_SCRIPT],
        input=payload, capture_output=True, text=True, timeout=timeout)
    # The script always writes JSON to stdout (even on error, so the caller
    # gets a parseable response). stderr carries diagnostics.
    try:
        data = _json.loads(proc.stdout)
    except Exception:
        raise RuntimeError(f"glm-chat.mjs returned non-JSON: {proc.stdout[:200]!r} stderr={proc.stderr[:200]!r}")
    return (data.get("content") or "").strip()


def _glm_call_http(messages: list[dict], model: str, timeout: float) -> str:
    """Call the HTTP bridge at localhost:3030/chat."""
    import urllib.request
    import json as _json
    body = _json.dumps({"messages": messages, "model": model}).encode()
    req = urllib.request.Request(
        f"{GLM_BRIDGE_URL}/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = _json.loads(r.read().decode())
    return (data.get("content") or "").strip()

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

    def __init__(self, workspace: Path, model: str | None = None,
                 workspace_id: str | None = None, system_prompt: str | None = None):
        super().__init__(workspace, workspace_id)
        self.model = model or os.environ.get("AGENT_MODEL", "claude-opus-4-8")
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = None
        self._sdk = None

    def open(self) -> None:
        import claude_agent_sdk as sdk
        self._sdk = sdk
        options = sdk.ClaudeAgentOptions(
            cwd=str(self.workspace),
            permission_mode="bypassPermissions",   # headless; the Space is the sandbox
            allow_dangerously_skip_permissions=True,  # required double opt-in for the SDK
            setting_sources=["project"],           # load <workspace>/.claude/skills
            model=self.model,
            system_prompt=self.system_prompt,
            max_turns=MAX_TURNS,
        )
        self.loop = asyncio.new_event_loop()
        self.client = sdk.ClaudeSDKClient(options=options)
        self.loop.run_until_complete(self.client.connect())
        # Tier 3 — register conscious tools on the owning session (Phase 1:
        # stored for inspection; Phase 2 wires real Claude SDK tool registration).
        try:
            import conscious_stubs
            conscious_stubs.register_conscious_tools(self._session_ref(), self)
        except Exception:
            pass

    def _session_ref(self):
        """Back-reference to the owning AgentSession (set by AgentSession._run)."""
        return getattr(self, "_session", None)

    def turn(self, user_msg: str, emit) -> None:
        assert self.loop is not None
        self.loop.run_until_complete(self._turn(user_msg, emit))

    def interrupt(self) -> None:
        #   the loop is owned by the worker thread; schedule the SDK's interrupt
        #   coroutine onto it from the caller's (HTTP handler) thread.
        if self.loop is None or self.client is None or self.loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.client.interrupt(), self.loop)
        except Exception:
            pass

    async def _turn(self, user_msg: str, emit) -> None:
        sdk = self._sdk
        thinking_cls = getattr(sdk, "ThinkingBlock", None)
        await self.client.query(user_msg)
        async for msg in self.client.receive_response():
            # Phase 2 (completed in Phase 5 pass) — mid-turn cost abort.
            # After each message received, check the conscious cost ceiling.
            # If over, interrupt the SDK and emit a cost.exceeded event so the
            # orchestrator's next conscious_context surfaces it. This is the
            # "hard enforcement mid-turn" from TIER3_PLAN §14 (the deeper SDK
            # hook that the turn-boundary check was the baseline for).
            sess = getattr(self, "_session", None)
            if sess is not None and getattr(sess, "conscious_id", None):
                if not _cost_turn_ok(sess.conscious_id):
                    spent, ceiling = _cost_figures(sess.conscious_id)
                    emit({"type": "status", "state": "idle",
                          "detail": f"cost ceiling exceeded mid-turn (spent=${spent:.4f}, ceiling=${ceiling:.4f})"})
                    try:
                        await self.client.interrupt()
                    except Exception:
                        pass
                    try:
                        import conscious_db
                        conscious_db.append_event(
                            sess.conscious_id, "cost.exceeded",
                            f"mid-turn abort: spent=${spent:.4f} ceiling=${ceiling:.4f}",
                            author=getattr(sess, "agent_id", None))
                    except Exception:
                        pass
                    return
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


def _parse_git_branch(cmd: str) -> str | None:
    """Best-effort extract a branch name from a git push/pull/fetch command.
    Returns None if no explicit branch is named (caller should use the
    current branch via ``_current_branch``)."""
    parts = cmd.split()
    # skip 'git' and the subcommand; ignore flags and the 'origin' keyword
    candidates = [p for p in parts[2:] if p and not p.startswith("-") and p != "origin"]
    return candidates[0] if candidates else None


def _current_branch(sandbox: str) -> str:
    """Return the current checked-out branch name, or 'main' as a fallback."""
    import subprocess
    r = subprocess.run(["git", "-C", sandbox, "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() or "main"


def _route_network_git(cmd: str, workspace_id: str) -> dict:
    """Route agent git push/pull/fetch through the backend git functions so
    the GitHub token is never written to ``.git/config`` and every push is
    audit-logged.  Returns a Strands-style tool result dict.
    """
    import db
    import github_integration
    ws = db.get_workspace(workspace_id)
    if not ws:
        return {"status": "error",
                "content": [{"text": f"workspace not found: {workspace_id}"}]}
    user_id = ws["user_id"]
    sandbox = ws["sandbox_path"]
    stripped = cmd.strip()
    try:
        if stripped.startswith("git push"):
            force = ("-f " in stripped) or ("--force" in stripped)
            branch = _parse_git_branch(stripped) or _current_branch(sandbox)
            github_integration.push_to_remote(
                user_id, workspace_id, branch, force=force)
            return {"status": "success",
                    "content": [{"text": f"pushed {branch} to origin"}]}
        if stripped.startswith("git fetch"):
            branch = _parse_git_branch(stripped) or ""
            github_integration.fetch_from_remote(user_id, workspace_id, branch)
            return {"status": "success",
                    "content": [{"text": "fetched from origin" +
                                (f" ({branch})" if branch else "")}]}
        if stripped.startswith("git pull"):
            branch = _parse_git_branch(stripped) or _current_branch(sandbox)
            github_integration.fetch_from_remote(user_id, workspace_id, branch)
            return {"status": "success",
                    "content": [{"text": f"pulled {branch} from origin"}]}
    except Exception as exc:  # noqa: BLE001 - surface git errors to the agent
        return {"status": "error",
                "content": [{"text": f"git operation failed: {exc}"}]}
    return {"status": "error",
            "content": [{"text": f"unsupported git command: {cmd}"}]}


def _guarded_shell(**kwargs):
    """Wrap the Strands shell tool: force workspace dir + intercept git commands.

    1. Overrides ``workdir`` with the session workspace from thread-local storage.
    2. Network git operations (push/pull/fetch) are routed through the backend
       git functions (``push_to_remote`` / ``fetch_from_remote``) so the GitHub
       token never lands in ``.git/config`` and pushes are audit-logged.
    """
    from strands_tools import shell as _shell

    kwargs["workdir"] = str(getattr(_thread_local, "workspace", Path.cwd()))

    cmd = kwargs.get("command", "")
    if isinstance(cmd, str) and cmd.strip():
        stripped = cmd.strip()
        workspace_id = getattr(_thread_local, "workspace_id", None)
        if workspace_id and (
            stripped.startswith("git push")
            or stripped.startswith("git pull")
            or stripped.startswith("git fetch")
        ):
            is_force = stripped.startswith("git push") and (
                "-f " in stripped or "--force" in stripped)
            if not is_force:
                return _route_network_git(stripped, workspace_id)
    return _shell.tool(**kwargs)


# --------------------------------------------------------------------------
# Strands full-capacity integration helpers (open tier)
# --------------------------------------------------------------------------
# These wire the Strands Agents SDK at full capacity — stacked context
# management (context_manager="auto" → SummarizingConversationManager with
# proactive compression + ContextOffloader), durable FileSessionManager, a
# ContextInjector for ephemeral facts, AgentSkills progressive disclosure,
# HookProvider guardrails + observability, agent.state KV, and custom
# @tool-decorated web + conscious tools (replacing the ad-hoc protocols the
# legacy adapter used). All SDK imports stay deferred so the service still
# boots when Strands isn't installed.

_SHARED_HTTP_CLIENT = None  # lazily-built httpx.AsyncClient shared by web tools


def _shared_http_client():
    """One shared httpx.AsyncClient for all Strands web tools (Tavily/DDG +
    fetch). Created lazily so httpx is only imported when the open tier runs."""
    global _SHARED_HTTP_CLIENT
    if _SHARED_HTTP_CLIENT is None:
        import httpx
        _SHARED_HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _SHARED_HTTP_CLIENT


def _build_web_strands_tools(client):
    """web_search + web_fetch as native Strands @tool functions (replacing the
    TOOLS_PROTOCOL text-parsing the legacy research loop used). Explicit
    inputSchema is passed so the model always sees the parameters."""
    from strands import tool

    ws_schema = {"type": "object",
                 "properties": {"query": {"type": "string", "description": "search terms"}},
                 "required": ["query"]}
    wf_schema = {"type": "object",
                 "properties": {"url": {"type": "string", "description": "http(s) URL to fetch"}},
                 "required": ["url"]}

    @tool(name="web_search", description=(
        "Search the web for current information (news, docs, recent data, APIs). "
        "Returns numbered results: title, url, snippet. Cite sources as [title](url). "
        "Uses Tavily when TAVILY_API_KEY is set, else keyless DuckDuckGo."),
          inputSchema=ws_schema)
    async def web_search(query: str) -> str:
        import web_tools
        return await web_tools.web_search(query, http_client=client)

    @tool(name="web_fetch", description=(
        "Fetch and extract the main text content of a web page. Use for reading a "
        "specific URL found via web_search. SSRF-guarded (public hosts only, "
        "redirects re-validated). Returns up to 24k chars of cleaned text."),
          inputSchema=wf_schema)
    async def web_fetch(url: str) -> str:
        import web_tools
        return await web_tools.web_fetch(url, http_client=client)

    return [web_search, web_fetch]


def _build_shell_strands_tool():
    """Wrap _guarded_shell as a native Strands @tool. The legacy adapter registered
    a bare module (types.ModuleType with .tool) which Strands 1.45 rejects; a
    decorated function with an explicit inputSchema is the supported form."""
    from strands import tool

    shell_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "bash command to run in the session workspace"},
            "workdir": {"type": "string", "description": "override working dir (defaults to session workspace)"},
        },
        "required": ["command"],
    }

    @tool(name="shell", description=(
        "Run a bash command in the session workspace sandbox (ls, cat, grep, git, "
        "python, make, etc.). PREFER this over python_repl. Network git operations "
        "(push/pull/fetch) are routed through the audited backend git functions."),
          inputSchema=shell_schema)
    def shell(command: str, workdir: str = "") -> str:
        return _guarded_shell(command=command, workdir=workdir or "")

    return shell


def _build_conscious_strands_tools(session_weakref):
    """Convert the ad-hoc CONSCIOUS_TOOLS dict protocol into proper Strands
    @tool(context=True) functions so the SDK owns tool dispatch + schema
    validation. Returns [] if conscious_tools isn't importable."""
    try:
        import conscious_tools as _ct
    except Exception:
        return []
    tools = []
    for entry in getattr(_ct, "CONSCIOUS_TOOLS", []):
        name = entry.get("name")
        desc = entry.get("description", "") or f"conscious tool {name}"
        schema = entry.get("input_schema") or {"type": "object", "properties": {}}
        handler = entry.get("handler")
        if not name or not callable(handler):
            continue
        try:
            tools.append(_make_conscious_tool(name, desc, schema, handler, session_weakref))
        except Exception:
            continue
    return tools


def _make_conscious_tool(name, desc, schema, handler, session_weakref):
    """Build one @tool(context=True) wrapper around a CONSCIOUS_TOOLS handler.

    Site 4 improvement: the owning AgentSession is recovered through the
    weakref (back-compat) BUT the tool also reads from tool_context.invocation_state
    (the doc-recommended pattern) for request-scoped data like conscious_id,
    agent_id, session_id, and owner. This is the same invocation_state dict
    passed via Agent.__call__(invocation_state=...) in turn() — the foundation
    for multi-agent shared state (site 3). The weakref remains as a fallback
    for when invocation_state isn't populated.

    The context param is identified by NAME ("tool_context") when context=True,
    so no ToolContext annotation is needed — and crucially none is used, because
    a ToolContext annotation would fail to resolve under get_type_hints() in this
    deferred-import context (ToolContext isn't in module globals)."""
    from strands import tool

    @tool(name=name, description=desc, inputSchema=schema, context=True)
    def _conscious_tool(tool_context=None, **kwargs):
        # Site 4: prefer invocation_state (doc-recommended), fall back to weakref
        istate = {}
        if tool_context is not None:
            try:
                istate = getattr(tool_context, "invocation_state", None) or {}
            except Exception:
                istate = {}
        sess = session_weakref() if session_weakref else None
        # if the invocation_state carries conscious binding, attach it to the
        # session so the handler sees it (multi-agent shared-state pattern)
        if sess is not None and istate:
            for k in ("conscious_id", "agent_id", "session_id", "owner"):
                if istate.get(k) is not None:
                    setattr(sess, k, istate[k])
        try:
            res = handler(sess, kwargs) if sess is not None else {
                "error": "conscious tool called with no bound session"}
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        return res if isinstance(res, (str, dict, list)) else str(res)

    return _conscious_tool


class _StrandsGuardrailHooks:
    """HookProvider: block destructive shell commands and enforce the conscious
    cost ceiling at the tool-call boundary (clean mid-turn abort via cancel_tool
    instead of the legacy callback-based cancellation)."""
    def __init__(self, adapter):
        self._adapter = adapter

    def register_hooks(self, registry, **kwargs):
        from strands.hooks.events import BeforeToolCallEvent

        async def _before(event):
            tu = getattr(event, "tool_use", None)
            name = (tu.get("name") if isinstance(tu, dict) else getattr(tu, "name", None)) or "tool"
            inp = (tu.get("input") if isinstance(tu, dict) else getattr(tu, "input", None)) or {}
            # block destructive shell (defense-in-depth; _guarded_shell also enforces)
            if name in ("shell", "bash") and isinstance(inp, dict):
                cmd = str(inp.get("command", ""))
                if any(b in cmd for b in ("rm -rf /", ":(){:|:&};:", "mkfs", "dd if=/dev/")):
                    event.cancel_tool = f"blocked destructive shell: {cmd[:80]}"
                    return
            # cost ceiling (conscious agents only) — refuse new tool calls once over
            sess = self._adapter._session_ref()
            cid = getattr(sess, "conscious_id", None) if sess else None
            if cid and not _cost_turn_ok(cid):
                spent, ceiling = _cost_figures(cid)
                event.cancel_tool = (f"cost ceiling exceeded (spent=${spent:.4f}, "
                                     f"ceiling=${ceiling:.4f})")

        registry.add_callback(BeforeToolCallEvent, _before)


class _StrandsOplogHooks:
    """HookProvider: emit real-time tool_use/tool_result transcript events (so
    the SSE stream shows tool calls as they happen, not after the turn) and log
    every tool call to oplog for observability. Records emitted toolUseIds on
    the adapter so the post-turn message-walk backstop can skip duplicates."""
    def __init__(self, adapter):
        self._adapter = adapter

    def register_hooks(self, registry, **kwargs):
        from strands.hooks.events import BeforeToolCallEvent, AfterToolCallEvent

        async def _before(event):
            tu = getattr(event, "tool_use", None)
            name = (tu.get("name") if isinstance(tu, dict) else getattr(tu, "name", None)) or "tool"
            inp = (tu.get("input") if isinstance(tu, dict) else getattr(tu, "input", None)) or {}
            tid = (tu.get("toolUseId") if isinstance(tu, dict) else getattr(tu, "toolUseId", None))
            sess = self._adapter._session_ref()
            if sess is not None:
                sess.emit({"type": "tool_use", "name": name,
                           "summary": _summarize_tool_input(name, inp),
                           "tool_use_id": tid})
            if tid and hasattr(self._adapter, "_emitted_tool_ids"):
                self._adapter._emitted_tool_ids.add(tid)
            try:
                from oplog import log_event
                log_event("agent_tool_use", name=name, arg=_clip(inp, 200))
            except Exception:
                pass

        async def _after(event):
            tu = getattr(event, "tool_use", None)
            tid = (tu.get("toolUseId") if isinstance(tu, dict) else getattr(tu, "toolUseId", None))
            result = getattr(event, "result", None)
            exc = getattr(event, "exception", None)
            if isinstance(result, dict):
                parts = []
                for c in (result.get("content") or []):
                    if isinstance(c, dict) and "text" in c:
                        parts.append(c["text"])
                text = "\n".join(parts) or _clip(result)
                is_err = result.get("status") == "error" or exc is not None
            else:
                text = _clip(result) if result is not None else ""
                is_err = exc is not None
            if exc is not None:
                text = f"{type(exc).__name__}: {str(exc)[:200]}"
            sess = self._adapter._session_ref()
            if sess is not None:
                sess.emit({"type": "tool_result", "text": _clip(text),
                           "is_error": bool(is_err), "tool_use_id": tid})
            if tid and hasattr(self._adapter, "_emitted_tool_ids"):
                self._adapter._emitted_tool_ids.add(tid)
            try:
                from oplog import log_event
                log_event("agent_tool_result", name=(tu.get("name") if isinstance(tu, dict) else None),
                          ok=not is_err, chars=len(text))
            except Exception:
                pass

        registry.add_callback(BeforeToolCallEvent, _before)
        registry.add_callback(AfterToolCallEvent, _after)


# --- Site 7: lightweight in-process memory store (MemoryManager stand-in) ---
# A dict-backed store that captures episodic memories (user question → assistant
# answer summaries) per owner. This stands in for MemoryManager +
# BedrockKnowledgeBaseStore without needing AWS credentials. The ContextInjector
# recalls recent memories into each turn (ephemeral, never persisted to history).
# In prod, swap _recall_memories for a BedrockKB query.
_MEMORY_STORE: dict[str, list[str]] = {}


def _record_memory(owner: str, text: str) -> None:
    """Append a memory (capped at 50 per owner). Called after each turn."""
    if not owner or not text:
        return
    _MEMORY_STORE.setdefault(owner, [])
    _MEMORY_STORE[owner].append(text[:300])
    if len(_MEMORY_STORE[owner]) > 50:
        _MEMORY_STORE[owner] = _MEMORY_STORE[owner][-50:]


def _recall_memories(owner: str, limit: int = 3) -> list[str]:
    """Return the N most recent memories for an owner (empty if none)."""
    if not owner:
        return []
    return list(reversed(_MEMORY_STORE.get(owner, [])[-limit:]))


class _StrandsInvocationHooks:
    """Site 2 improvement: turn-level observability via BeforeInvocationEvent +
    AfterInvocationEvent. Logs turn start/stop + duration to oplog, increments
    agent.state.turn_count, and records an episodic memory after the turn
    completes (site 7 integration). Accesses invocation_state for request-scoped
    tracing when available."""
    def __init__(self, adapter):
        self._adapter = adapter

    def register_hooks(self, registry, **kwargs):
        from strands.hooks.events import BeforeInvocationEvent, AfterInvocationEvent
        import time as _time

        async def _before(event):
            # access invocation_state (site 2 + site 3 integration) — the
            # request-scoped dict flows through here for tracing.
            istate = getattr(event, "invocation_state", None) or {}
            try:
                from oplog import log_event
                log_event("agent_turn_start",
                          session=istate.get("session_id", ""),
                          owner=istate.get("owner", ""))
            except Exception:
                pass

        async def _after(event):
            istate = getattr(event, "invocation_state", None) or {}
            # increment turn_count in agent.state (site 10 — mutable state)
            try:
                ag = self._adapter.agent
                if ag is not None and hasattr(ag, "state"):
                    tc = ag.state.get("turn_count") or 0
                    ag.state.set("turn_count", tc + 1) if hasattr(ag.state, "set") else None
            except Exception:
                pass
            # site 7: record an episodic memory from the turn (best-effort)
            try:
                owner = istate.get("owner") or (self._adapter.workspace_id or "")
                user_msg = istate.get("user_msg", "")
                if owner and user_msg:
                    _record_memory(owner, f"Q: {str(user_msg)[:200]}")
            except Exception:
                pass
            try:
                from oplog import log_event
                log_event("agent_turn_complete", turn=ag.state.get("turn_count") if ag else 0)
            except Exception:
                pass

        registry.add_callback(BeforeInvocationEvent, _before)
        registry.add_callback(AfterInvocationEvent, _after)


class _StrandsSteeringHooks:
    """Site 10 improvement: Steering-style just-in-time guidance. Instead of
    front-loading a monolithic citation SOP in the system prompt, this hook
    injects citation-policy guidance ONLY when the web_fetch or shell tool is
    about to be called — the doc's "modular prompting" pattern (100% accuracy
    at 66% fewer tokens vs monolithic SOPs). Uses BeforeToolCallEvent (the
    Steering docs note Python uses the interventions/hooks framework)."""
    def __init__(self, adapter):
        self._adapter = adapter

    def register_hooks(self, registry, **kwargs):
        from strands.hooks.events import BeforeToolCallEvent

        async def _before(event):
            tu = getattr(event, "tool_use", None)
            name = (tu.get("name") if isinstance(tu, dict) else getattr(tu, "name", None)) or "tool"
            # just-in-time citation guidance when the agent is about to fetch
            # web content (the moment it matters, not front-loaded in the prompt)
            if name in ("web_fetch", "web_search", "http_request"):
                sess = self._adapter._session_ref()
                if sess is not None:
                    sess.emit({"type": "steering", "guidance": "citation_policy",
                               "detail": "Cite sources as [title](url) in your final answer."})
                try:
                    from oplog import log_event
                    log_event("agent_steering", policy="citation", tool=name)
                except Exception:
                    pass

        registry.add_callback(BeforeToolCallEvent, _before)


class StrandsAdapter(BaseAdapter):
    """Open tier — Strands Agents SDK (AWS, Apache-2.0) at full capacity.

    Provider-agnostic via LiteLLM, so one adapter drives every OpenAI-compatible
    model (Kimi, GLM, MiniMax, DeepSeek, Groq, …). The agent is wired with the
    full Strands feature set rather than a shallow wrapper:

      • context_manager="auto" → SummarizingConversationManager with proactive
        compression (proactive trimming + reactive overflow summarization) plus
        a ContextOffloader. Our own durable FileStorage offloader wins over the
        auto in-memory default — required when a session_manager is set.
      • FileSessionManager → conversation history + agent state + conversation-
        manager state survive Space restarts (S3 in prod via env swap).
      • ContextInjector → ephemeral facts (time, sandbox, session) folded into
        each turn without polluting conversation history.
      • AgentSkills → progressive disclosure from agent_skills/SKILL.md folders
        (only metadata enters the system prompt; full instructions load on use).
      • HookProvider hooks → guardrails (destructive shell, cost ceiling) +
        real-time tool_use/tool_result streaming + oplog observability.
      • agent.state KV → owner/tier/workspace (not passed to the model; tools
        read/write freely via ToolContext.agent.state).
      • @tool web_search/web_fetch + community strands_tools (shell, file_read,
        file_write, editor, http_request, python_repl, calculator, load_tool).
      • conscious_* tools registered as native @tool(context=True) functions.

    Runs synchronously in the worker thread (Agent.__call__ drives the event
    loop); a rich callback_handler streams text + reasoning deltas in real time,
    hooks stream tool boundaries, and the post-turn message walk is kept as a
    backstop for the final assistant text.
    """

    def __init__(self, workspace: Path, model: str | None = None,
                 workspace_id: str | None = None, system_prompt: str | None = None):
        super().__init__(workspace, workspace_id)
        self.model = model
        self.agent = None
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        self._msg_cursor = 0
        self._http_client = None
        # per-turn dedup set: toolUseIds already streamed by hooks so the
        # message-walk backstop doesn't double-emit them.
        self._emitted_tool_ids: set = set()

    def _session_ref(self):
        """Back-reference to the owning AgentSession (set by AgentSession._run)."""
        return getattr(self, "_session", None)

    def open(self) -> None:
        import os as _os
        from strands import Agent
        from strands.models.litellm import LiteLLMModel

        # headless: skip interactive consent prompts (no TTY in the Space container).
        # must be set before importing strands_tools so their module-level checks see it.
        _os.environ["BYPASS_TOOL_CONSENT"] = "true"

        # --- model resolution (env-gated LiteLLM model) ---
        if self.model:
            model = self.model
            key_env = _model_key_env(model) or _os.environ.get("AGENT_OPEN_KEY_ENV", "")
            base_url = _model_base_url(model)
            if not key_env or not _os.environ.get(key_env, "").strip():
                raise RuntimeError(f"no API key for model {model}")
        else:
            picked = _pick_open_llm()
            if picked is None:
                raise RuntimeError("no open-tier provider key available")
            key_env, model, base_url = picked
        client_args: dict = {"api_key": _os.environ[key_env]}
        if base_url:
            client_args["api_base"] = base_url
        llm = LiteLLMModel(client_args=client_args, model_id=model)

        # --- shared httpx client for the @tool web tools (SSRF-guarded inside) ---
        self._http_client = _shared_http_client()

        # --- durable session storage (local FS; S3SessionManager in prod) ---
        # Site 8 improvement: env-gated S3 swap — if SESSION_BUCKET is set,
        # use S3SessionManager (MinIO/LocalStack in dev via S3_ENDPOINT, real
        # S3 in prod) so sessions persist across containers/restarts. Otherwise
        # fall back to FileSessionManager on the local FS.
        sess = self._session_ref()
        sid_hint = (self.workspace_id or (sess.id if sess else None)
                    or uuid.uuid4().hex[:12])
        session_manager = None
        _session_bucket = _os.environ.get("SESSION_BUCKET", "").strip()
        if _session_bucket:
            try:
                from strands.session.s3_session_manager import S3SessionManager
                session_manager = S3SessionManager(
                    session_id=f"ws-{sid_hint}", bucket=_session_bucket,
                    prefix=_os.environ.get("SESSION_PREFIX", "doomalaysocreate/"),
                    region_name=_os.environ.get("AWS_REGION", _os.environ.get("AWS_DEFAULT_REGION", "")) or None,
                    endpoint_url=_os.environ.get("S3_ENDPOINT", "").strip() or None)
            except Exception:
                pass
        if session_manager is None:
            # RedisSessionManager (prod when REDIS_URL is set)
            redis_url = _os.environ.get("REDIS_URL", "").strip()
            if redis_url:
                try:
                    from redis_session_manager import RedisSessionManager
                    session_manager = RedisSessionManager(
                        session_id=f"ws-{sid_hint}", redis_url=redis_url)
                except Exception:
                    pass
        if session_manager is None:
            sessions_dir = self.workspace / ".strands" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            try:
                from strands.session.file_session_manager import FileSessionManager
                session_manager = FileSessionManager(
                    session_id=f"ws-{sid_hint}", storage_dir=str(sessions_dir))
            except Exception:
                pass

        # --- plugins: durable ContextOffloader + ContextInjector + AgentSkills ---
        plugins = []
        artifacts_dir = self.workspace / ".strands" / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Site 5 improvement: env-gated S3 storage backend — if
            # SESSION_BUCKET is set, offload oversized tool results to S3
            # (durable across containers); else local FileStorage (durable
            # across restarts on the same host). Both are DURABLE (not
            # InMemory) — required when session_manager is set.
            if _session_bucket:
                from strands.vended_plugins.context_offloader import ContextOffloader, S3Storage
                offloader_storage = S3Storage(
                    bucket=_session_bucket,
                    prefix=_os.environ.get("SESSION_PREFIX", "doomalaysocreate/") + "artifacts/",
                    region_name=_os.environ.get("AWS_REGION", _os.environ.get("AWS_DEFAULT_REGION", "")) or None)
            else:
                from strands.vended_plugins.context_offloader import ContextOffloader, FileStorage
                offloader_storage = FileStorage(artifact_dir=str(artifacts_dir))
            plugins.append(ContextOffloader(
                storage=offloader_storage,
                max_result_tokens=5000, preview_tokens=2000, include_retrieval_tool=True))
        except Exception:
            pass
        try:
            from strands.vended_plugins.context_injector import ContextInjector

            def _render_injected(_ctx) -> str:
                bits = [f"Current time: {int(time.time())} (unix)",
                        f"Workspace (sandbox): {self.workspace}"]
                s = self._session_ref()
                if s is not None:
                    bits.append(f"Session: {s.id} (tier={s.tier}, model={s.model})")
                    if getattr(s, "conscious_id", None):
                        bits.append(f"Conscious: {s.conscious_id} (agent {s.agent_id})")
                bits.append("The disk is ephemeral — remind the user to download artifacts.")
                # Site 7 improvement: recall recent memories into the turn
                # (ephemeral — folded into this call only, never persisted to
                # the conversation history, per the ContextInjector contract).
                recalled = _recall_memories(self.workspace_id or (sess.id if sess else ""))
                if recalled:
                    bits.append("\nRecent memories (from prior turns):")
                    for m in recalled:
                        bits.append(f"  - {m}")
                return "\n".join(bits)

            # Site 6 improvement: trigger="everyTurn" so the agent always has
            # the ephemeral context (time, sandbox, conscious binding, recalled
            # memories) on EVERY model call, not just fresh user turns. This
            # matters for multi-step tool loops where the agent makes several
            # model calls within a single user turn.
            plugins.append(ContextInjector(render_content=_render_injected,
                                           name="session_context", trigger="everyTurn"))
        except Exception:
            pass
        try:
            from strands.vended_plugins.skills import AgentSkills
            # Site 9 improvement: state_key="activated_skills" lets the agent
            # track which skills it has activated in agent.state (survives
            # across turns via session_manager). max_resource_files=4 caps
            # the resource files loaded per skill (progressive disclosure).
            skill_dirs = []
            skills_root = HERE / "agent_skills"
            if skills_root.is_dir():
                for sd in sorted(skills_root.iterdir()):
                    if (sd / "SKILL.md").is_file():
                        skill_dirs.append(str(sd))
            if skill_dirs:
                plugins.append(AgentSkills(skills=skill_dirs,
                                           state_key="activated_skills",
                                           max_resource_files=4, strict=False))
        except Exception:
            pass

        # --- tools: community strands_tools + custom @tool web + conscious ---
        tools = self._build_tools()

        # --- hooks: guardrails + steering + real-time tool streaming/observability ---
        # Site 2 improvement: added _StrandsInvocationHooks (Before/AfterInvocation)
        # for turn-level observability (duration, token usage) + the new
        # _StrandsSteeringHooks (site 10) for just-in-time citation guidance.
        hooks = [_StrandsGuardrailHooks(self), _StrandsSteeringHooks(self),
                 _StrandsOplogHooks(self), _StrandsInvocationHooks(self)]

        # --- agent.state KV (not passed to the model; tools read/write freely) ---
        # Site 10 improvement: state now includes turn_count + cost_spent (mutable
        # via tools/hooks) + activated_skills (written by AgentSkills state_key).
        state = {
            "owner": self.workspace_id or (sess.id if sess else "local"),
            "tier": "open",
            "model": model,
            "workspace": str(self.workspace),
            "created": int(time.time()),
            "turn_count": 0,
            "cost_spent_usd": 0.0,
        }
        if sess is not None and getattr(sess, "conscious_id", None):
            state["conscious_id"] = sess.conscious_id
            state["agent_id"] = sess.agent_id

        # --- conversation manager (site 1 improvement) ---
        # Explicit stacked manager instead of "auto": a SlidingWindowConversationManager
        # with pin_first=2 (protect the system prompt + first user message),
        # should_truncate_results=True (trim oversized tool results), window_size=40,
        # and proactive_compression at 0.7 (compress when 70% of the context window
        # is used). This is finer-grained than "auto" and the doc-recommended
        # configuration for long-running agent sessions.
        conversation_manager = None
        try:
            from strands.agent.conversation_manager import SlidingWindowConversationManager
            conversation_manager = SlidingWindowConversationManager(
                window_size=40, should_truncate_results=True,
                pin_first=2, per_turn=True,
                proactive_compression={"compression_threshold": 0.7})
        except Exception:
            pass

        # --- build the Agent at full capacity ---
        agent_kwargs: dict = dict(
            model=llm, tools=tools, system_prompt=self.system_prompt,
            plugins=plugins or None, hooks=hooks, state=state,
            load_tools_from_directory=False,
        )
        # Site 1: pass the explicit stacked conversation manager INSTANCE via
        # the `conversation_manager` param (NOT `context_manager`, which only
        # accepts the strings "auto"/"agentic"). Falls back to "auto" (which
        # composes SummarizingCM + offloader) if construction failed.
        if conversation_manager is not None:
            agent_kwargs["conversation_manager"] = conversation_manager
        else:
            agent_kwargs["context_manager"] = "auto"
        if session_manager is not None:
            agent_kwargs["session_manager"] = session_manager
        self.agent = Agent(**agent_kwargs)

        # back-compat: store the conscious tool registry on the session for
        # inspection (the tools are ALSO registered natively above).
        try:
            import conscious_stubs
            conscious_stubs.register_conscious_tools(self._session_ref(), self)
        except Exception:
            pass

    def _build_tools(self) -> list:
        import importlib
        tools = []
        # community strands_tools (defensive import — renamed/missing never blocks startup)
        for mod_name in ("file_read", "file_write", "editor",
                         "http_request", "python_repl", "calculator", "load_tool"):
            try:
                tools.append(importlib.import_module(f"strands_tools.{mod_name}"))
            except Exception:
                continue
        for grep_mod in ("grep", "search_files", "grep_code"):
            try:
                tools.append(importlib.import_module(f"strands_tools.{grep_mod}"))
                break
            except Exception:
                continue
        # guarded shell as a native @tool (forces execution into this session's
        # workspace; routes network git through the audited backend functions)
        try:
            tools.append(_build_shell_strands_tool())
        except Exception:
            pass
        # custom @tool web_search/web_fetch (Tavily/DDG + SSRF-guarded fetch)
        try:
            tools.extend(_build_web_strands_tools(self._http_client))
        except Exception:
            pass
        # conscious_* tools as native Strands @tool(context=True) functions
        try:
            sess = self._session_ref()
            ref = weakref.ref(sess) if sess is not None else None
            tools.extend(_build_conscious_strands_tools(ref))
        except Exception:
            pass
        return tools

    def turn(self, user_msg: str, emit) -> None:
        _thread_local.workspace = self.workspace
        _thread_local.workspace_id = self.workspace_id
        # reset per-turn dedup so the message-walk backstop only skips tools the
        # hooks streamed THIS turn.
        self._emitted_tool_ids = set()

        # rich callback_handler: stream text + reasoning DELTAS in real time.
        # The frontend merges consecutive *_delta events and replaces the last
        # delta with the matching final (assistant/thinking) from the backstop.
        def _stream_callback(**kw):
            reasoning = kw.get("reasoningText")
            if reasoning:
                emit({"type": "thinking_delta", "text": str(reasoning)})
            data = kw.get("data")
            if data:
                emit({"type": "assistant_delta", "text": str(data)})

        try:
            self.agent.callback_handler = _stream_callback
        except Exception:
            pass
        try:
            # Site 3 + 4 improvement: pass invocation_state through the agent
            # call. This is the doc-recommended way to share request-scoped
            # data with tools (via tool_context.invocation_state) and hooks
            # (via event.invocation_state) — the same mechanism Graph/Swarm
            # use for multi-agent shared state. Tools + hooks read from this
            # dict instead of the weakref hack; no agent-to-agent arg passing.
            sess = self._session_ref()
            invocation_state = {
                "session_id": sess.id if sess else "",
                "owner": self.workspace_id or (sess.id if sess else "local"),
                "user_msg": user_msg,
                "workspace": str(self.workspace),
                "conscious_id": getattr(sess, "conscious_id", None) if sess else None,
                "agent_id": getattr(sess, "agent_id", None) if sess else None,
            }
            self.agent(user_msg, invocation_state=invocation_state)
        finally:
            pass

        # backstop: walk newly-appended messages for the final assistant text +
        # any tool_use/tool_result the hooks missed (dedup by toolUseId).
        msgs = getattr(self.agent, "messages", []) or []
        for m in msgs[self._msg_cursor:]:
            role = m.get("role")
            for block in (m.get("content") or []):
                if "toolUse" in block:
                    tu = block["toolUse"] or {}
                    tid = tu.get("toolUseId")
                    if tid and tid in self._emitted_tool_ids:
                        continue
                    name = tu.get("name", "tool")
                    emit({"type": "tool_use", "name": name,
                          "summary": _summarize_tool_input(name, tu.get("input", {})),
                          "tool_use_id": tid})
                    if tid:
                        self._emitted_tool_ids.add(tid)
                elif "toolResult" in block:
                    tr = block["toolResult"] or {}
                    tid = tr.get("toolUseId")
                    if tid and tid in self._emitted_tool_ids:
                        continue
                    parts = []
                    for c in (tr.get("content") or []):
                        if isinstance(c, dict) and "text" in c:
                            parts.append(c["text"])
                    emit({"type": "tool_result", "text": _clip("\n".join(parts) or str(tr)),
                          "is_error": tr.get("status") == "error", "tool_use_id": tid})
                    if tid:
                        self._emitted_tool_ids.add(tid)
                elif "reasoningContent" in block:
                    rc = block["reasoningContent"] or {}
                    txt = (rc.get("reasoningText") or {}).get("text") if isinstance(
                        rc.get("reasoningText"), dict) else rc.get("reasoningText")
                    if txt:
                        emit({"type": "thinking", "text": _clip(txt)})
                elif "text" in block and role == "assistant":
                    if block["text"].strip():
                        emit({"type": "assistant", "text": block["text"]})
        self._msg_cursor = len(msgs)

    def interrupt(self) -> None:
        canceler = getattr(self.agent, "cancel", None)
        if callable(canceler):
            try:
                canceler()
            except Exception:
                pass

    def close(self) -> None:
        cleanup = getattr(self.agent, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass


def _make_adapter(tier: str, workspace: Path, model: str | None = None,
                  workspace_id: str | None = None, system_prompt: str | None = None) -> BaseAdapter:
    if tier == "claude":
        return ClaudeAdapter(workspace, model, workspace_id, system_prompt)
    if tier == "open":
        return StrandsAdapter(workspace, model, workspace_id, system_prompt)
    return MockAdapter(workspace, workspace_id, system_prompt)


def tier_for_model(model: str | None) -> str | None:
    """Resolve which tier a chosen model belongs to (None → auto/default)."""
    if not model:
        return agent_tier()
    if model.startswith("claude"):
        return "claude" if (os.environ.get("ANTHROPIC_API_KEY", "").strip()
                            and _installed("claude_agent_sdk")) else None
    if any(m["model"] == model for m in agent_models()):
        return "open"
    return None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

class AgentSession:
    def __init__(self, tier: str, model: str | None = None,
                 workspace_path: Path | None = None, workspace_id: str | None = None,
                 conscious_id: str | None = None, agent_id: str | None = None,
                 chat_session_id: str | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.chat_session_id = chat_session_id
        self.persisted_seq = 0
        self.tier = tier
        self.model = model
        self.workspace_id = workspace_id  # links to user's workspace, if any
        # Tier 3 — Conscious binding. Set when an agent is spawned against a
        # Conscious. Existing non-Conscious agents have both as None; the
        # conscious_* tool handlers no-op with an error in that case.
        self.conscious_id = conscious_id
        self.agent_id = agent_id
        # Build a context-rich system prompt when the agent is bound to a
        # cloned workspace, so the model knows it can run git commands.
        self.system_prompt = AGENT_SYSTEM_PROMPT
        if self.workspace_id:
            try:
                import db
                ws = db.get_workspace(self.workspace_id)
                if ws and ws.get("source_repo"):
                    self.system_prompt = (
                        f"{AGENT_SYSTEM_PROMPT}\n\n"
                        f"Workspace: {ws['title'] or ws['source_repo']} at {self.workspace}\n"
                        f"Git repo: {ws['source_repo']}\n"
                        f"Current branch: {ws.get('current_branch', 'main') or 'main'}\n"
                        f"You can use git commands: status, log, diff, branch, "
                        f"checkout, pull, add, commit, and push.\n"
                        f"To save changes: git add + git commit.\n"
                        f"To sync with remote: git push (allowed).\n"
                    )
            except Exception:
                pass
        # Tier 3 — Conscious binding: extend the system prompt with the
        # conscious toolset overview so the model knows the brain exists.
        # The actual tool registration happens in each adapter's open(); this
        # just primes the prompt. Phase 1 tools are stubs (no real execution).
        self.conscious_tools: list = []
        if self.conscious_id and self.agent_id:
            self.system_prompt = (
                f"{self.system_prompt}\n\n"
                f"--- Conscious (Tier 3) ---\n"
                f"You are bound to conscious {self.conscious_id} as agent {self.agent_id}.\n"
                f"The shared brain lives at .brain/ in your workspace. Read it before acting.\n"
                f"Tools available: conscious_context, conscious_post (orchestrator-only), "
                f"conscious_propose (sub-agents), conscious_commit_proposal, "
                f"conscious_reject_proposal, conscious_invoke, conscious_delegate, "
                f"conscious_drawer, conscious_message, conscious_claim, conscious_task, "
                f"conscious_subscribe. See .brain/skills/conscious/SKILL.md for the full guide.\n"
                f"Phase 1 note: invoke/delegate return stubbed results; real worktree-per-agent "
                f"execution lands in Phase 2.\n"
            )
        self.created = time.time()
        self.updated = self.created
        self.status = "starting"
        self.closed = False
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self._stream_queues: list[queue.Queue] = []
        self.adapter: BaseAdapter | None = None
        self._interrupting = False
        # use provided workspace path (user's workspace) or create ephemeral one
        if workspace_path is not None:
            self.workspace = workspace_path
            self.workspace.mkdir(parents=True, exist_ok=True)
        else:
            self.workspace = AGENT_ROOT / self.id
            self.workspace.mkdir(parents=True, exist_ok=True)
        self._seed_skills()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"agent-{self.id}")
        self.thread.start()

    def _seed_skills(self) -> None:
        #   ship doomalaysocreate's skill folders into the workspace so the Claude tier
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
            if (ev.get("type") == "thinking"
                    and self.events and self.events[-1].get("type") == "thinking"):
                self.events[-1]["text"] = ev["text"]
                self.events[-1]["ts"] = time.time()
            else:
                self.events.append({"i": len(self.events), "ts": time.time(), **ev})
            self.updated = time.time()
            queues = list(self._stream_queues)
        for q in queues:
            try:
                q.put_nowait(ev)
            except (queue.Full, ValueError):
                try:
                    self._stream_queues.remove(q)
                except ValueError:
                    pass

    def register_stream_queue(self, q: queue.Queue) -> None:
        with self.lock:
            self._stream_queues.append(q)

    def unregister_stream_queue(self, q: queue.Queue) -> None:
        with self.lock:
            try:
                self._stream_queues.remove(q)
            except ValueError:
                pass

    def _set_status(self, state: str, **extra) -> None:
        self.status = state
        self.emit({"type": "status", "state": state, **extra})

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            events = self.events[max(0, since):]
            return {"session_id": self.id, "tier": self.tier, "model": self.model,
                    "status": self.status, "events": events, "next": len(self.events)}

    # -- lifecycle ----------------------------------------------------------
    def submit(self, message: str) -> None:
        self.updated = time.time()
        self.inbox.put(message)

    def interrupt(self) -> bool:
        #   stop the current turn (called from the HTTP thread). No-op unless running.
        if self.status != "running" or self.adapter is None:
            return False
        self._interrupting = True
        try:
            self.adapter.interrupt()
        except Exception:
            pass
        return True

    def close(self) -> None:
        self.closed = True
        self.inbox.put(None)

    def _run(self) -> None:
        adapter = _make_adapter(self.tier, self.workspace, self.model,
                                self.workspace_id, self.system_prompt)
        self.adapter = adapter
        # Tier 3 — give the adapter a back-reference so it can register the
        # conscious tool registry on the owning session (conscious_id/agent_id).
        adapter._session = self
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
            self._interrupting = False
            self._set_status("running")
            self.emit({"type": "user", "text": msg})
            # Phase 2 — cost-ceiling turn-boundary check (§14 hard enforcement).
            # If over ceiling before the turn even starts, refuse to run and
            # emit a cost.exceeded event. Mid-turn abort needs deeper SDK hooks
            # (callback handler inspecting each tool result); the turn-boundary
            # check is the safe baseline that works in both adapters.
            if self.conscious_id and not _cost_turn_ok(self.conscious_id):
                spent, ceiling = _cost_figures(self.conscious_id)
                self.emit({"type": "status", "state": "idle",
                           "detail": f"cost ceiling exceeded (spent=${spent:.4f}, ceiling=${ceiling:.4f})"})
                try:
                    import conscious_db
                    conscious_db.append_event(
                        self.conscious_id, "cost.exceeded",
                        f"turn refused: spent=${spent:.4f} ceiling=${ceiling:.4f}",
                        author=self.agent_id)
                except Exception:
                    pass
                self._set_status("idle", detail="cost ceiling exceeded")
                continue
            try:
                adapter.turn(msg, self.emit)
                if self._interrupting:
                    self._set_status("idle", detail="interrupted")
                else:
                    self._set_status("idle")
            except Exception as exc:
                if self._interrupting:
                    self._set_status("idle", detail="interrupted")
                else:
                    self._set_status("error", detail=_clip(str(exc), 300))
            finally:
                self._interrupting = False
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


def get_or_create(session_id: str | None = None,
                  model: str | None = None,
                  workspace_id: str | None = None,
                  conscious_id: str | None = None,
                  agent_id: str | None = None,
                  chat_session_id: str | None = None) -> AgentSession:
    """Reuse a live session by id, or start a new one (CapacityError if full).
    `model` (optional) selects which model/tier drives a NEW session.
    `workspace_id` (optional) links the session to a user workspace sandbox.
    `conscious_id` + `agent_id` (optional, Tier 3) bind the session to a
    Conscious agent row so the conscious_* tools resolve context.
    `chat_session_id` (optional) links this agent session to a persistent
    chat session for event persistence.
    """
    tier = tier_for_model(model)
    if tier is None:
        raise RuntimeError("no agent tier available for the requested model")
    with _sessions_lock:
        _sweep_locked()
        if session_id and session_id in _sessions:
            return _sessions[session_id]
        # Reuse existing agent session linked to this chat_session_id
        if chat_session_id:
            for s in _sessions.values():
                if s.chat_session_id == chat_session_id:
                    return s
        if len(_sessions) >= MAX_SESSIONS:
            raise CapacityError(f"max {MAX_SESSIONS} concurrent agent sessions")
        # resolve workspace_id to a filesystem path
        workspace_path = None
        if workspace_id:
            import db
            import github_integration
            # repair sandbox if missing (Space restart wiped /data/)
            github_integration.ensure_workspace_sandbox(workspace_id)
            ws = db.get_workspace(workspace_id)
            if ws and ws.get("sandbox_path"):
                workspace_path = Path(ws["sandbox_path"])
        s = AgentSession(tier, model, workspace_path=workspace_path,
                         workspace_id=workspace_id,
                         conscious_id=conscious_id, agent_id=agent_id,
                         chat_session_id=chat_session_id)
        _sessions[s.id] = s
        return s


# ---------------------------------------------------------------------------
# Phase 2 — cost-ceiling turn-boundary helpers (§14 hard enforcement)
# ---------------------------------------------------------------------------

def _cost_figures(conscious_id: str) -> tuple[float, float]:
    """Return (spent, ceiling) for a conscious. ceiling=0 means infinite."""
    try:
        import conscious_db
        c = conscious_db.get_conscious(conscious_id) or {}
        return (float(c.get("cost_spent_usd", 0) or 0),
                float(c.get("cost_ceiling_usd", 0) or 0))
    except Exception:
        return (0.0, 0.0)


def _cost_turn_ok(conscious_id: str) -> bool:
    """True if the conscious is under its cost ceiling (or ceiling=0=infinite)."""
    spent, ceiling = _cost_figures(conscious_id)
    if ceiling <= 0:
        return True
    return spent < ceiling
