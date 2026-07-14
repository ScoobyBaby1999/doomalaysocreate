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
    "You have FULL capabilities — this is a cloud-hosted virtual PC:\n"
    "- shell: REAL bash execution (ls, cat, grep, find, git, python, pip, npm, make, curl, etc.)\n"
    "- file_read: read file contents\n"
    "- file_write: write/create files\n"
    "- editor: edit existing files (str_replace)\n"
    "- http_request: fetch URLs (GET/POST/PUT/DELETE — full web access)\n"
    "- grep: search file contents with regex\n"
    "- glob: find files by pattern (e.g. **/*.py)\n"
    "- calculator: math calculations\n"
    "- agent_panel: invoke the multi-model judge panel for critiques\n"
    "- load_tool: dynamically load more tools at runtime\n\n"
    "CRITICAL: ALWAYS use the `shell` tool for ANY command-line operation. "
    "The `shell` tool gives you a REAL bash shell with full output capture. "
    "NEVER use python_repl to run subprocess or os.system — use `shell` directly.\n\n"
    "Examples of using shell:\n"
    '- shell(command="ls -la") — list files\n'
    '- shell(command="git status") — check git state\n'
    '- shell(command="echo hello world") — print text\n'
    '- shell(command="python3 -c \'print(1+1)\'") — run Python\n'
    '- shell(command="grep -r \'pattern\' .") — search files\n'
    '- shell(command="git clone https://github.com/user/repo") — clone a repo\n\n'
    "Use file_read/file_write/editor for file operations. "
    "Use http_request for web fetches. "
    "Use grep/glob for code search. "
    "You can git clone repos, install packages, run build tools, and do "
    "anything a developer terminal can do. Lead with the outcome, not the process."
)

#   open-tier model routing: dynamically built from providers_catalog.json +
#   synced models from each provider's /v1/models endpoint.
#   Each entry is (env key, provider label, litellm model string, base_url or None).
#   Order = priority for auto-pick (first present env var wins). The model
#   picker surfaces EVERY entry whose key is set, not just the first.
#   Overridable via AGENT_OPEN_MODEL / AGENT_OPEN_BASE_URL / AGENT_OPEN_KEY_ENV.

_open_models_cache: list[tuple[str, str, str, str | None, dict | None]] | None = None


# Each entry: (env_var, label, litellm_model, base_url, extra_headers)


def _build_open_models() -> list[tuple[str, str, str, str | None, dict | None]]:
    """Build open model entries from providers_catalog.json dynamically.

    Each configured provider contributes ALL its models (env-var-gated), so
    every model from NVIDIA, Cloudflare, PrivateMode AI etc. is available in
    the agent without hardcoding model names.

    Returns 5-tuples: (env_var, label, litellm_model, base_url, extra_headers).
    extra_headers is a dict of HTTP headers the provider requires (e.g.
    OpenRouter needs HTTP-Referer + X-Title for free models).
    """
    global _open_models_cache
    # Re-probe if the cache is empty — the panel sync may not have completed
    # on the first call (cache poisoning fix). A non-empty cache is still
    # trusted (the catalog + synced models don't change at runtime).
    if _open_models_cache is not None and len(_open_models_cache) > 0:
        return _open_models_cache

    entries: list[tuple[str, str, str, str | None, dict | None]] = []
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

        # extra_headers (e.g. OpenRouter requires HTTP-Referer + X-Title)
        extra_headers = prov.get("extra_headers") or None

        for model_id in prov.get("models", []):
            litellm_model = f"openai/{model_id}"
            label = f"{model_id} ({name})"
            entries.append((env_var, label, litellm_model, base_url or None, extra_headers))

    # Also include dynamically synced models from providers with sync_config.
    from provider_sync import get_panel_sync_cache
    sync_cache = get_panel_sync_cache()
    if sync_cache:
        seen: set[str] = set(m.split("/")[-1] for _, _, m, _, _ in entries)
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
            sheaders = prov.get("extra_headers") or None
            for mid in synced:
                last = mid.split("/")[-1]
                if last not in seen:
                    seen.add(last)
                    entries.append((senv, f"{mid} ({name})",
                                    f"openai/{mid}", sbase or None, sheaders))

    _open_models_cache = entries
    return entries


def _resolve_open_model(user_model: str) -> tuple[str, str | None, str | None, str | None, dict | None] | None:
    """Match a user-provided model name to a litellm model string + base_url +
    env_var + provider name + extra_headers from the dynamic open-models list.

    Handles all input formats:
    - Canonical litellm: ``openai/kimi-k2.6`` (exact match)
    - Panel provider/model: ``privatemodeai/kimi-k2.6`` (match by last segment)
    - Logical/bare name: ``kimi-k2.6`` (match by last segment)
    - Partial: ``deepseek-v4-flash`` (match by last segment)

    Prefers an exact full match, then falls back to last-segment matching.
    When the same model name exists on multiple providers, the first one in
    catalog order wins (same as _build_open_models dedup). To route to a
    specific provider, pass ``provider/model`` — the provider prefix is
    matched against the env_var's provider name.

    Returns ``(litellm_model, base_url, env_var, provider_name, extra_headers)``
    or ``None``.
    """
    if not user_model:
        return None
    user_last = user_model.split("/")[-1]
    # Also extract a provider hint if the user passed "provider/model"
    user_provider = user_model.split("/")[0] if "/" in user_model else ""
    models = _build_open_models()
    # Pass 1: exact full match (canonical litellm string)
    for _env, _label, model, base, extra in models:
        if model == user_model:
            return (model, base, _env, _label, extra)
    # Pass 2: match by provider hint + last segment (e.g. "privatemodeai/kimi-k2.6")
    if user_provider:
        for env, label, model, base, extra in models:
            model_last = model.split("/")[-1]
            # label looks like "kimi-k2.6 (PrivateMode AI)" — extract provider
            label_provider = ""
            if "(" in label and ")" in label:
                label_provider = label[ label.index("(") + 1 : label.rindex(")") ].strip().lower()
            # env var maps to provider: PRIVATEMODEAI_API_KEY -> privatemodeai
            env_provider = env.lower().replace("_api_key", "").replace("_token", "").replace("_api_token", "")
            if (model_last == user_last and
                (user_provider.lower() in env_provider or
                 user_provider.lower() in label_provider.lower())):
                return (model, base, env, label, extra)
    # Pass 3: last-segment match (bare logical names, partial names)
    for env, label, model, base, extra in models:
        if model.split("/")[-1] == user_last:
            return (model, base, env, label, extra)
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
    for env_key, _label, model, base_url, _extra in _build_open_models():
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
        for env_key, label, model, _base, _extra in _build_open_models():
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
    for env_key, _label, m, _base, _extra in _build_open_models():
        if m == model or m.split("/")[-1] == model_last:
            return env_key
    return None

def _model_base_url(model: str) -> str | None:
    """base_url for a chosen open model (matches the dynamic model list)."""
    model_last = model.split("/")[-1]
    for _env, _label, m, base, _extra in _build_open_models():
        if m == model or m.split("/")[-1] == model_last:
            return base
    return None


def agent_tier() -> str | None:
    """Which agent tier this Space can actually run: "claude" | "open" | "mock".

    Requires BOTH a key and the matching SDK installed for a real tier — so
    /health never advertises a tier the worker can't start.  Falls back to
    "mock" when no real tier is available so duplicated Spaces (which start
    with zero provider keys) still let users dogfood the agent UI instead of
    hitting a 503.  Once a user adds a provider key via the onboarding wizard,
    HF restarts the Space and the real tier takes over automatically.
    Set AGENT_FORCE_TIER=none to disable the mock fallback (old behaviour).
    """
    forced = os.environ.get("AGENT_FORCE_TIER", "").strip().lower()
    if forced in ("claude", "open", "mock"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _installed("claude_agent_sdk"):
        return "claude"
    if _pick_open_llm() is not None and _open_sdk_installed():
        return "open"
    # Auto-fallback: mock tier so the agent UI is always usable on fresh
    # duplicated Spaces that have no provider keys yet.
    if forced != "none":
        return "mock"
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
    # Handle both "shell" and "bash" tool names
    if name.lower() in ("shell", "bash") and "command" in tool_input:
        return str(tool_input["command"])[:200]
    for key in ("file_path", "path", "pattern", "url", "query", "prompt"):
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


def _guarded_shell(tool_use=None, **kwargs):
    """Execute a bash command in the session workspace with FULL output capture.

    This is a REAL bash shell — the agent can run any command (git, npm, pip,
    python3, make, curl, etc.). Output (stdout + stderr) is captured and
    returned in the Strands tool result format.

    Strands calls this as: _guarded_shell(tool_use, **invocation_state)
    where tool_use is a TypedDict with "input", "name", "toolUseId".
    The command is in tool_use["input"]["command"].

    Network git operations (push/pull/fetch) are routed through the backend
    git functions so the GitHub token never lands in .git/config.
    """
    import subprocess

    # Extract toolUseId for the Strands ToolResult format (REQUIRED)
    tool_use_id = ""
    if tool_use and isinstance(tool_use, dict):
        tool_use_id = tool_use.get("toolUseId", "")

    # Extract the command from the tool_use input (Strands format) or
    # from kwargs (direct call format for backwards compat).
    cmd = ""
    if tool_use and isinstance(tool_use, dict):
        inp = tool_use.get("input", {})
        if isinstance(inp, dict):
            cmd = inp.get("command", "")
    if not cmd:
        cmd = kwargs.get("command", "")

    if not isinstance(cmd, str) or not cmd.strip():
        return {"status": "error", "toolUseId": tool_use_id,
                "content": [{"text": "command (non-empty string) is required"}]}

    workdir = str(getattr(_thread_local, "workspace", Path.cwd()))
    stripped = cmd.strip()

    # Intercept network git operations (push/pull/fetch) for security
    workspace_id = getattr(_thread_local, "workspace_id", None)
    if workspace_id and (
        stripped.startswith("git push")
        or stripped.startswith("git pull")
        or stripped.startswith("git fetch")
    ):
        is_force = stripped.startswith("git push") and (
            "-f " in stripped or "--force" in stripped)
        if not is_force:
            result = _route_network_git(stripped, workspace_id)
            result["toolUseId"] = tool_use_id
            return result

    # Execute the command directly via subprocess for reliable output capture
    try:
        result = subprocess.run(
            stripped,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max per command
        )
        output = result.stdout
        if result.stderr:
            output = (output + "\n" if output else "") + result.stderr
        if not output.strip():
            output = "(no output)"
        status = "success" if result.returncode == 0 else "error"
        if result.returncode != 0:
            output = f"Exit code: {result.returncode}\n{output}"
        return {
            "toolUseId": tool_use_id,
            "status": status,
            "content": [{"text": output[:50000]}],
        }
    except subprocess.TimeoutExpired:
        return {"toolUseId": tool_use_id, "status": "error",
                "content": [{"text": f"Command timed out after 300s: {stripped[:200]}"}]}
    except Exception as exc:
        return {"toolUseId": tool_use_id, "status": "error",
                "content": [{"text": f"Shell error: {exc}"}]}


class StrandsAdapter(BaseAdapter):
    """Open tier — Strands Agents SDK (AWS, Apache-2.0). Provider-agnostic via
    LiteLLM, so one adapter drives every OpenAI-compatible model (Kimi, GLM,
    MiniMax, DeepSeek, Groq, …). Native swarm/graph multi-agent + a deep built-in
    tool suite (shell, file edit, python, http). Runs synchronously in the worker
    thread; the structured message log is walked after each turn to build the
    transcript in order.
    """

    def __init__(self, workspace: Path, model: str | None = None,
                 workspace_id: str | None = None, system_prompt: str | None = None):
        super().__init__(workspace, workspace_id)
        self.model = model
        self.agent = None
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        # Resolved routing info (set by open(), surfaced in snapshot for verification)
        self.resolved_model: str | None = None
        self.resolved_provider: str | None = None
        self.resolved_api_base: str | None = None

    def open(self) -> None:
        import os as _os
        from strands import Agent
        from strands.models.litellm import LiteLLMModel

        # headless: skip interactive consent prompts (no TTY in the Space container).
        # must be set before importing strands_tools so their module-level checks see it.
        _os.environ["BYPASS_TOOL_CONSENT"] = "true"

        if self.model:
            # CRITICAL: resolve through _resolve_open_model so we get the
            # canonical litellm model string (openai/<model_id>) + the correct
            # api_base + env_var + extra_headers for this specific model.
            # Without this, a panel-format string like "privatemodeai/kimi-k2.6"
            # reaches LiteLLM directly and throws BadRequestError. Without
            # extra_headers, OpenRouter free models fail.
            pair = _resolve_open_model(self.model)
            if pair:
                model, base_url, key_env, provider_label, extra_headers = pair
            else:
                # Fallback: use the raw model string + best-effort key/base lookup.
                model = self.model
                key_env = _model_key_env(model) or _os.environ.get("AGENT_OPEN_KEY_ENV", "")
                base_url = _model_base_url(model)
                provider_label = None
                extra_headers = None
            if not key_env or not _os.environ.get(key_env, "").strip():
                raise RuntimeError(f"no API key for model {model}")
        else:
            picked = _pick_open_llm()
            if picked is None:
                raise RuntimeError("no open-tier provider key available")
            key_env, model, base_url = picked
            provider_label = None
            extra_headers = None

        # Record resolved routing for verification (snapshot + /api/agent response)
        self.resolved_model = model
        self.resolved_api_base = base_url
        self.resolved_provider = provider_label or key_env

        # client_args pass straight to litellm.completion (api_base = custom
        # OpenAI-compatible endpoint, e.g. Z.ai for GLM, NVIDIA for Kimi).
        # extra_headers is REQUIRED for OpenRouter (HTTP-Referer + X-Title).
        client_args: dict = {"api_key": _os.environ[key_env]}
        if base_url:
            client_args["api_base"] = base_url
        if extra_headers:
            client_args["extra_headers"] = extra_headers
        # stream=False: use non-streaming mode. Some providers (OpenRouter free,
        # NVIDIA) don't return toolUseId in streaming tool_use deltas → KeyError
        # in Strands' event loop. Non-streaming mode processes the full response
        # at once and _process_tool_calls generates UUIDs for missing IDs.
        llm = LiteLLMModel(client_args=client_args, model_id=model, stream=False)


        # the agent works in its session workspace; tools are imported defensively
        # so a renamed/missing tool never blocks startup.
        # Tool suite — FULL capabilities for a cloud-hosted virtual PC.
        # shell is FIRST (highest priority) and uses the @tool decorator which
        # handles ALL Strands format requirements (toolUseId, inputSchema, etc.)
        # automatically. The module-based approach had too many format issues.
        import types as _types
        tools = []

        # 1. shell (FIRST — primary tool for all command-line operations)
        # Use the @tool decorator which handles all Strands format requirements.
        try:
            from strands import tool as strands_tool_decorator

            @strands_tool_decorator(name="shell", description=(
                "Execute a bash command in the workspace sandbox. Returns "
                "stdout + stderr. Use this for ALL command-line operations: "
                "ls, cat, grep, find, git, python3, pip, npm, make, curl, etc. "
                "This is a REAL bash shell with full output capture."
            ))
            def shell(command: str) -> str:
                """Execute a bash command and return stdout + stderr."""
                import subprocess
                workdir = str(getattr(_thread_local, "workspace", Path.cwd()))
                stripped = command.strip()

                # SECURITY: strip all secrets from the subprocess environment
                # so the agent can't read API keys, tokens, or encryption keys
                # via `env`, `printenv`, or Python's os.environ.
                # Only pass through safe, non-secret environment variables.
                _SECRET_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD",
                                    "_KEY", "_ROTATION_SECRET", "_ENCRYPTION_KEY")
                safe_env = {}
                for k, v in os.environ.items():
                    if any(k.upper().endswith(s) for s in _SECRET_SUFFIXES):
                        continue  # strip secrets
                    if k.upper() in ("CRITIQUE_TOKEN", "CRITIQUE_ROTATION_SECRET",
                                     "ENCRYPTION_KEY", "APP_SECRET",
                                     "HF_CLIENT_SECRET", "GITHUB_CLIENT_SECRET",
                                     "HF_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
                        continue  # strip known sensitive vars
                    safe_env[k] = v
                # Keep PATH, HOME, LANG, etc. but strip secrets
                safe_env.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
                safe_env.setdefault("HOME", "/tmp")
                safe_env.setdefault("TERM", "dumb")

                # Intercept network git operations for security
                workspace_id = getattr(_thread_local, "workspace_id", None)
                if workspace_id and (
                    stripped.startswith("git push")
                    or stripped.startswith("git pull")
                    or stripped.startswith("git fetch")
                ):
                    is_force = stripped.startswith("git push") and (
                        "-f " in stripped or "--force" in stripped)
                    if not is_force:
                        result = _route_network_git(stripped, workspace_id)
                        parts = [c.get("text", "") for c in result.get("content", [])]
                        return "\n".join(parts) or "(no output)"

                try:
                    result = subprocess.run(
                        stripped, shell=True, cwd=workdir,
                        capture_output=True, text=True, timeout=300,
                        env=safe_env,  # SECURITY: stripped env (no secrets)
                    )
                    output = result.stdout
                    if result.stderr:
                        output = (output + "\n" if output else "") + result.stderr
                    if not output.strip():
                        output = "(no output)"
                    if result.returncode != 0:
                        output = f"Exit code: {result.returncode}\n{output}"
                    return output[:50000]
                except subprocess.TimeoutExpired:
                    return f"Command timed out after 300s: {stripped[:200]}"
                except Exception as exc:
                    return f"Shell error: {exc}"

            tools.append(shell)
        except Exception:
            # Fallback: module-based approach (less reliable but better than nothing)
            shell_mod = _types.ModuleType("shell")
            shell_mod.__file__ = __file__
            shell_mod.shell = _guarded_shell
            shell_mod.TOOL_SPEC = {
                "name": "shell",
                "description": "Execute a bash command in the workspace sandbox.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
            tools.append(shell_mod)

        # 2. File operations
        for mod_name in ("file_read", "file_write", "editor"):
            try:
                import importlib
                tools.append(importlib.import_module(f"strands_tools.{mod_name}"))
            except Exception:
                continue

        # 3. Web + search
        for mod_name in ("http_request", "calculator", "load_tool", "glob",
                         "web_search", "memorize", "journal", "slug",
                         "current_time", "env", "retrieve", "think"):
            try:
                import importlib
                tools.append(importlib.import_module(f"strands_tools.{mod_name}"))
            except Exception:
                continue
        # grep (may be named differently across versions)
        for grep_mod in ("grep", "search_files", "grep_code"):
            try:
                import importlib
                tools.append(importlib.import_module(f"strands_tools.{grep_mod}"))
                break
            except Exception:
                continue

        # Register the agent_panel tool using @tool decorator (same as shell).
        # The decorator handles all Strands format requirements automatically.
        try:
            import conscious_tools
            sess = self._session_ref()
            if sess is not None and 'strands_tool_decorator' in dir():
                @strands_tool_decorator(name="agent_panel", description=(
                    "Invoke the multi-model judge panel for a critique. "
                    "Fans the prompt out to a diverse panel of frontier LLMs "
                    "and returns merged results. Use for code review, plan "
                    "critique, or getting multiple expert opinions."
                ))
                def agent_panel(prompt: str, panel: list = None, effort: str = "med") -> str:
                    """Invoke the judge panel. Returns merged critique results."""
                    args = {"prompt": prompt}
                    if panel:
                        args["panel"] = panel
                    if effort:
                        args["effort"] = effort
                    result = conscious_tools._agent_panel(sess, args)
                    if isinstance(result, dict):
                        parts = [c.get("text", "") for c in result.get("content", [])]
                        return "\n".join(parts) or str(result)
                    return str(result)

                tools.append(agent_panel)
        except Exception:
            pass

        # Log which tools loaded so we can verify capabilities
        loaded = [getattr(t, "TOOL_SPEC", {}).get("name", "?") if hasattr(t, "TOOL_SPEC")
                  else getattr(t, "__name__", "?") for t in tools]
        try:
            log_event("agent_tools_loaded", tools=loaded)
        except Exception:
            pass

        # ─── Strands full-capability configuration ───────────────────────
        # 1. Conversation Management — SlidingWindowConversationManager
        #    Manages the context window automatically: when it gets too large,
        #    older messages are trimmed (preserving tool-use/tool-result pairs).
        #    This prevents context overflow errors and keeps the agent responsive
        #    in long conversations.
        conv_manager = None
        try:
            from strands.agent.conversation_manager import SlidingWindowConversationManager
            conv_manager = SlidingWindowConversationManager(window_size=40)
        except Exception:
            pass

        # 2. Session Management — FileSessionManager
        #    Persists the full conversation to disk so it survives Space restarts.
        #    Each agent session gets its own directory under the workspace.
        #    On restart, the agent can rehydrate from the saved messages.
        session_mgr = None
        try:
            from strands.session import FileSessionManager
            sess = self._session_ref()
            session_id = sess.chat_session_id or sess.id if sess else None
            sessions_dir = str(self.workspace / ".sessions")
            (self.workspace / ".sessions").mkdir(parents=True, exist_ok=True)
            session_mgr = FileSessionManager(
                session_id=session_id,
                sessions_dir=sessions_dir,
            )
        except Exception:
            pass

        # 3. Hooks — disabled for now (requires proper typed event callbacks
        #    that Strands can infer from type hints). The conversation manager
        #    and session manager are more critical. TODO: add hooks with
        #    @hook_provider decorator or explicit event_type annotations.
        agent_hooks = []

        # 4. Build the Agent with all features enabled
        agent_kwargs = dict(
            model=llm,
            tools=tools,
            system_prompt=self.system_prompt,
            callback_handler=None,
        )
        if conv_manager:
            agent_kwargs["conversation_manager"] = conv_manager
        if session_mgr:
            agent_kwargs["session_manager"] = session_mgr
        if agent_hooks:
            agent_kwargs["hooks"] = agent_hooks

        self.agent = Agent(**agent_kwargs)
        self._msg_cursor = 0
        # Register conscious tools for inspection (the panel tool is already
        # registered above as a Strands tool).
        try:
            import conscious_stubs
            conscious_stubs.register_conscious_tools(self._session_ref(), self)
        except Exception:
            pass

    def _session_ref(self):
        """Back-reference to the owning AgentSession (set by AgentSession._run)."""
        return getattr(self, "_session", None)

    def turn(self, user_msg: str, emit) -> None:
        _thread_local.workspace = self.workspace
        _thread_local.workspace_id = self.workspace_id
        sess = getattr(self, "_session", None)
        # Track whether thinking was emitted via the streaming callback.
        # If so, skip thinking emission in the post-turn walk (which would
        # duplicate the text). Reset at the start of each turn.
        self._thinking_streamed = False
        # Build a streaming callback handler: thinking text in real-time,
        # plus mid-turn cost ceiling enforcement for conscious agents.
        def _stream_callback(**kw):
            reasoning = kw.get("reasoningText")
            if reasoning:
                self._thinking_streamed = True
                emit({"type": "thinking", "text": reasoning})
            # Cost ceiling check (conscious agents only)
            if sess is not None and getattr(sess, "conscious_id", None):
                if not _cost_turn_ok(sess.conscious_id):
                    spent, ceiling = _cost_figures(sess.conscious_id)
                    emit({"type": "status", "state": "idle",
                          "detail": f"cost ceiling exceeded mid-turn (spent=${spent:.4f}, ceiling=${ceiling:.4f})"})
                    canceler = getattr(self.agent, "cancel", None)
                    if callable(canceler):
                        try: canceler()
                        except Exception: pass
                    try:
                        import conscious_db
                        conscious_db.append_event(
                            sess.conscious_id, "cost.exceeded",
                            f"mid-turn abort: spent=${spent:.4f} ceiling=${ceiling:.4f}",
                            author=getattr(sess, "agent_id", None))
                    except Exception: pass
        try:
            self.agent.callback_handler = _stream_callback
        except Exception:
            pass
        try:
            self.agent(user_msg)
        finally:
            pass
        # Walk newly-appended messages for tool results and final assistant text
        msgs = getattr(self.agent, "messages", []) or []
        for m in msgs[self._msg_cursor:]:
            role = m.get("role")
            for block in (m.get("content") or []):
                if "toolUse" in block:
                    tu = block["toolUse"] or {}
                    emit({"type": "tool_use", "name": tu.get("name", "tool"),
                          "summary": _summarize_tool_input(tu.get("name", ""),
                                                            tu.get("input", {}))})
                elif "toolResult" in block:
                    tr = block["toolResult"] or {}
                    parts = []
                    for c in (tr.get("content") or []):
                        if isinstance(c, dict) and "text" in c:
                            parts.append(c["text"])
                    emit({"type": "tool_result", "text": _clip("\n".join(parts) or tr),
                          "is_error": tr.get("status") == "error"})
                elif "reasoningContent" in block:
                    # Skip if the streaming callback already captured thinking.
                    # The streaming callback emits fragments in real-time which
                    # emit() accumulates into one event. Re-emitting the full
                    # text here would create a duplicate thinking bubble.
                    if getattr(self, "_thinking_streamed", False):
                        continue
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
    """Resolve which tier a chosen model belongs to (None → auto/default).

    Falls back to agent_tier() (which itself falls back to "mock") so an
    unknown model never hard-fails — the user gets a mock response and a
    clear path to add a real provider key.
    """
    if not model:
        return agent_tier()
    if model.startswith("claude"):
        return "claude" if (os.environ.get("ANTHROPIC_API_KEY", "").strip()
                            and _installed("claude_agent_sdk")) else agent_tier()
    if any(m["model"] == model for m in agent_models()):
        return "open"
    # Unknown model — fall back to whatever tier is available (mock if nothing
    # else) instead of returning None (which would raise RuntimeError).
    return agent_tier()


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
                # Always APPEND to the last thinking event. The Strands callback
                # sends reasoning fragments (one per token or chunk). Tool calls
                # (tool_use/tool_result) naturally separate reasoning blocks —
                # after a tool_result, self.events[-1] is tool_result (not
                # thinking), so a new thinking event is created automatically.
                # This means consecutive thinking events are ALWAYS continuations
                # of the same reasoning block, so appending is correct.
                old_text = (self.events[-1].get("text", "") or "")
                new_text = (ev.get("text", "") or "")
                # Skip stale duplicates (new text is a prefix of old)
                if old_text and new_text and old_text.startswith(new_text) and len(new_text) < len(old_text):
                    stream_ev = {"i": self.events[-1]["i"], "ts": self.events[-1]["ts"], **ev}
                    stream_ev["text"] = old_text
                else:
                    self.events[-1]["text"] = old_text + new_text
                    self.events[-1]["ts"] = time.time()
                    stream_ev = {"i": self.events[-1]["i"], "ts": self.events[-1]["ts"], **ev}
                    stream_ev["text"] = self.events[-1]["text"]
            else:
                new_ev = {"i": len(self.events), "ts": time.time(), **ev}
                self.events.append(new_ev)
                stream_ev = new_ev
            self.updated = time.time()
            queues = list(self._stream_queues)
            # Determine the event to persist (for non-thinking events, it's
            # stream_ev; for thinking merges, we persist the accumulated event)
            persist_ev = stream_ev
        for q in queues:
            try:
                q.put_nowait(stream_ev)
            except (queue.Full, ValueError):
                try:
                    self._stream_queues.remove(q)
                except ValueError:
                    pass
        # Persist new events to the DB so chat history survives restarts.
        # Only persist events with a new seq (not thinking merges which reuse
        # an existing seq). This is async (fire-and-forget) to avoid blocking
        # the agent turn.
        if self.chat_session_id and persist_ev.get("i", -1) > self.persisted_seq:
            ev_i = persist_ev["i"]
            self.persisted_seq = ev_i
            try:
                import chat_routes
                chat_routes.append_chat_events(self.chat_session_id, [persist_ev])
            except Exception:
                pass  # best-effort — don't block the turn on DB errors

    def register_stream_queue(self, q: queue.Queue) -> None:
        with self.lock:
            self._stream_queues.append(q)

    def unregister_stream_queue(self, q: queue.Queue) -> None:
        with self.lock:
            try:
                self._stream_queues.remove(q)
            except ValueError:
                pass

    def subscribe(self, since: int = 0):
        """Subscribe to live events for SSE streaming. Returns a queue.Queue
        that receives every new event as it's emitted. Events with i < since
        are replayed first (so a late subscriber catches up), then the queue
        stays open for live events. Call unsubscribe(q) when done.

        This is the method the SSE endpoint looks for via getattr(session,
        'subscribe', None). Without it, the SSE handler falls back to polling
        (200ms snapshots) — which is why streaming felt choppy/non-live."""
        q: queue.Queue = queue.Queue(maxsize=256)
        # Replay events the subscriber hasn't seen yet.
        with self.lock:
            replay = [e for e in self.events if (e.get("i", 0) >= since)]
        for ev in replay:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass  # drop backlog if subscriber is slow
        self.register_stream_queue(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Unsubscribe from live events. Pushes None to signal end-of-stream."""
        self.unregister_stream_queue(q)
        try:
            q.put_nowait(None)
        except queue.Full:
            pass

    def _set_status(self, state: str, **extra) -> None:
        self.status = state
        self.emit({"type": "status", "state": state, **extra})

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            events = self.events[max(0, since):]
            # Include resolved routing info so the frontend can verify which
            # model/provider actually served the request (not just what was
            # requested). self.adapter is set in _run() after open(); before
            # that, resolved_* are None.
            adapter = self.adapter
            return {"session_id": self.id, "tier": self.tier, "model": self.model,
                    "resolved_model": getattr(adapter, "resolved_model", None),
                    "resolved_provider": getattr(adapter, "resolved_provider", None),
                    "resolved_api_base": getattr(adapter, "resolved_api_base", None),
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


def _normalize_model_for_compare(model: str | None) -> str | None:
    """Normalize a model string for comparison so that equivalent models from
    different formats compare equal. E.g. all of these -> "kimi-k2.6":
      "kimi-k2.6", "openai/kimi-k2.6", "privatemodeai/kimi-k2.6"
    Returns None if model is None/empty.
    """
    if not model:
        return None
    m = model.strip()
    if not m:
        return None
    # Strip the litellm "openai/" prefix and any "provider/" prefix
    parts = m.split("/")
    return parts[-1].lower()


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
        # Reuse existing agent session linked to this chat_session_id — BUT
        # only if the model hasn't changed. If the user switched models mid-
        # conversation, we must create a fresh agent session so the new model
        # actually takes effect (the old session's adapter is pinned to the
        # old model). The old session is closed to free its resources.
        if chat_session_id:
            existing = None
            for s in _sessions.values():
                if s.chat_session_id == chat_session_id:
                    existing = s
                    break
            if existing is not None:
                # Normalize both model strings for comparison so
                # "kimi-k2.6" and "openai/kimi-k2.6" are treated as the same.
                norm_existing = _normalize_model_for_compare(existing.model)
                norm_new = _normalize_model_for_compare(model)
                if norm_new is None or norm_existing == norm_new:
                    return existing
                # Model changed — close the old session and fall through to
                # create a new one with the new model.
                existing.close()
                _sessions.pop(existing.id, None)
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
