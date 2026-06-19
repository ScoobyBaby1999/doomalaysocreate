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
    "You are loom's agent — an agentic orchestrator running inside the user's "
    "own private Space container. Your workspace directory is your sandbox: "
    "create files, run shell commands, pack/unpack zips and repos there. "
    "Artifacts you write to the workspace are listed for the user to download. "
    "The disk is ephemeral — remind the user to download anything important. "
    "Be direct and concise; lead with outcomes."
)

#   open-tier model routing: each entry is
#   (env key, provider label, litellm model string, base_url or None).
#   Order = priority for auto-pick (first present env var wins). The model
#   picker (Part 2) surfaces EVERY entry whose key is set, not just the first.
#   Overridable via AGENT_OPEN_MODEL / AGENT_OPEN_BASE_URL / AGENT_OPEN_KEY_ENV.
_OPEN_LLMS: list[tuple[str, str, str, str | None]] = [
    ("MOONSHOT_API_KEY",   "Kimi (Moonshot)",   "moonshot/kimi-k2-0905-preview", None),
    # Groq removed — best model is GPT-120B-OSS which underperforms; users
    # reported it as not useful. Re-add if Groq adds a competitive model.
    ("OPENROUTER_API_KEY", "OpenRouter Qwen3",  "openrouter/qwen/qwen3-coder",   None),
    ("CEREBRAS_API_KEY",   "Cerebras Qwen3",    "cerebras/qwen-3-coder-480b",    None),
    ("ZAI_API_KEY",        "GLM 5.2 (Z.ai)",     "openai/glm-5.2",
     "https://api.z.ai/api/paas/v4"),
    ("GEMINI_API_KEY",     "Gemini 2.5 Flash",  "gemini/gemini-2.5-flash",       None),
    ("GOOGLE_API_KEY",     "Gemini 2.5 Flash",  "gemini/gemini-2.5-flash",       None),
    ("NVIDIA_API_KEY", "Kimi K2.6 (NVIDIA)", "openai/moonshotai/kimi-k2.6",
      "https://integrate.api.nvidia.com/v1"),
]


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
    for env_key, _label, model, base_url in _OPEN_LLMS:
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

    The free GLM 5.2 (zai tier) is ALWAYS listed when the bridge is reachable
    — no API key required. It's the default when nothing else is configured so
    the agent panel never silently falls back to the MockAdapter echo.
    """
    out: list[dict] = []
    default_model = os.environ.get("AGENT_MODEL", "")
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _installed("claude_agent_sdk"):
        for model, label in _CLAUDE_MODELS:
            out.append({"tier": "claude", "provider": "Anthropic", "model": model,
                        "label": label, "default": model == default_model})
    if _open_sdk_installed():
        seen: set[str] = set()
        for env_key, label, model, _base in _OPEN_LLMS:
            if os.environ.get(env_key, "").strip() and model not in seen:
                seen.add(model)
                out.append({"tier": "open", "provider": label, "model": model,
                            "label": label, "default": False})
    # Free GLM 5.2 via the z-ai-web-dev-sdk bridge — no API key needed, so it's
    # always offered when the bridge is up. This is the tier the user picks
    # when they see "GLM 5.2 (free)" in the agent panel model dropdown.
    if _glm_bridge_available():
        out.append({"tier": "zai", "provider": "Z.ai (free)", "model": "glm-5.2-free",
                    "label": "GLM 5.2 (free)", "default": False})
    if out and not any(m["default"] for m in out):
        # Prefer free GLM as the default over mock when no AGENT_MODEL is set,
        # so a fresh Space with no API keys still gets real AI responses.
        zai = next((m for m in out if m["tier"] == "zai"), None)
        if zai:
            zai["default"] = True
        else:
            out[0]["default"] = True
    return out


def _model_base_url(model: str) -> str | None:
    """base_url for a chosen open model (matches the _OPEN_LLMS table)."""
    for _env, _label, m, base in _OPEN_LLMS:
        if m == model:
            return base
    return os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or None


def _model_key_env(model: str) -> str | None:
    """which env var holds the API key for a chosen open model."""
    for env, _label, m, _base in _OPEN_LLMS:
        if m == model:
            return env
    return None


def agent_tier() -> str | None:
    """Which agent tier this Space can actually run: "claude" | "open" | "zai" | None.

    Requires BOTH a key and the matching SDK installed — so /health never
    advertises a tier the worker can't start. The "zai" tier (free GLM via
    the bridge) needs no API key — only a reachable bridge on port 3030.
    """
    forced = os.environ.get("AGENT_FORCE_TIER", "").strip().lower()
    if forced in ("claude", "open", "mock", "zai"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _installed("claude_agent_sdk"):
        return "claude"
    if _pick_open_llm() is not None and _open_sdk_installed():
        return "open"
    # Free GLM via the bridge — the fallback that gives every Space real AI
    # even with zero API keys configured. Without this, a keyless Space would
    # resolve to None and the agent panel would 500 ("no agent tier available")
    # or silently use the MockAdapter echo.
    if _glm_bridge_available():
        return "zai"
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

    Only used when AGENT_FORCE_TIER=mock or no real tier is available. Real
    GLM (free) is provided by ZaiAdapter below — the agent panel never falls
    back to mock silently when the bridge is reachable.
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
#
# All calls are logged to debug/glm.log via debug_log.dlog() so you can see
# exactly which provider was tried, what happened, and how long it took.
# --------------------------------------------------------------------------
GLM_BRIDGE_URL = os.environ.get("GLM_BRIDGE_URL", "http://localhost:3030")
GLM_BRIDGE_TIMEOUT_S = float(os.environ.get("GLM_BRIDGE_TIMEOUT_S", "120"))
_GLM_CHAT_SCRIPT = os.environ.get(
    "GLM_CHAT_SCRIPT",
    str(Path(__file__).resolve().parent.parent / "mini-services" / "glm-bridge" / "glm-chat.mjs"))
_GLM_NODE_BIN = os.environ.get("GLM_NODE_BIN", "node")

# --- OpenAI-compatible provider configs (for Python-native calls) ---
# Each provider: env var name, endpoint URL, supported models, model ID formatter.
# Priority order: Puter (free 5.2) → Z.ai (real 5.2) → NVIDIA (free 5.1) →
# OpenRouter (paid 5.2) → SiliconFlow (free tier).
_GLM_PROVIDERS = [
    {"name": "puter", "env": "PUTER_API_TOKEN",
     "url": "https://api.puter.com/puterai/openai/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: f"z-ai/{m}"},
    {"name": "zai", "env": "ZAI_API_KEY",
     "url": "https://api.z.ai/api/paas/v4/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: m},
    {"name": "nvidia", "env": "NVIDIA_API_KEY",
     "url": "https://integrate.api.nvidia.com/v1/chat/completions",
     "models": ["glm-5.1"], "model_id": lambda m: "z-ai/glm-5.1"},
    {"name": "openrouter", "env": "OPENROUTER_API_KEY",
     "url": "https://openrouter.ai/api/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: f"z-ai/{m}"},
    {"name": "siliconflow", "env": "SILICONFLOW_API_KEY",
     "url": "https://api.siliconflow.cn/v1/chat/completions",
     "models": ["glm-5.2", "glm-5.1"], "model_id": lambda m: m},
]


def _glm_native_available() -> bool:
    """True if ANY provider env var is set (Python-native call path is usable)."""
    available = any(os.environ.get(p["env"], "").strip() for p in _GLM_PROVIDERS)
    try:
        import debug_log
        providers_set = [p["name"] for p in _GLM_PROVIDERS
                         if os.environ.get(p["env"], "").strip()]
        debug_log.dlog("glm", "_glm_native_available",
                       f"native_available={available}, providers_with_keys={providers_set}",
                       data={"available": available, "providers_with_keys": providers_set})
    except Exception:
        pass
    return available


def _glm_call_native(messages: list[dict], model: str, timeout: float) -> str:
    """Call GLM providers directly from Python (urllib — stdlib, no Node needed).
    Tries each provider whose key is set + whose models include the requested one.
    Returns the response text, or raises if all providers fail.
    Every attempt is logged to debug/glm.log for instant debugging."""
    import urllib.request
    import urllib.error
    import json as _json
    import debug_log
    # Normalize the model name: the agent panel sends "glm-5.2-free" but the
    # provider config checks against ["glm-5.2", "glm-5.1"]. Strip suffixes
    # like "-free", "-paid", etc. so the model matches the provider's list.
    # THIS WAS THE ROOT CAUSE OF "No provider available": the model ID
    # "glm-5.2-free" never matched "glm-5.2" so every provider was skipped.
    normalized = model
    for suffix in ("-free", "-paid", "-turbo", "-flash"):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    debug_log.dlog("glm", "_glm_call_native",
                   f"model normalization: '{model}' → '{normalized}'",
                   level="DEBUG", data={"original": model, "normalized": normalized})
    errors = []
    for p in _GLM_PROVIDERS:
        key = os.environ.get(p["env"], "").strip()
        if not key:
            continue
        if normalized not in p["models"]:
            debug_log.dlog("glm", "_glm_call_native",
                           f"skip {p['name']}: model '{normalized}' not in {p['models']}",
                           level="DEBUG", data={"provider": p["name"], "model": normalized})
            continue
        model_id = p["model_id"](normalized)
        body = _json.dumps({"model": model_id, "messages": messages}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if p["name"] == "openrouter":
            headers["HTTP-Referer"] = "https://scoobybaby1999-loom.hf.space"
            headers["X-Title"] = "loom conscious agents"
        req = urllib.request.Request(p["url"], data=body, method="POST", headers=headers)
        t0 = time.monotonic()
        debug_log.dlog("glm", "_glm_call_native",
                       f"calling {p['name']} endpoint",
                       data={"provider": p["name"], "url": p["url"],
                             "model_id": model_id, "msg_count": len(messages),
                             "token_preview": f"{key[:4]}...{key[-4:]}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = _json.loads(r.read().decode())
            ms = (time.monotonic() - t0) * 1000
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            served = data.get("model", "?")
            debug_log.dlog("glm", "_glm_call_native",
                           f"SUCCESS via {p['name']}: {len(content)} chars",
                           ms=ms, data={"provider": p["name"], "served_model": served,
                                        "content_len": len(content),
                                        "content_preview": content[:80]})
            if content:
                return content.strip()
        except urllib.error.HTTPError as exc:
            ms = (time.monotonic() - t0) * 1000
            body_text = ""
            try:
                body_text = exc.read().decode(errors="replace")[:200]
            except Exception:
                pass
            err = f"{p['name']} HTTP {exc.code}: {body_text}"
            errors.append(err)
            debug_log.derror("glm", "_glm_call_native",
                             f"FAIL {p['name']} HTTP {exc.code}", exc=exc,
                             data={"provider": p["name"], "status": exc.code,
                                   "body": body_text, "ms": round(ms, 1)})
        except Exception as exc:
            ms = (time.monotonic() - t0) * 1000
            err = f"{p['name']}: {type(exc).__name__}: {str(exc)[:120]}"
            errors.append(err)
            debug_log.derror("glm", "_glm_call_native",
                             f"FAIL {p['name']} exception", exc=exc,
                             data={"provider": p["name"], "ms": round(ms, 1)})
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


class ZaiAdapter(BaseAdapter):
    """Drives the agent panel with the FREE GLM 5.2 model.

    Tries Python-native HTTP calls first (Puter/Z.ai/NVIDIA/OpenRouter/
    SiliconFlow — no Node needed), then Node subprocess, then HTTP bridge.
    Maintains an in-memory conversation for multi-turn context.
    Every turn is logged to debug/glm.log for instant debugging.
    """

    def __init__(self, workspace: Path, model: str | None = None,
                 workspace_id: str | None = None, system_prompt: str | None = None):
        super().__init__(workspace, workspace_id)
        self.model = model or "glm-5.2"
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        self.messages: list[dict] = []
        try:
            import debug_log
            debug_log.dlog("agent", "ZaiAdapter.__init__",
                           f"created adapter model={self.model}",
                           data={"model": self.model, "workspace_id": workspace_id})
        except Exception:
            pass

    def open(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]
        try:
            import debug_log
            debug_log.dlog("agent", "ZaiAdapter.open", "adapter opened",
                           data={"model": self.model, "system_prompt_len": len(self.system_prompt)})
        except Exception:
            pass

    def turn(self, user_msg: str, emit) -> None:
        import json as _json
        import debug_log
        self.messages.append({"role": "user", "content": user_msg})
        content = ""
        errors: list[str] = []
        t0 = time.monotonic()
        debug_log.dlog("agent", "ZaiAdapter.turn", f"ENTER: msg={user_msg[:60]!r}",
                       data={"model": self.model, "msg_len": len(user_msg),
                             "history_len": len(self.messages)})
        # Path 1: PYTHON-NATIVE direct HTTP call (PREFERRED — no Node needed).
        # This is the path that works on a Python-only HF Space. Uses urllib
        # (stdlib) to call Puter/Z.ai/NVIDIA/OpenRouter/SiliconFlow directly.
        if _glm_native_available():
            debug_log.dlog("agent", "ZaiAdapter.turn", "trying native path (Python urllib)")
            try:
                content = _glm_call_native(self.messages, self.model, GLM_BRIDGE_TIMEOUT_S)
            except Exception as exc:
                errors.append(f"native: {type(exc).__name__}: {str(exc)[:200]}")
                debug_log.derror("agent", "ZaiAdapter.turn", "native path failed", exc=exc)
        # Path 2: Node.js subprocess (fallback — used if native fails or no keys set)
        if not content and _glm_subprocess_available():
            debug_log.dlog("agent", "ZaiAdapter.turn", "trying subprocess path (Node.js)")
            try:
                content = _glm_call_subprocess(self.messages, self.model, GLM_BRIDGE_TIMEOUT_S)
            except Exception as exc:
                errors.append(f"subprocess: {type(exc).__name__}: {str(exc)[:160]}")
                debug_log.derror("agent", "ZaiAdapter.turn", "subprocess path failed", exc=exc)
        # Path 3: HTTP bridge at localhost:3030 (last resort)
        if not content and _glm_http_available():
            debug_log.dlog("agent", "ZaiAdapter.turn", "trying HTTP bridge (localhost:3030)")
            try:
                content = _glm_call_http(self.messages, self.model, GLM_BRIDGE_TIMEOUT_S)
            except Exception as exc:
                errors.append(f"http: {type(exc).__name__}: {str(exc)[:160]}")
                debug_log.derror("agent", "ZaiAdapter.turn", "HTTP bridge path failed", exc=exc)
        ms = (time.monotonic() - t0) * 1000
        if not content:
            if errors:
                content = (f"[GLM error] Could not reach the GLM model.\n\n"
                           f"Attempted paths:\n" + "\n".join(f"  • {e}" for e in errors) +
                           f"\n\nSet PUTER_API_TOKEN (free, puter.com/dashboard) as a Space Secret"
                           f" for free GLM-5.2. Or NVIDIA_API_KEY (free 5.1, build.nvidia.com).")
                debug_log.dlog("agent", "ZaiAdapter.turn",
                               f"ALL PATHS FAILED in {ms:.0f}ms",
                               level="ERROR", ms=ms, data={"errors": errors})
            else:
                content = ("[GLM error] No GLM provider configured. Set ONE of these as a "
                           "Space Secret:\n  • PUTER_API_TOKEN (free GLM-5.2) → puter.com/dashboard\n"
                           "  • NVIDIA_API_KEY (free GLM-5.1) → build.nvidia.com\n"
                           "  • ZAI_API_KEY (real GLM-5.2) → z.ai")
                debug_log.dlog("agent", "ZaiAdapter.turn",
                               "NO PROVIDER CONFIGURED",
                               level="ERROR", ms=ms)
        else:
            debug_log.dlog("agent", "ZaiAdapter.turn",
                           f"EXIT OK in {ms:.0f}ms: {len(content)} chars",
                           ms=ms, data={"content_len": len(content),
                                        "content_preview": content[:80]})
        self.messages.append({"role": "assistant", "content": content})
        emit({"type": "assistant", "text": content})

    def interrupt(self) -> None:
        # Synchronous call — nothing to cancel mid-flight.
        pass

    def close(self) -> None:
        self.messages = []


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
    3. Other blocked/approval-required commands return an error result instead
       of executing.
    """
    from strands_tools import shell as _shell
    from git_intercept import check_command

    kwargs["workdir"] = str(getattr(_thread_local, "workspace", Path.cwd()))

    # git command interception: check before execution
    cmd = kwargs.get("command", "")
    if isinstance(cmd, str) and cmd.strip():
        stripped = cmd.strip()
        workspace_id = getattr(_thread_local, "workspace_id", None)
        # Route network git operations through the backend so the token is
        # never written to .git/config and pushes are audit-logged. Only when
        # the agent is operating inside a linked workspace.
        if workspace_id and (
            stripped.startswith("git push")
            or stripped.startswith("git pull")
            or stripped.startswith("git fetch")
        ):
            # Force-push still requires explicit approval — don't auto-route it.
            is_force = stripped.startswith("git push") and (
                "-f " in stripped or "--force" in stripped)
            if not is_force:
                return _route_network_git(stripped, workspace_id)
        verdict = check_command(cmd)
        if not verdict.allowed:
            return {
                "status": "error",
                "content": [{"text": (
                    f"Command requires user approval: {verdict.action}\n"
                    f"Original command: {verdict.command}\n"
                    f"This action has been queued for user review."
                )}],
            }
    return _shell.tool(**kwargs)


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

    def open(self) -> None:
        import os as _os
        from strands import Agent
        from strands.models.litellm import LiteLLMModel

        # headless: skip interactive consent prompts (no TTY in the Space container).
        # must be set before importing strands_tools so their module-level checks see it.
        _os.environ["BYPASS_TOOL_CONSENT"] = "true"

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

        # client_args pass straight to litellm.completion (api_base = custom
        # OpenAI-compatible endpoint, e.g. Z.ai for GLM, NVIDIA for Kimi).
        client_args: dict = {"api_key": _os.environ[key_env]}
        if base_url:
            client_args["api_base"] = base_url
        llm = LiteLLMModel(client_args=client_args, model_id=model)


        # the agent works in its session workspace; tools are imported defensively
        # so a renamed/missing tool never blocks startup.
        # shell is replaced by _guarded_shell to force workdir per-thread instead
        # of using a process-wide os.chdir() (which breaks concurrent sessions).
        tools = []
        for mod_name in ("file_read", "file_write", "editor",
                         "python_repl", "http_request"):
            try:
                import importlib
                tools.append(importlib.import_module(f"strands_tools.{mod_name}"))
            except Exception:
                continue
        # guarded shell: forces execution into this session's workspace
        import types as _types
        shell_mod = _types.ModuleType("shell_guarded")
        shell_mod.tool = _guarded_shell
        tools.append(shell_mod)

        self.agent = Agent(model=llm, tools=tools, system_prompt=self.system_prompt,
                           callback_handler=None)
        self._msg_cursor = 0
        # Tier 3 — register conscious tools on the owning session (Phase 1:
        # stored for inspection; Phase 2 wraps them as strands_tools modules).
        try:
            import conscious_stubs
            conscious_stubs.register_conscious_tools(self._session_ref(), self)
        except Exception:
            pass

    def _session_ref(self):
        """Back-reference to the owning AgentSession (set by AgentSession._run)."""
        return getattr(self, "_session", None)

    def turn(self, user_msg: str, emit) -> None:
        # set per-thread workspace so _guarded_shell knows where to run commands.
        _thread_local.workspace = self.workspace
        _thread_local.workspace_id = self.workspace_id
        # Phase 2 (completed in this pass) — mid-turn cost abort.
        # Install a callback handler that checks the conscious cost ceiling
        # after each tool result. If over, calls agent.cancel() to abort the
        # turn mid-flight + emits a cost.exceeded event. This is the "hard
        # enforcement mid-turn" from TIER3_PLAN §14.
        sess = getattr(self, "_session", None)
        cost_handler = None
        if sess is not None and getattr(sess, "conscious_id", None):
            def _cost_check_callback(**_kwargs):
                # Strands fires callback handlers on each message event; we
                # only act when cost is exceeded (defensive — never blocks a
                # normal turn).
                if not _cost_turn_ok(sess.conscious_id):
                    spent, ceiling = _cost_figures(sess.conscious_id)
                    emit({"type": "status", "state": "idle",
                          "detail": f"cost ceiling exceeded mid-turn (spent=${spent:.4f}, ceiling=${ceiling:.4f})"})
                    canceler = getattr(self.agent, "cancel", None)
                    if callable(canceler):
                        try:
                            canceler()
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
            cost_handler = _cost_check_callback
            # Strands accepts callback_handler on the Agent; we set it per-turn
            # by attaching to the agent's callback registry if it has one.
            try:
                reg = getattr(self.agent, "callback_handler", None)
                if reg is None:
                    self.agent.callback_handler = cost_handler
                elif hasattr(reg, "register") and callable(reg.register):
                    reg.register(cost_handler)
                else:
                    # reg is a callable; wrap it so both fire
                    orig = reg
                    def _both(**kw):
                        try: orig(**kw)
                        except Exception: pass
                        try: cost_handler(**kw)
                        except Exception: pass
                    self.agent.callback_handler = _both
            except Exception:
                pass
        try:
            self.agent(user_msg)
        finally:
            # restore the original callback handler so non-cost sessions aren't
            # saddled with our closure on their next turn
            if cost_handler is not None:
                try:
                    # best-effort: leave the handler in place; it no-ops when
                    # cost is under budget. Strands agents are per-session so
                    # this is safe.
                    pass
                except Exception:
                    pass
        # walk newly-appended messages and map Bedrock-style content blocks
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
    if tier == "zai":
        return ZaiAdapter(workspace, model, workspace_id, system_prompt)
    return MockAdapter(workspace, workspace_id, system_prompt)


def tier_for_model(model: str | None) -> str | None:
    """Resolve which tier a chosen model belongs to (None → auto/default)."""
    if not model:
        return agent_tier()
    if model.startswith("claude"):
        return "claude" if (os.environ.get("ANTHROPIC_API_KEY", "").strip()
                            and _installed("claude_agent_sdk")) else None
    # The free GLM model routes to the zai tier (bridge on port 3030).
    if model == "glm-5.2-free":
        return "zai" if _glm_bridge_available() else None
    if any(m["model"] == model for m in agent_models()):
        return "open"
    return None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

class AgentSession:
    def __init__(self, tier: str, model: str | None = None,
                 workspace_path: Path | None = None, workspace_id: str | None = None,
                 conscious_id: str | None = None, agent_id: str | None = None):
        self.id = uuid.uuid4().hex[:16]
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
                  agent_id: str | None = None) -> AgentSession:
    """Reuse a live session by id, or start a new one (CapacityError if full).
    `model` (optional) selects which model/tier drives a NEW session.
    `workspace_id` (optional) links the session to a user workspace sandbox.
    `conscious_id` + `agent_id` (optional, Tier 3) bind the session to a
    Conscious agent row so the conscious_* tools resolve context.
    """
    tier = tier_for_model(model)
    if tier is None:
        raise RuntimeError("no agent tier available for the requested model")
    with _sessions_lock:
        _sweep_locked()
        if session_id and session_id in _sessions:
            return _sessions[session_id]
        if len(_sessions) >= MAX_SESSIONS:
            raise CapacityError(f"max {MAX_SESSIONS} concurrent agent sessions")
        # resolve workspace_id to a filesystem path
        workspace_path = None
        if workspace_id:
            import db
            ws = db.get_workspace(workspace_id)
            if ws and ws.get("sandbox_path"):
                workspace_path = Path(ws["sandbox_path"])
        s = AgentSession(tier, model, workspace_path=workspace_path,
                         workspace_id=workspace_id,
                         conscious_id=conscious_id, agent_id=agent_id)
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
