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

from oplog import log_event
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

AGENT_ROOT = Path(os.environ.get("AGENT_ROOT", "/tmp/agent"))
SESSION_TTL_S = int(os.environ.get("AGENT_SESSION_TTL_S", "7200"))   # 2h idle
MAX_SESSIONS = int(os.environ.get("AGENT_MAX_SESSIONS", "32"))        # RAM bound
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
    "- memory: read/write the workspace memory layer (.pied sanity log)\n"
    "- delegate: spawn a sub-agent for a sub-task (multi-agent orchestration)\n"
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
    "Use memory to read/write the workspace memory layer (.pied sanity log) — "
    "read it at the start of each task to know the current goal, plan, and "
    "recent events. Write decisions and findings to the blackboard so other "
    "agents can see them. "
    "You can git clone repos, install packages, run build tools, and do "
    "anything a developer terminal can do. Lead with the outcome, not the process.\n\n"
    "ISSUE-5 (RESPONSIVE-FIX): TOOL CALLING — The tools listed above are "
    "available as FUNCTION CALLS via the model's native tool-calling API "
    "(OpenAI function-calling format). You MUST invoke tools via the "
    "function-calling mechanism, NOT by emitting tool calls as plain text. "
    "For example, do NOT write `memory {\"action\": \"read\"}` or "
    "`shell(command=\"ls\")` as text — instead, emit a function_call with "
    "the tool name and arguments. The runtime executes the function call "
    "and returns the result as a tool_result. Emitting tool calls as text "
    "will NOT execute them — the user will see raw text instead of results."
)

#   open-tier model routing: dynamically built from providers_catalog.json +
#   synced models from each provider's /v1/models endpoint.
#   Each entry is (env key, provider label, litellm model string, base_url or None).
#   Order = priority for auto-pick (first present env var wins). The model
#   picker surfaces EVERY entry whose key is set, not just the first.
#   Overridable via AGENT_OPEN_MODEL / AGENT_OPEN_BASE_URL / AGENT_OPEN_KEY_ENV.

_open_models_cache: list[tuple[str, str, str, str | None, dict | None, str]] | None = None


# Each entry: (env_var, label, litellm_model, base_url, extra_headers, provider_name)
# provider_name is the canonical catalog name (e.g. "cloudflare", "github-models")
# used for provider-hint matching in _resolve_open_model — the env_var name and
# the label's parenthesised suffix are NOT reliable for this (e.g. Cloudflare's
# env_var is CF_API_TOKEN which reduces to "cf", not "cloudflare").


def _build_open_models() -> list[tuple[str, str, str, str | None, dict | None, str]]:
    """Build open model entries from providers_catalog.json dynamically.

    Each configured provider contributes ALL its models (env-var-gated), so
    every model from NVIDIA, Cloudflare, PrivateMode AI etc. is available in
    the agent without hardcoding model names.

    Returns 6-tuples: ``(env_var, label, litellm_model, base_url, extra_headers,
    provider_name)``. ``extra_headers`` is a dict of HTTP headers the provider
    requires (e.g. OpenRouter needs HTTP-Referer + X-Title for free models).
    ``provider_name`` is the canonical catalog name (e.g. "cloudflare") used
    for provider-hint matching in ``_resolve_open_model``.
    """
    global _open_models_cache
    # Re-probe if the cache is empty — the panel sync may not have completed
    # on the first call (cache poisoning fix). A non-empty cache is still
    # trusted (the catalog + synced models don't change at runtime).
    # DON'T cache an empty result — the sync may complete later and we want
    # to pick it up on the next call.
    if _open_models_cache is not None and len(_open_models_cache) > 0:
        return _open_models_cache

    entries: list[tuple[str, str, str, str | None, dict | None, str]] = []
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
            entries.append((env_var, label, litellm_model,
                            base_url or None, extra_headers, name))

    # Also include dynamically synced models from providers with sync_config.
    from provider_sync import get_panel_sync_cache
    sync_cache = get_panel_sync_cache()
    if sync_cache:
        # Dedupe by (provider_name, model_last_segment) so each provider can
        # host its own copy of a shared model (e.g. both NVIDIA and Cloudflare
        # expose "glm-5.2"). The previous dedup-by-last-segment kept only the
        # first provider's copy, which broke provider-hint routing in
        # _resolve_open_model: when the user picked "cloudflare/glm-5.2" the
        # Cloudflare entry had been dropped, so Pass 2 found no match and Pass
        # 3 returned NVIDIA's copy instead (the wrong-provider bug).
        seen: set[tuple[str, str]] = set(
            (pname, m.split("/")[-1]) for _, _, m, _, _, pname in entries)
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
                key = (name, last)
                if key not in seen:
                    seen.add(key)
                    entries.append((senv, f"{mid} ({name})",
                                    f"openai/{mid}", sbase or None, sheaders, name))

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
    matched against the canonical catalog provider name (preferred), the
    env_var's reduced name, and the label's parenthesised suffix.

    Returns ``(litellm_model, base_url, env_var, provider_name, extra_headers)``
    or ``None``. The 4th tuple element is the canonical catalog provider name
    (e.g. "cloudflare") when known, falling back to the human-readable label
    for back-compat with callers that display it.
    """
    if not user_model:
        return None
    user_last = user_model.split("/")[-1]
    # Also extract a provider hint if the user passed "provider/model"
    user_provider = user_model.split("/")[0] if "/" in user_model else ""
    user_provider_lc = user_provider.lower()
    # Normalise common separator mismatches: "github-models" vs "github_models"
    # vs "githubmodels". The catalog uses dashes; env vars use underscores.
    user_provider_norm = user_provider_lc.replace("-", "").replace("_", "")
    models = _build_open_models()
    # Pass 1: exact full match (canonical litellm string)
    for _env, _label, model, base, extra, _pname in models:
        if model == user_model:
            return (model, base, _env, _pname or _label, extra)
    # Pass 2: match by provider hint + last segment (e.g. "cloudflare/glm-5.2"
    # or "privatemodeai/kimi-k2.6"). The provider hint is matched against
    # ALL of: the canonical catalog provider_name (preferred — handles
    # Cloudflare's CF_API_TOKEN → "cf" mismatch), the env_var's reduced name,
    # and the label's parenthesised suffix. Without the provider_name check,
    # Pass 2 fails for Cloudflare (env_var "CF_API_TOKEN" → "cf", which does
    # not contain "cloudflare") and falls through to Pass 3, which returns
    # the FIRST provider in catalog order (e.g. NVIDIA) — wrong provider.
    if user_provider:
        for env, label, model, base, extra, pname in models:
            model_last = model.split("/")[-1]
            if model_last != user_last:
                continue
            # Canonical catalog provider name (e.g. "cloudflare").
            pname_lc = (pname or "").lower()
            pname_norm = pname_lc.replace("-", "").replace("_", "")
            # label looks like "kimi-k2.6 (PrivateMode AI)" — extract provider
            label_provider = ""
            if "(" in label and ")" in label:
                label_provider = label[ label.index("(") + 1 : label.rindex(")") ].strip().lower()
            # env var maps to provider: PRIVATEMODEAI_API_KEY -> privatemodeai
            env_provider = env.lower().replace("_api_key", "").replace("_token", "").replace("_api_token", "")
            env_provider_norm = env_provider.replace("-", "").replace("_", "")
            if (user_provider_lc == pname_lc
                    or user_provider_norm == pname_norm
                    or user_provider_lc in env_provider
                    or user_provider_norm in env_provider_norm
                    or user_provider_lc in label_provider
                    or user_provider_norm in label_provider.replace("-", "").replace("_", "")):
                # Return the canonical provider name (4th element) so callers
                # can display it; fall back to the label for back-compat.
                return (model, base, env, pname or label, extra)
    # Pass 3: last-segment match (bare logical names, partial names).
    # Catalog order wins when the same model name exists on multiple providers
    # AND no provider hint was supplied.
    for env, label, model, base, extra, pname in models:
        if model.split("/")[-1] == user_last:
            return (model, base, env, pname or label, extra)
    # Pass 4: FALLBACK — if _build_open_models() returned empty (sync cache
    # not populated yet), do a LIVE fetch from the provider APIs to find the
    # model. This is fully dynamic — no hardcoded model names.
    _user_last_clean = user_last.replace(":free", "").replace("openai/", "")
    _PROVIDER_FETCH = [
        ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/models", "nvidia",
         "https://integrate.api.nvidia.com/v1"),
        ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/models", "openrouter",
         "https://openrouter.ai/api/v1"),
    ]
    for env, url, pname, base in _PROVIDER_FETCH:
        if not os.environ.get(env, "").strip():
            continue
        try:
            import urllib.request as _ur
            req = _ur.Request(url, headers={
                "Authorization": f"Bearer {os.environ[env]}",
                "User-Agent": "doomalaysocreate/1.0",
            })
            with _ur.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            for m in data.get("data", []):
                mid = m.get("id", "")
                mid_last = mid.split("/")[-1]
                if (mid_last == _user_last_clean or
                    _user_last_clean in mid or
                    mid in user_model):
                    litellm_model = f"openai/{mid}"
                    log_event("resolve_open_model_live_fetch",
                              provider=pname, model=litellm_model, requested=user_model)
                    return (litellm_model, base, env, pname, None)
        except Exception as e:
            log_event("resolve_open_model_fetch_error", provider=pname, error=str(e)[:200])
    return None


def _installed(module: str) -> bool:
    """True if a module is importable, without importing it (cheap)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _reset_open_models_cache():
    """Reset the open models cache so the next call re-probes."""
    global _open_models_cache
    _open_models_cache = None


def _pick_open_llm() -> tuple[str, str, str | None] | None:
    """(env_key, model, base_url) for the default open model, or None if no key set.

    Prefers known-good, fast, reliable models when multiple providers are
    available. The previous version picked the FIRST model in catalog order
    (often an obscure NVIDIA model like dracarys-llama that times out),
    which caused the "first message gets no response" bug.
    """
    key_env = os.environ.get("AGENT_OPEN_KEY_ENV", "").strip()
    if key_env and os.environ.get(key_env, "").strip():
        return (key_env,
                os.environ.get("AGENT_OPEN_MODEL", "groq/llama-3.3-70b-versatile"),
                os.environ.get("AGENT_OPEN_BASE_URL", "").strip() or None)
    # Build the list of AVAILABLE models (key set)
    available = []
    for env_key, _label, model, base_url, _extra, _pname in _build_open_models():
        if os.environ.get(env_key, "").strip():
            available.append((env_key, model, base_url))
    # Preference order: known-good, fast, reliable models first
    _PREFERRED = [
        "glm-5.2", "glm-5.1", "kimi-k2.6", "deepseek-v4-flash",
        "llama-3.3-70b-versatile", "qwen-3-235b", "nemotron-3-ultra",
        "gemma-4-31b", "llama-3.3-70b", "phi-4-reasoning",
    ]
    for pref in _PREFERRED:
        for env_key, model, base_url in available:
            if pref in model.lower():
                return (env_key, model, base_url)
    # Fall back to the first available
    if available:
        return available[0]
    # LAST RESORT: the sync cache hasn't populated yet. Do a LIVE fetch
    # from the first provider that has a key set. This is dynamic — no
    # hardcoded model names. We fetch the provider's model list and pick
    # the first one (or a preferred one if available).
    _PROVIDER_FETCH = [
        ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/models", "nvidia",
         "https://integrate.api.nvidia.com/v1"),
        ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/models", "openrouter",
         "https://openrouter.ai/api/v1"),
    ]
    for env, url, pname, base in _PROVIDER_FETCH:
        if os.environ.get(env, "").strip():
            try:
                import urllib.request as _ur
                req = _ur.Request(url, headers={
                    "Authorization": f"Bearer {os.environ[env]}",
                    "User-Agent": "doomalaysocreate/1.0",
                })
                with _ur.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                models = data.get("data", [])
                if models:
                    # Pick the first model (or a preferred one if available)
                    _PREFERRED = ["glm-5.2", "kimi-k2.6", "deepseek-v4-flash", "llama-3.3-70b"]
                    chosen = None
                    for pref in _PREFERRED:
                        for m in models:
                            mid = m.get("id", "")
                            if pref in mid.lower():
                                chosen = mid
                                break
                        if chosen:
                            break
                    if not chosen:
                        chosen = models[0].get("id", "")
                    if chosen:
                        # Add openai/ prefix for LiteLLM routing
                        litellm_model = f"openai/{chosen}"
                        log_event("pick_open_llm_live_fetch", provider=pname, model=litellm_model)
                        return (env, litellm_model, base)
            except Exception as e:
                log_event("pick_open_llm_fetch_error", provider=pname, error=str(e)[:200])
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
        # Dedupe by (provider_name, model) so each provider's copy of a shared
        # model is surfaced separately (e.g. NVIDIA's glm-5.2 AND Cloudflare's
        # glm-5.2). The frontend can route to a specific provider by sending
        # "<provider_name>/<model_last_segment>" as the model id, which
        # _resolve_open_model() Pass 2 matches against the canonical provider
        # name. The previous dedup-by-model kept only the first provider's
        # copy, which made provider-specific routing impossible.
        seen: set[tuple[str, str]] = set()
        for env_key, label, model, _base, _extra, pname in _build_open_models():
            if not os.environ.get(env_key, "").strip():
                continue
            key = (pname or "", model)
            if key in seen:
                continue
            seen.add(key)
            # Build a provider-qualified model id the frontend can send back
            # to route to THIS provider's copy (e.g. "cloudflare/glm-5.2").
            # Falls back to the litellm model string when no provider name
            # is known (back-compat for hand-written entries).
            model_last = model.split("/")[-1]
            provider_model = f"{pname}/{model_last}" if pname else model
            out.append({"tier": "open", "provider": label, "model": model,
                        "label": label, "provider_name": pname or None,
                        "provider_model": provider_model,
                        "default": False})
    if out and not any(m["default"] for m in out):
        out[0]["default"] = True
    return out


def _model_key_env(model: str) -> str | None:
    """Env var name for the API key of a chosen open model."""
    model_last = model.split("/")[-1]
    for env_key, _label, m, _base, _extra, _pname in _build_open_models():
        if m == model or m.split("/")[-1] == model_last:
            return env_key
    return None

def _model_base_url(model: str) -> str | None:
    """base_url for a chosen open model (matches the dynamic model list)."""
    model_last = model.split("/")[-1]
    for _env, _label, m, base, _extra, _pname in _build_open_models():
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
                 workspace_id: str | None = None, system_prompt: str | None = None,
                 effort: str | None = None, panel=None,
                 web_search: bool = False):
        super().__init__(workspace, workspace_id)
        self.model = model
        self.agent = None
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        # Effort level (low|med|high|max) — when set to non-"low",
        # open() resolves the per-provider reasoning body from
        # reasoning_catalog.json via lib/provider_tools.py and forwards
        # it to LiteLLM as additional_request_params.extra_body. This is
        # the programmatic way to invoke reasoning (vs. feeding prompts).
        self.effort = effort
        self.panel = panel
        # When True and the provider supports NATIVE web search
        # (e.g. OpenRouter :online / plugins:[{id:web}]), open() merges
        # the native-web-search body into the request. Falls back to the
        # Strands `web_search` tool (Tavily/DuckDuckGo) when not supported.
        self.web_search = bool(web_search)
        # Resolved routing info (set by open(), surfaced in snapshot for verification)
        self.resolved_model: str | None = None
        self.resolved_provider: str | None = None
        self.resolved_api_base: str | None = None
        # Initialize the memory layer (.pied sanity log)
        try:
            import memory_layer
            memory_layer.init_memory(workspace)
            # Inject memory context into the system prompt so the agent
            # knows the current goal, plan, recent events, and tasks.
            memory_ctx = memory_layer.get_context_for_agent(workspace)
            self.system_prompt = self.system_prompt + "\n\n" + memory_ctx
        except Exception:
            pass

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
        # stream=True: streaming mode releases the GIL between token chunks,
        # which allows the thread timeout to fire if the LLM hangs. Also gives
        # the user real-time token-by-token feedback. Modern Strands handles
        # streaming tool_use correctly (generates UUIDs for missing toolUseIds).
        #
        # PROGRAMMATIC SKILL INVOCATION (PROVIDER-SKILLS task):
        # resolve the per-(provider, model) reasoning body from
        # reasoning_catalog.json and the native-web-search body (when
        # the user asked for web search AND this provider supports it).
        # These get shallow-merged into the request body via LiteLLM's
        # `extra_body` kwarg — litellm forwards extra_body to the underlying
        # OpenAI-compatible HTTP request. This is the documented way to
        # invoke reasoning (e.g. OpenRouter `reasoning:{enabled:true}`,
        # NVIDIA `reasoning_effort:"high"`, CF `chat_template_kwargs`)
        # and OpenRouter's native web search plugin — instead of feeding
        # prompts and hoping the model decides to think.
        extra_body: dict = {}
        try:
            import provider_tools
            import effort_detector
            # Resolve the canonical provider name from _resolve_open_model
            # (4th tuple element); fall back to key_env-derived name.
            _provider_for_caps = provider_label or key_env
            _enable_reasoning = bool(self.effort and self.effort != "low" and self.effort != "off")
            _enable_native_ws = bool(self.web_search)
            # If native web search is requested but unsupported, fall back
            # to Strands' built-in web_search tool (Tavily/DuckDuckGo) —
            # which is already loaded below in the tools list.
            if _enable_native_ws and not provider_tools.supports_native_web_search(
                    _provider_for_caps, model):
                _enable_native_ws = False
            # Use effort_detector for the correct per-provider effort body.
            # This translates the user-selected effort level (e.g. "on", "high",
            # "max") to the correct provider-specific body (e.g.
            # chat_template_kwargs:{thinking:true} or reasoning_effort:"high").
            if _enable_reasoning and self.effort:
                extra_body = effort_detector.detect_effort_body(
                    _provider_for_caps, model, self.effort)
                if not extra_body:
                    # Fall back to the old catalog-based resolution
                    extra_body = provider_tools.reasoning_body_for(
                        _provider_for_caps, model)
            if _enable_native_ws:
                ws_body = provider_tools.native_web_search_body(
                    _provider_for_caps, model)
                if ws_body:
                    extra_body.update(ws_body)
            if extra_body:
                try:
                    log_event("agent_extra_body", provider=_provider_for_caps,
                              model=model, keys=list(extra_body.keys()),
                              effort=self.effort, web_search=_enable_native_ws)
                except Exception:
                    pass
        except Exception:
            extra_body = {}
        # Add a timeout so LiteLLM doesn't hang forever on an unresponsive
        # provider. 60s is generous for reasoning models but bounded.
        client_args["timeout"] = 60
        # Retry on 429 (rate limit) and 5xx errors — LitellM handles this
        # natively via num_retries with exponential backoff.
        client_args["num_retries"] = 3
        llm_kwargs = dict(client_args=client_args, model_id=model, stream=True)
        if extra_body:
            # Strands LiteLLMModel forwards additional_request_params as
            # **kwargs to litellm.completion(), which forwards extra_body
            # to the OpenAI-compatible HTTP request body.
            llm_kwargs["additional_request_params"] = {"extra_body": extra_body}
        llm = LiteLLMModel(**llm_kwargs)


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

        # 4. memory tool — read/write the .pied sanity log
        try:
            _ws = self.workspace
            @strands_tool_decorator(name="memory", description=(
                "Read or write the workspace memory layer (.pied sanity log). "
                "Use 'read' to get current state, goal, plan, and recent events. "
                "Use 'write' to post a decision, finding, or update the goal/plan. "
                "Use 'log' to append to the event log. "
                "Actions: read, write, log, update_goal, update_plan, add_task, complete_task."
            ))
            def memory(action: str, section: str = "", key: str = "", value: str = "", task: str = "") -> str:
                """Access the workspace memory layer.
                action: read|write|log|update_goal|update_plan|add_task|complete_task
                section: blackboard section (for write)
                key: blackboard key (for write)
                value: the value to write
                task: the task text (for add_task/complete_task)
                """
                import memory_layer
                if action == "read":
                    return memory_layer.get_context_for_agent(_ws, max_chars=8000)
                elif action == "write":
                    memory_layer.post_blackboard(_ws, section, key, value, "agent")
                    return f"Posted to blackboard [{section}/{key}]"
                elif action == "log":
                    memory_layer.log_event(_ws, "agent", "manual_log", {"message": value})
                    return "Logged"
                elif action == "update_goal":
                    memory_layer.update_goal(_ws, value)
                    return f"Goal updated: {value}"
                elif action == "update_plan":
                    memory_layer.update_plan(_ws, value)
                    return f"Plan updated: {value}"
                elif action == "add_task":
                    memory_layer.add_task(_ws, task, "agent")
                    return f"Task added: {task}"
                elif action == "complete_task":
                    memory_layer.complete_task(_ws, task, "agent")
                    return f"Task completed: {task}"
                else:
                    return f"Unknown action: {action}. Use: read, write, log, update_goal, update_plan, add_task, complete_task"

            tools.append(memory)
        except Exception:
            pass

        # 5. delegate tool — spawn a sub-agent for a sub-task (multi-agent orchestration)
        # The orchestrator can delegate work to sub-agents, each running in the same
        # workspace but with their own agent session. The memory layer (.pied) serves
        # as the shared state — sub-agents read the goal/plan and write findings.
        try:
            _ws = self.workspace
            _model = self.model
            @strands_tool_decorator(name="delegate", description=(
                "Delegate a sub-task to a sub-agent. The sub-agent runs in the same "
                "workspace with its own session. Use for parallel work, code review, "
                "or breaking complex tasks into smaller pieces. The sub-agent can "
                "use all tools (shell, file ops, memory, etc.). Results are written "
                "to the memory layer."
            ))
            def delegate(task: str, model: str = "") -> str:
                """Spawn a sub-agent for a sub-task.
                task: the task description (be specific)
                model: optional model override (defaults to the same model)
                """
                import memory_layer
                # Log the delegation
                sub_id = uuid.uuid4().hex[:8]
                memory_layer.log_event(_ws, "orchestrator", "delegate", {
                    "sub_agent": sub_id, "task": task[:200]
                })
                # Create a sub-agent session
                try:
                    sub_model = model.strip() if model.strip() else _model
                    sub_session = get_or_create(
                        model=sub_model,
                        workspace_path=_ws,
                        workspace_id=getattr(_thread_local, "workspace_id", None),
                        chat_session_id=None,  # sub-agents don't persist to chat
                    )
                    # Submit the task
                    sub_session.submit(
                        f"You are a sub-agent (id: {sub_id}). Your task: {task}\n\n"
                        f"Read the memory layer first (memory(action='read')) to understand "
                        f"the current goal and plan. After completing your task, write your "
                        f"findings to the memory layer (memory(action='write', section='findings', "
                        f"key='{sub_id}', value='your findings')). Then report your result."
                    )
                    # Wait for the sub-agent to finish (with timeout)
                    import time as _time
                    deadline = _time.time() + 120  # 2 min timeout
                    while _time.time() < deadline:
                        if sub_session.status in ("idle", "error"):
                            break
                        _time.sleep(2)
                    # Collect the result
                    if sub_session.status == "error":
                        return f"Sub-agent {sub_id} failed. Check memory layer for details."
                    # Get the last assistant message
                    msgs = getattr(sub_session.adapter, "agent", None)
                    if msgs and hasattr(msgs, "messages"):
                        for m in reversed(msgs.messages):
                            if m.get("role") == "assistant":
                                for block in (m.get("content") or []):
                                    if "text" in block and block["text"].strip():
                                        memory_layer.log_event(_ws, sub_id, "sub_agent_complete", {
                                            "task": task[:200], "result_preview": block["text"][:200]
                                        })
                                        return f"Sub-agent {sub_id} completed:\n{block['text'][:5000]}"
                    memory_layer.log_event(_ws, sub_id, "sub_agent_complete", {"task": task[:200]})
                    return f"Sub-agent {sub_id} completed (no text output). Check memory layer."
                except Exception as e:
                    return f"Failed to spawn sub-agent: {e}"

            tools.append(delegate)
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

        # 3. Hooks — STRANDS-COMPLETE-FIX (Issue 4): real Strands SDK hooks.
        #    Strands exposes a typed hook system via `strands.hooks`:
        #      * `@hook_provider` decorator wraps a callable that takes a
        #        typed event (e.g. BeforeToolCallEvent) and returns it
        #        (possibly mutated). The Agent picks up the callback by
        #        inspecting the type annotation.
        #      * Multiple hooks compose into a list passed as Agent(hooks=[...]).
        #    We add:
        #      * BeforeToolCallEvent -> log the tool name + truncated input
        #        for debugging + cost transparency.
        #      * AfterToolCallEvent -> log the tool result size + status
        #        so we can see what each tool returned.
        #      * StartRequestEvent -> log the model invocation (so the
        #        backend log shows every LLM round-trip).
        #      * EndRequestEvent -> log the response (token usage/cost).
        #    Everything is best-effort and wrapped in try/except so a
        #    missing/incompatible Strands hooks API never blocks boot.
        agent_hooks: list = []
        try:
            from strands.hooks import hook_provider
            from strands.hooks.events import (
                StartRequestEvent,
                EndRequestEvent,
                BeforeToolCallEvent,
                AfterToolCallEvent,
            )

            @hook_provider
            def _hook_start_request(e: StartRequestEvent) -> StartRequestEvent:
                try:
                    log_event("strands_request_start",
                              model=self.resolved_model,
                              message_count=len(getattr(self.agent, "messages", []) or []))
                except Exception:
                    pass
                return e

            @hook_provider
            def _hook_end_request(e: EndRequestEvent) -> EndRequestEvent:
                try:
                    # e.usage / e.stop_reason are set by the LiteLLM model adapter
                    usage = getattr(e, "usage", None)
                    if usage:
                        # Strands Usage dataclass: input_tokens, output_tokens, total_tokens
                        log_event("strands_request_end",
                                  input=getattr(usage, "input_tokens", None),
                                  output=getattr(usage, "output_tokens", None),
                                  total=getattr(usage, "total_tokens", None),
                                  stop_reason=getattr(e, "stop_reason", None))
                    else:
                        log_event("strands_request_end",
                                  stop_reason=getattr(e, "stop_reason", None))
                except Exception:
                    pass
                return e

            @hook_provider
            def _hook_before_tool(e: BeforeToolCallEvent) -> BeforeToolCallEvent:
                try:
                    tool_name = getattr(e, "tool_name", "?") or "?"
                    # Truncate the input so we don't blow up the log on huge
                    # tool calls (e.g. file_write with a 100KB payload).
                    inp = getattr(e, "input", None) or {}
                    inp_summary = _summarize_tool_input(tool_name, inp) if isinstance(inp, dict) else str(inp)[:200]
                    log_event("strands_tool_call",
                              tool=tool_name, input_summary=inp_summary[:200])
                except Exception:
                    pass
                return e

            @hook_provider
            def _hook_after_tool(e: AfterToolCallEvent) -> AfterToolCallEvent:
                try:
                    tool_name = getattr(e, "tool_name", "?") or "?"
                    status = getattr(e, "status", None)
                    result = getattr(e, "result", None)
                    result_size = len(str(result)) if result is not None else 0
                    log_event("strands_tool_result",
                              tool=tool_name, status=status,
                              result_size=result_size)
                except Exception:
                    pass
                return e

            agent_hooks = [_hook_start_request, _hook_end_request,
                           _hook_before_tool, _hook_after_tool]
        except ImportError:
            # Older Strands without the typed hook API -- hooks stay empty.
            agent_hooks = []
        except Exception:
            # Any unexpected failure -> degrade gracefully.
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
        # STRANDS-COMPLETE-FIX (Issue 3): seed the agent's `messages` list
        # with the conversation history persisted to the SQLite chat_events
        # table. The HF Space free tier has NO persistent /data, so on every
        # restart:
        #   1. The in-memory AgentSession is gone (rebuilt from scratch).
        #   2. The Strands FileSessionManager's JSON file at
        #      /data/workspaces/<id>/.sessions/<sid>.json is GONE.
        # Without this seed, a freshly-booted agent would have an empty
        # messages list, so the second user message ("What tools do you
        # have?") would be sent with NO conversation history -- the model
        # Don't pre-populate messages from chat_events — it caused the
        # "same response repeated" bug. The Strands FileSessionManager handles
        # conversation persistence. If /data/ is ephemeral, conversations
        # restart fresh on Space restart (acceptable).
        self._msg_cursor = 0  # set in turn() after each turn
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
        # Track whether thinking / assistant text was emitted via the streaming
        # callback. If so, skip re-emitting it in the post-turn walk (which
        # would duplicate the text). Reset at the start of each turn.
        self._thinking_streamed = False
        # ISSUE-2 (RESPONSIVE-FIX): track assistant text streamed via deltas.
        # Strands' callback_handler receives `data` (str) for each content
        # token chunk (see strands/handlers/callback_handler.py — the
        # PrintingCallbackHandler streams `data` to stdout). We emit each
        # chunk as an `assistant_delta` event so the frontend can render
        # token-by-token streaming. The post-turn walk then SKIPS the full
        # `assistant` event because the streaming bubble already has the
        # complete text (finalized by the trailing `status: idle` event).
        self._assistant_streamed = False
        self._assistant_streamed_text = ""

        # Build a streaming callback handler: thinking text + content deltas
        # in real-time, plus mid-turn cost ceiling enforcement for conscious
        # agents.
        def _stream_callback(**kw):
            reasoning = kw.get("reasoningText")
            if reasoning:
                self._thinking_streamed = True
                emit({"type": "thinking", "text": reasoning})
            # ISSUE-2 (RESPONSIVE-FIX): capture content text deltas for
            # token-by-token streaming. Strands sends `data` (str) for each
            # text chunk and `complete` (bool) on the final chunk.
            data = kw.get("data")
            if data:
                self._assistant_streamed = True
                self._assistant_streamed_text = (self._assistant_streamed_text or "") + data
                emit({"type": "assistant_delta", "text": data})
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
        # Record the message count BEFORE the agent call so the post-turn
        # walk only emits NEW messages (not the entire history).
        self._pre_count = len(getattr(self.agent, "messages", []) or [])
        # Run the agent call with a thread + timeout. The GIL means we can't
        # hard-kill a blocking C extension call, but we CAN set a timeout and
        # process whatever messages were produced so far (best-effort).
        # After the timeout, we emit an error and move on — the daemon thread
        # continues in the background but doesn't block the user.
        import threading as _threading
        _agent_error: list = []
        _agent_done = {"done": False}
        _TIMEOUT_S = 90

        def _run_agent():
            _sess = getattr(self, "_session", None)
            _sid = getattr(_sess, "id", "?") if _sess else "?"
            try:
                log_event("agent_call_start", session_id=_sid, model=self.resolved_model)
                # Check what tools are loaded
                _tools = getattr(self.agent, 'tools', {})
                _tool_names = list(_tools.keys()) if isinstance(_tools, dict) else [getattr(t, 'tool_name', '?') for t in (_tools if isinstance(_tools, list) else [])]
                log_event("agent_tools", count=len(_tool_names), names=_tool_names[:10])
                resp = self.agent(user_msg)
                _agent_done["done"] = True
                log_event("agent_call_done", session_id=_sid, resp_type=type(resp).__name__)
            except Exception as e:
                log_event("agent_call_error", error=str(e)[:300], error_type=type(e).__name__)
                _agent_error.append(e)

        _t = _threading.Thread(target=_run_agent, daemon=True)
        _t.start()
        _t.join(timeout=_TIMEOUT_S)
        _sess = getattr(self, "_session", None)
        _sid = getattr(_sess, "id", "?") if _sess else "?"
        if not _agent_done["done"]:
            log_event("agent_call_timeout", session_id=_sid, timeout_s=_TIMEOUT_S)
            emit({"type": "error",
                  "error": f"model timed out ({_TIMEOUT_S}s) — try a different model"})
            # Process whatever messages were produced so far (best-effort)
        if _agent_error:
            # Don't raise — emit the error and continue processing messages
            emit({"type": "error", "error": str(_agent_error[0])[:200]})
        # Walk newly-appended messages for tool results and final assistant text
        msgs = getattr(self.agent, "messages", []) or []
        # ISSUE-3 (RESPONSIVE-FIX): Safety check — if the conversation manager
        # trimmed old messages (SlidingWindowConversationManager with
        # window_size=40), the cursor may point past the end of the list.
        # Reset to 0 so we walk all REMAINING messages. This prevents missing
        # assistant replies in long conversations. (We accept that some
        # already-emitted tool_use/tool_result events might re-emit — the
        # frontend dedupes by seq.)
        # Walk only NEW messages (those added by this turn's agent call).
        # self._pre_count was set before self.agent(user_msg) was called.
        _start = getattr(self, "_pre_count", 0)
        if _start > len(msgs):
            _start = max(0, len(msgs) - 5)  # SlidingWindow trimmed — walk last 5
        for m in msgs[_start:]:
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
                        # ISSUE-2 (RESPONSIVE-FIX): if the streaming callback
                        # already streamed the assistant text as deltas, the
                        # frontend's streaming bubble already has the full
                        # text (accumulated from assistant_delta events).
                        # Skip emitting a full `assistant` event because it
                        # would REPLACE the streaming content (causing visual
                        # flicker). The trailing `status: idle` event
                        # finalizes the streaming bubble.
                        #
                        # Safety: only skip if we actually streamed something.
                        # If `_assistant_streamed` is True but
                        # `_assistant_streamed_text` is empty (edge case where
                        # the callback was called with `data=""`), fall back
                        # to emitting the full text so the user always sees a
                        # reply.
                        if (getattr(self, "_assistant_streamed", False)
                                and getattr(self, "_assistant_streamed_text", "")):
                            continue
                        emit({"type": "assistant", "text": block["text"]})
        self._msg_cursor = len(msgs)
        self._pre_count = len(msgs)  # update for next turn

        # COST TRANSPARENCY: emit usage/cost info after each turn.
        # Strands tracks this on the agent's _loop_state.
        try:
            usage = getattr(self.agent, "_loop_state", {}).get("usage", None) if hasattr(self.agent, "_loop_state") else None
            if usage is None:
                # Try the agent's messages for usage metadata
                for m in reversed(msgs):
                    meta = m.get("metadata", {}) if isinstance(m, dict) else {}
                    if meta.get("usage"):
                        usage = meta["usage"]
                        break
            if usage:
                input_tokens = usage.get("inputTokens", 0) if isinstance(usage, dict) else getattr(usage, "inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0) if isinstance(usage, dict) else getattr(usage, "outputTokens", 0)
                total_tokens = usage.get("totalTokens", 0) if isinstance(usage, dict) else getattr(usage, "totalTokens", 0)
                cost = getattr(self.agent, "_loop_state", {}).get("total_cost_usd", None) if hasattr(self.agent, "_loop_state") else None
                emit({
                    "type": "status",
                    "state": "idle",
                    "detail": "",
                    "cost_usd": cost,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    },
                })
        except Exception:
            pass

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



# --------------------------------------------------------------------------
# Research adapter — drives research_templates + deep_research modes when the
# chat endpoint is called with webSearch=true / deepResearch=true. Bypasses
# the Claude/Strands SDKs entirely; uses the panel's scheduler + slots.
# --------------------------------------------------------------------------

class ResearchAdapter(BaseAdapter):
    """Runs a research template or deep-research mode in a private event loop.

    Picks a slot from the panel (explicit provider/model or the default panel),
    resolves reasoning params from reasoning_catalog.json, and dispatches to
    ``research_templates.run_template`` or ``run_deep_research``. Emits SSE
    events (status, sources, thinking, delta, done, error) through the same
    ``emit`` callback the other adapters use, so the existing polling/SSE
    infrastructure works unchanged.
    """

    def __init__(self, workspace: Path, model: str | None = None,
                 workspace_id: str | None = None, system_prompt: str | None = None,
                 panel=None, effort: str = "med",
                 web_search: bool = False, web_search_template: str | None = None,
                 deep_research: bool = False, deep_research_mode: str | None = None,
                 deep_research_template: str | None = None):
        super().__init__(workspace, workspace_id)
        self.model = model
        self.system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        self.panel = panel
        self.effort = effort or "med"
        self.web_search = web_search
        self.web_search_template = web_search_template
        self.deep_research = deep_research
        self.deep_research_mode = deep_research_mode
        self.deep_research_template = deep_research_template
        self.loop: asyncio.AbstractEventLoop | None = None
        # Resolved routing info (set by open()/turn() so snapshot() can surface it).
        self.resolved_model: str | None = None
        self.resolved_provider: str | None = None
        self.resolved_api_base: str | None = None

    def open(self) -> None:
        self.loop = asyncio.new_event_loop()
        # Best-effort: resolve the model up-front so snapshot() can show it
        # before the first turn. The actual slot pick happens in _turn().
        if self.panel is not None:
            try:
                picked = self._pick_slot_sync()
                if picked is not None:
                    self.resolved_model = picked.model
                    self.resolved_provider = picked.provider.name
                    self.resolved_api_base = picked.provider.url or None
            except Exception:
                pass

    def _pick_slot_sync(self):
        """Synchronous slot pick (best-effort) for the open() snapshot."""
        if self.panel is None:
            return None
        from content.roles import Roles
        pick_role = Roles("critiquer")
        candidates: list = []
        if self.model:
            _, candidates = self.panel.resolve_candidates(self.model)
        if not candidates and self.panel.default_panel:
            _, candidates = self.panel.resolve_candidates(self.panel.default_panel[0])
        if not candidates:
            return None
        picker = getattr(self.panel.scheduler, "pick_slot_from", None)
        if callable(picker):
            try:
                return picker(candidates, pick_role) or candidates[0]
            except Exception:
                return candidates[0]
        return candidates[0]

    def turn(self, user_msg: str, emit) -> None:
        assert self.loop is not None
        self.loop.run_until_complete(self._turn(user_msg, emit))

    async def _turn(self, user_msg: str, emit) -> None:
        if self.panel is None:
            emit({"type": "error", "error": "no panel available for research"})
            return
        picked, logical, extra_body = self._resolve_slot()
        if picked is None:
            emit({"type": "error",
                  "error": f"no slot available for model {self.model!r} (no configured provider hosts it)"})
            return
        self.resolved_model = picked.model
        self.resolved_provider = picked.provider.name
        self.resolved_api_base = picked.provider.url or None

        import httpx
        async with httpx.AsyncClient() as client:
            if self.deep_research:
                # If a template is also specified, run that template instead
                # of the mode (templates are richer; modes are simpler).
                if self.deep_research_template:
                    from research_templates import run_template
                    await run_template(template=self.deep_research_template,
                                       question=user_msg, client=client, picked=picked,
                                       extra_body=extra_body, emit=emit, effort=self.effort)
                else:
                    from research_templates import run_deep_research
                    await run_deep_research(mode=self.deep_research_mode or "default",
                                            question=user_msg, client=client, picked=picked,
                                            extra_body=extra_body, max_tokens=16384,
                                            timeout_s=1500.0, emit=emit, effort=self.effort)
            elif self.web_search and self.web_search_template:
                from research_templates import run_template
                await run_template(template=self.web_search_template,
                                   question=user_msg, client=client, picked=picked,
                                   extra_body=extra_body, emit=emit, effort=self.effort)
            else:
                emit({"type": "error",
                      "error": "research adapter created without a template or mode"})

    def _resolve_slot(self):
        """Pick a slot + resolve reasoning body. Returns (picked, logical, extra_body)."""
        from providers import resolve_reasoning_body
        from content.roles import Roles
        pick_role = Roles("critiquer")
        logical: str | None = None
        candidates: list = []
        if self.model:
            logical, candidates = self.panel.resolve_candidates(self.model)
        if not candidates and self.panel.default_panel:
            logical, candidates = self.panel.resolve_candidates(self.panel.default_panel[0])
        if not candidates:
            return None, None, None
        picker = getattr(self.panel.scheduler, "pick_slot_from", None)
        picked = None
        if callable(picker):
            try:
                picked = picker(candidates, pick_role)
            except Exception:
                picked = None
        if picked is None:
            picked = candidates[0]
        extra_body: dict | None = None
        if self.effort and self.effort != "low":
            rcat = getattr(self.panel, "reasoning_catalog", None) or {}
            try:
                extra_body = resolve_reasoning_body(
                    rcat, logical=logical, who=picked.who,
                    family=getattr(picked, "model_family", None)) or None
            except Exception:
                extra_body = None
        # PROGRAMMATIC SKILL INVOCATION: when web search or deep research
        # is requested AND this (provider, model) has a NATIVE web-search
        # tool (e.g. OpenRouter `plugins:[{id:"web"}]`), merge that body
        # in here. When NOT supported, the research template falls back
        # to our web_tools.py injection (Tavily/DuckDuckGo) — no change
        # needed in extra_body for the fallback path.
        if (self.web_search or self.deep_research):
            try:
                import provider_tools
                ws_body = provider_tools.native_web_search_body(
                    picked.provider.name, picked.model)
                if ws_body:
                    extra_body = dict(extra_body or {})
                    extra_body.update(ws_body)
                    try:
                        from oplog import log_event as _le
                        _le("agent_native_web_search",
                            provider=picked.provider.name, model=picked.model,
                            body_keys=list(ws_body.keys()))
                    except Exception:
                        pass
            except Exception:
                pass
        return picked, logical, extra_body

    def interrupt(self) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        try:
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
        except Exception:
            pass

    def close(self) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        try:
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            self.loop.run_until_complete(asyncio.sleep(0.05))
        except Exception:
            pass
        finally:
            try:
                self.loop.close()
            except Exception:
                pass


def _make_adapter(tier: str, workspace: Path, model: str | None = None,
                  workspace_id: str | None = None, system_prompt: str | None = None,
                  effort: str | None = None, panel=None,
                  web_search: bool = False) -> BaseAdapter:
    if tier == "claude":
        return ClaudeAdapter(workspace, model, workspace_id, system_prompt)
    if tier == "open":
        return StrandsAdapter(workspace, model, workspace_id, system_prompt,
                              effort=effort, panel=panel, web_search=web_search)
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
    # Accept any of the formats the frontend may send: the litellm model
    # string ("openai/glm-5.2"), the provider-qualified id
    # ("cloudflare/glm-5.2"), or the bare last segment ("glm-5.2").
    models = agent_models()
    if any(m.get("model") == model or m.get("provider_model") == model
           or m.get("model", "").split("/")[-1] == model.split("/")[-1]
           for m in models):
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
                 chat_session_id: str | None = None, mode: str = "auto",
                 panel=None, effort: str | None = None,
                 web_search: bool = False, web_search_template: str | None = None,
                 deep_research: bool = False, deep_research_mode: str | None = None,
                 deep_research_template: str | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.chat_session_id = chat_session_id
        self.persisted_seq = 0
        self.tier = tier
        self.model = model
        self.workspace_id = workspace_id  # links to user's workspace, if any
        self.mode = mode  # "auto" (default), "build", "plan"
        # Research mode params — when web_search or deep_research is True, the
        # session uses ResearchAdapter instead of the Claude/Strands SDK.
        self.panel = panel
        self.effort = effort
        self.web_search = web_search
        self.web_search_template = web_search_template
        self.deep_research = deep_research
        self.deep_research_mode = deep_research_mode
        self.deep_research_template = deep_research_template
        # Tier 3 — Conscious binding. Set when an agent is spawned against a
        # Conscious. Existing non-Conscious agents have both as None; the
        # conscious_* tool handlers no-op with an error in that case.
        self.conscious_id = conscious_id
        self.agent_id = agent_id
        # Build a context-rich system prompt when the agent is bound to a
        # cloned workspace, so the model knows it can run git commands.
        self.system_prompt = AGENT_SYSTEM_PROMPT
        # Mode-specific prompt additions
        if mode == "plan":
            self.system_prompt += (
                "\n\nMODE: PLAN. Do NOT execute or make changes. Instead, "
                "decompose the task into steps, outline the approach, and "
                "present the plan for user approval. Use the memory tool to "
                "save the plan. Wait for user confirmation before executing."
            )
        elif mode == "build":
            self.system_prompt += (
                "\n\nMODE: BUILD. Execute the task step by step. After each "
                "step, report the outcome and wait for user confirmation "
                "before proceeding to the next step."
            )
        else:  # auto
            self.system_prompt += (
                "\n\nMODE: AUTO. Execute the full task autonomously. Make "
                "decisions independently, use tools freely, and report the "
                "final outcome. Only pause if you encounter an error you "
                "can't resolve or if you need user input."
            )
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
        # BUGFIX (double-response on model switch, Bug 2): a brand-new
        # AgentSession ALWAYS starts with an empty event buffer and seq
        # numbering from 0. The seq is derived from len(self.events) in
        # emit() (new_ev = {"i": len(self.events), ...}), so a fresh
        # session's first event is always i=0. When the user switches
        # models mid-conversation, get_or_create() closes the old session
        # and creates a new one here -- the new session's events start
        # from i=0, independent of the old session's seq numbering. The
        # frontend MUST reset its _lastEventSeq cursor when it detects a
        # new session_id (server returns a different session_id from
        # POST /api/agent); otherwise it would poll the new stream with
        # a stale since=<old_max_seq> and miss all events. The backend's
        # subscribe() also guards against this by resetting an out-of-
        # range cursor to 0 (see subscribe()).
        self.events: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self._stream_queues: list[queue.Queue] = []
        # Task 7 — smart queue injection. ``_queued_msgs`` holds messages
        # the user submitted while a turn was already running. They are
        # injected at natural breakpoints (after a thinking chain ends,
        # before/after a tool call, on paragraph breaks in assistant text)
        # so the agent gets the new context without interrupting coherent
        # thought. ``_last_event_type`` tracks the most recent event type
        # so we can detect breakpoint transitions.
        self._queued_msgs: list[str] = []
        self._queued_msgs_lock = threading.Lock()
        self._last_event_type: str | None = None
        self._last_assistant_text: str = ""
        # Soft-inject cap so a runaway submitter can't OOM the session.
        self._queued_msgs_max = 16
        self.adapter: BaseAdapter | None = None
        self._interrupting = False
        # Bug 3: auto-title generation. Set to True after we've generated
        # (or attempted to generate) a title for this session's chat
        # session, so we only do it once (on the first completed turn).
        self._title_generated = False
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
                # FIX-ISSUE-5 (FIX-CHAT-BROKEN): Thinking dedup.
                #
                # The Strands callback_handler emits `reasoningText` as the
                # ACCUMULATED string each time (NOT a delta). So as the model
                # reasons, we get a sequence like:
                #   emit("The user said Hey")
                #   emit("The user said Hey - this is a casual greeting")
                #   emit("The user said Hey - this is a casual greeting. I should...")
                # Each call's text is a PREFIX-EXTENSION of the previous.
                #
                # Previously this code only skipped when new_text was a prefix
                # of old_text (stale re-emit). It did NOT detect the reverse
                # case (new extends old) — and fell through to the else branch
                # which APPENDS. The result was the accumulated text got
                # concatenated with itself on every token, producing the
                # runaway duplication seen in the transcript:
                #   "The user said Hey - this is a casual greeting. I should
                #    respond brieflyThe user said Hey - this is a simple
                #    greeting.The user said Hey - this is a simple greeting.
                #    I should respond brieflyThe user said Hey - ..."
                #
                # The fix below handles three cases:
                #  1. new_text starts with old_text → REPLACE (new is the
                #     accumulated version; emit only the full new text so the
                #     frontend replaces its bubble content).
                #  2. old_text starts with new_text (and new is shorter) →
                #     SKIP (stale re-emit of an earlier prefix; keep old).
                #  3. No prefix relationship → APPEND (genuine fragment, e.g.
                #     a new reasoning chunk after a tool call).
                old_text = (self.events[-1].get("text", "") or "")
                new_text = (ev.get("text", "") or "")
                if old_text and new_text and new_text.startswith(old_text) and len(new_text) > len(old_text):
                    # Case 1: new is the accumulated version — REPLACE.
                    self.events[-1]["text"] = new_text
                    self.events[-1]["ts"] = time.time()
                    stream_ev = {"i": self.events[-1]["i"], "ts": self.events[-1]["ts"], **ev}
                    stream_ev["text"] = self.events[-1]["text"]
                elif old_text and new_text and old_text.startswith(new_text) and len(new_text) < len(old_text):
                    # Case 2: new is a stale prefix of old — SKIP (keep old).
                    stream_ev = {"i": self.events[-1]["i"], "ts": self.events[-1]["ts"], **ev}
                    stream_ev["text"] = old_text
                elif old_text and new_text and new_text == old_text:
                    # Case 2b: identical — SKIP (no-op).
                    stream_ev = {"i": self.events[-1]["i"], "ts": self.events[-1]["ts"], **ev}
                    stream_ev["text"] = old_text
                else:
                    # Case 3: no prefix relationship — APPEND.
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
            except queue.Full:
                # Queue full -> the SSE client is too slow. Drop THIS event
                # but keep the queue registered so future events still flow.
                # Previously we removed the queue on overflow, which silently
                # disconnected slow clients mid-turn (they'd see "only my
                # message, no reply" because the assistant event was the one
                # that overflowed). The client can recover by reconnecting
                # with since=<max_seen> to get a fresh replay.
                pass
            except ValueError:
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

        # Task 7 — smart queue injection at natural breakpoints. Detect:
        #   - thinking_end: prior event was thinking, new event is NOT
        #     thinking (reasoning chain ended).
        #   - tool_use_start: new event is tool_use (about to call a tool).
        #   - tool_result_end: prior event was tool_result (tool finished).
        #   - paragraph_break: new assistant text contains a double-newline
        #     AND we already had some assistant text this turn (mid-thought
        #     paragraph break, not the start of the message).
        # We do NOT inject mid-token (only on event boundaries), so the
        # agent's coherent thought is preserved. The injection is SOFT: the
        # queued message goes to the inbox for the NEXT turn (the current
        # turn continues to completion).
        try:
            new_type = ev.get("type")
            if new_type:
                prev_type = self._last_event_type
                inject_kind: str | None = None
                if (prev_type == "thinking"
                        and new_type not in ("thinking", "thinking_delta")):
                    inject_kind = "thinking_end"
                elif new_type == "tool_use":
                    inject_kind = "tool_use_start"
                elif prev_type == "tool_result" and new_type != "tool_result":
                    inject_kind = "tool_result_end"
                elif new_type == "assistant":
                    new_text = ev.get("text") or ""
                    # Paragraph break = a double newline AFTER the first
                    # paragraph (so we don't inject on the very first
                    # assistant chunk of a turn).
                    if (self._last_assistant_text
                            and "\n\n" in new_text
                            and "\n\n" not in self._last_assistant_text):
                        inject_kind = "paragraph_break"
                    self._last_assistant_text = new_text
                elif new_type in ("status",) and ev.get("state") in ("idle", "done"):
                    inject_kind = "status_idle"
                # Track the new type as the prev for the next emit().
                if new_type not in ("thinking_delta", "assistant_delta"):
                    self._last_event_type = new_type
                if inject_kind:
                    self._inject_queued_at_breakpoint(inject_kind)
        except Exception:
            pass  # never let queue logic break the turn

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
        that receives every new event as it's emitted. Events with i >= since
        are replayed first (so a late subscriber catches up), then the queue
        stays open for live events. Call unsubscribe(q) when done.

        This is the method the SSE endpoint looks for via getattr(session,
        'subscribe', None). Without it, the SSE handler falls back to polling
        (200ms snapshots) — which is why streaming felt choppy/non-live.

        BUGFIX (first-message-no-response): the previous implementation
        snapshotted self.events FIRST, then registered the queue. That left
        a race window: an event emitted between the snapshot and the
        registration would be in NEITHER the snapshot (already taken) NOR
        the queue (not yet registered) — lost forever. On a fresh session
        where the agent emits status/user/assistant events in quick
        succession before the SSE client connects, this could drop the
        assistant event so the user saw "only my message, no reply".

        Fix: register the queue AND snapshot self.events under the SAME
        lock. emit() also holds this lock when appending to self.events
        and snapshotting _stream_queues, so the two operations are atomic
        with respect to emit():
          - emit() that ran BEFORE we acquired the lock: its event is in
            self.events (so it's in our replay) AND it didn't push to our
            queue (we hadn't registered yet) -> exactly one copy in the q.
          - emit() that runs AFTER we release the lock: its event is NOT
            in our replay (snapshot already taken) AND it pushes to our
            queue (we registered it) -> exactly one copy in the q.
        No duplicates, no missing events."""
        q: queue.Queue = queue.Queue(maxsize=512)
        with self.lock:
            # Register the queue FIRST (under the lock) so emit() can't
            # miss it, then snapshot the events for replay.
            self._stream_queues.append(q)
            # If the client's cursor is AHEAD of our buffer (e.g., a stale
            # cursor carried over from a previous agent session after a
            # model switch), reset to 0 so we replay the full transcript
            # from the start instead of returning an empty stream that
            # leaves the user with no assistant reply.
            max_seq = self.events[-1].get("i", -1) if self.events else -1
            effective_since = since if since <= max_seq else 0
            replay = [e for e in self.events if (e.get("i", 0) >= effective_since)]
        for ev in replay:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass  # drop backlog if subscriber is slow
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

    def _drain_queued_message(self) -> str | None:
        """Pop the next queued message (FIFO) if any. Returns None if the
        queue is empty. Called at natural breakpoints during a turn."""
        with self._queued_msgs_lock:
            if not self._queued_msgs:
                return None
            return self._queued_msgs.pop(0)

    def _inject_queued_at_breakpoint(self, breakpoint_kind: str) -> bool:
        """If there's a queued message, inject it as a new user message for
        the current turn. Returns True iff a message was injected.

        ``breakpoint_kind`` is one of:
          - 'thinking_end'   — a reasoning chain just ended
          - 'tool_use_start' — about to execute a tool
          - 'tool_result_end' — a tool finished
          - 'paragraph_break' — assistant emitted a double-newline
          - 'status_idle'    — agent went idle (turn boundary)

        The injection is SOFT: we don't kill the current turn. The message
        is fed to the adapter as additional context via the inbox; on the
        next ``_run`` iteration the adapter picks it up as a follow-up
        user message and continues with the new context.
        """
        msg = self._drain_queued_message()
        if not msg:
            return False
        try:
            self.emit({"type": "injected", "text": msg[:200],
                       "breakpoint": breakpoint_kind,
                       "remaining_queued": len(self._queued_msgs)})
        except Exception:
            pass
        # Push to the inbox so the next ``_run`` iteration processes it
        # as a new turn. The current turn continues to completion; the
        # injected message is the NEXT turn's input.
        self.inbox.put(msg)
        return True

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
        """Submit a message. If a turn is running, the message is added to
        the smart queue and injected at the next natural breakpoint
        (Task 7). Otherwise it goes straight to the inbox for the next turn.
        """
        self.updated = time.time()
        # If a turn is currently running, queue the message for smart
        # injection at the next breakpoint. Otherwise push to the inbox
        # so the next ``_run`` iteration picks it up immediately.
        if self.status == "running":
            with self._queued_msgs_lock:
                if len(self._queued_msgs) < self._queued_msgs_max:
                    self._queued_msgs.append(message)
                    # Emit a 'queued' event so the frontend can show
                    # "message queued, will be sent at the next breakpoint".
                    try:
                        self.emit({"type": "queued",
                                   "text": message[:200],
                                   "queue_position": len(self._queued_msgs)})
                    except Exception:
                        pass
                    return
        # Not running (or queue full): push to the inbox for immediate
        # processing on the next turn.
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
        log_event("agent_run_start", session_id=self.id, tier=self.tier, model=self.model)
        if self.web_search or self.deep_research:
            # Research mode: bypass the Claude/Strands SDKs and drive
            # research_templates directly from the panel's slots.
            adapter = ResearchAdapter(
                self.workspace, model=self.model, workspace_id=self.workspace_id,
                system_prompt=self.system_prompt, panel=self.panel, effort=self.effort,
                web_search=self.web_search, web_search_template=self.web_search_template,
                deep_research=self.deep_research, deep_research_mode=self.deep_research_mode,
                deep_research_template=self.deep_research_template)
        else:
            adapter = _make_adapter(self.tier, self.workspace, self.model,
                                    self.workspace_id, self.system_prompt,
                                    effort=self.effort, panel=self.panel,
                                    web_search=self.web_search)
        self.adapter = adapter
        # Tier 3 — give the adapter a back-reference so it can register the
        # conscious tool registry on the owning session (conscious_id/agent_id).
        adapter._session = self
        try:
            log_event("agent_adapter_open_start", session_id=self.id)
            adapter.open()
            log_event("agent_adapter_open_done", session_id=self.id)
        except Exception as exc:
            # Bug 2 (fresh-session no-response): emit BOTH a status="error"
            # event AND a typed "error" event so the frontend definitely
            # surfaces the failure (some clients only listen for the typed
            # event). Without this, an init failure (e.g. missing provider
            # key for the user's selected model) silently ends the turn and
            # the user sees "I sent Hi but got no reply".
            err_detail = f"agent init failed: {_clip(str(exc), 300)}"
            self._set_status("error", detail=err_detail)
            self.emit({"type": "error", "error": err_detail})
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
            # Task 7 — reset the breakpoint tracker for the new turn.
            self._last_event_type = None
            self._last_assistant_text = ""
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
            # Track whether the adapter emitted any assistant text this turn.
            # If it didn't (e.g. the LLM returned only tool calls without a
            # final message, or returned an empty response), we emit a
            # placeholder assistant event so the frontend doesn't sit waiting
            # for a reply that will never come (Bug 2: "Hi → no reply").
            assistant_emitted = False
            pre_count = len(self.events)
            try:
                log_event("agent_turn_start", session_id=self.id, msg_preview=msg[:50])
                adapter.turn(msg, self.emit)
                log_event("agent_turn_done", session_id=self.id)
                if self._interrupting:
                    self._set_status("idle", detail="interrupted")
                else:
                    self._set_status("idle")
            except Exception as exc:
                if self._interrupting:
                    self._set_status("idle", detail="interrupted")
                else:
                    err_detail = _clip(str(exc), 300)
                    # Bug 2: emit a typed "error" event too so the frontend
                    # always shows feedback — never silently fail.
                    self._set_status("error", detail=err_detail)
                    self.emit({"type": "error", "error": err_detail})
            finally:
                self._interrupting = False
                # FIX-ISSUE-4 (FIX-CHAT-BROKEN): Check whether any assistant
                # event was emitted during the turn (events appended after
                # pre_count). If NEITHER a full `assistant` event NOR any
                # `assistant_delta` event was emitted, emit a placeholder so
                # the user sees SOMETHING.
                #
                # Previously this check only looked for type == "assistant".
                # But the streaming callback emits `assistant_delta` events
                # (token-by-token) and the post-turn walk SKIPS the full
                # `assistant` event when streaming captured the text (see
                # _assistant_streamed / _assistant_streamed_text guards).
                # So a turn that produced a complete streamed reply had ZERO
                # `assistant` events — the placeholder fired after EVERY real
                # streamed response, producing the duplicate:
                #   ## Assistant
                #   Hey! How can I help you today?
                #   ## Assistant
                #   (no response from the model — check that the provider
                #    key is valid and the model name is correct)
                # The fix: count `assistant_delta` events as well so the
                # placeholder ONLY fires when the model genuinely produced no
                # output (empty response, init error, provider 400, etc.).
                if not self._interrupting:
                    with self.lock:
                        for ev in self.events[pre_count:]:
                            if ev.get("type") in ("assistant", "assistant_delta"):
                                assistant_emitted = True
                                break
                    if not assistant_emitted:
                        self.emit({"type": "assistant",
                                   "text": "(no response from the model — "
                                           "check that the provider key is "
                                           "valid and the model name is correct)"})
                # Bug 3: auto-generate a title for the chat session on the
                # first completed turn. The chat session is created with
                # title="New Chat" by _handle_agent_post; we replace it
                # with a 3-6 word title derived from the user's first
                # message via a lightweight LLM call (falling back to a
                # plain truncation if no panel/LLM is available). Emits a
                # `title` event so the frontend sidebar updates live.
                if (not self._title_generated and self.chat_session_id
                        and msg and msg.strip()):
                    self._title_generated = True
                    try:
                        self._maybe_auto_title(msg.strip())
                    except Exception:
                        pass  # best-effort -- never block the turn loop
        adapter.close()

    def _maybe_auto_title(self, first_user_msg: str) -> None:
        """Generate a 3-6 word title for the chat session from the user's
        first message and emit a `title` event. Best-effort: falls back to
        a plain truncation if no panel/LLM is available or the LLM call
        fails. Idempotent (caller guards with self._title_generated)."""
        import chat_routes
        # Skip if the session already has a non-default title (e.g., the
        # user set one manually before sending the first message).
        cs = chat_routes.get_chat_session(self.chat_session_id)
        if not cs:
            return
        existing_title = (cs.get("title") or "").strip()
        if existing_title and existing_title != "New Chat":
            return

        title: str | None = None
        # Try a lightweight LLM call via the panel (3-6 word title).
        if self.panel is not None:
            try:
                title = self._llm_title(first_user_msg)
            except Exception:
                title = None
        # Fallback: plain truncation (same as chat_routes._auto_title).
        if not title or not title.strip():
            title = chat_routes._auto_title(first_user_msg, max_len=48)
        title = title.strip()[:80] or "New Chat"

        chat_routes.update_chat_session(self.chat_session_id, title=title)
        # Emit a title event so the frontend can update its sidebar
        # immediately without re-fetching the session list.
        self.emit({"type": "title", "title": title,
                   "chat_session_id": self.chat_session_id})

    def _llm_title(self, user_msg: str) -> str | None:
        """Use the panel to generate a 3-6 word title via a single
        lightweight LLM call. Returns None on any failure (caller falls
        back to truncation). Runs in a private event loop so it works
        from the synchronous _run() worker thread."""
        import asyncio
        import httpx
        from content.roles import Roles
        from scheduler import call_slot, ProviderError

        # Pick a slot: prefer the session's model, else the panel's
        # default panel, else the first available candidate.
        pick_role = Roles("critiquer")
        logical: str | None = None
        candidates: list = []
        if self.model:
            logical, candidates = self.panel.resolve_candidates(self.model)
        if not candidates and self.panel.default_panel:
            logical, candidates = self.panel.resolve_candidates(
                self.panel.default_panel[0])
        if not candidates:
            return None
        picker = getattr(self.panel.scheduler, "pick_slot_from", None)
        picked = None
        if callable(picker):
            try:
                picked = picker(candidates, pick_role)
            except Exception:
                picked = None
        if picked is None:
            picked = candidates[0]

        # Keep the title-gen prompt tiny so it's cheap and fast.
        sys_prompt = (
            "You generate a short chat title. Read the user's first message "
            "and reply with ONLY a 3-6 word title (no quotes, no punctuation "
            "at the end, no prefix like 'Title:'). Plain text, no markdown."
        )
        # Truncate the user message to keep the prompt small.
        user_excerpt = user_msg[:500]
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_excerpt},
        ]

        async def _call() -> str | None:
            async with httpx.AsyncClient() as client:
                try:
                    content, _usage = await call_slot(
                        client, picked, messages,
                        max_tokens=32, timeout_s=20.0)
                except ProviderError:
                    return None
                except Exception:
                    return None
            return content

        loop = asyncio.new_event_loop()
        try:
            content = loop.run_until_complete(_call())
        finally:
            try:
                loop.close()
            except Exception:
                pass
        if not content:
            return None
        # Clean up: strip quotes, newlines, "Title:" prefix, trailing period.
        content = content.strip()
        for prefix in ("Title:", "title:", "TITLE:"):
            if content.startswith(prefix):
                content = content[len(prefix):].strip()
        content = content.strip('"').strip("'").strip()
        content = content.split(chr(10))[0].strip()
        if content.endswith("."):
            content = content[:-1].strip()
        # Collapse whitespace.
        content = " ".join(content.split())
        # Sanity-check length: if the LLM returned something absurdly long
        # or empty, signal failure so the caller falls back.
        if not content or len(content) > 80:
            return None
        return content


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
                  chat_session_id: str | None = None,
                  mode: str = "auto",
                  panel=None, effort: str | None = None,
                  web_search: bool = False, web_search_template: str | None = None,
                  deep_research: bool = False, deep_research_mode: str | None = None,
                  deep_research_template: str | None = None) -> AgentSession:
    """Reuse a live session by id, or start a new one (CapacityError if full).
    `model` (optional) selects which model/tier drives a NEW session.
    `workspace_id` (optional) links the session to a user workspace sandbox.
    `conscious_id` + `agent_id` (optional, Tier 3) bind the session to a
    Conscious agent row so the conscious_* tools resolve context.
    `chat_session_id` (optional) links this agent session to a persistent
    chat session for event persistence.
    """
    # Research mode bypasses the SDK tier check — it uses the panel's slots
    # directly, so it works even when no Claude/Strands SDK is installed.
    if web_search or deep_research:
        tier = "research"
    else:
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
        # If the pool is full, try to evict the oldest IDLE session (not running)
        # before raising CapacityError. This prevents the "max 8 sessions" error
        # when the user has been switching between multiple chats.
        if len(_sessions) >= MAX_SESSIONS:
            # Find the oldest idle session (not running, not starting)
            oldest_idle = None
            for s in _sessions.values():
                if s.status not in ("running", "starting") and not s.closed:
                    if oldest_idle is None or s.updated < oldest_idle.updated:
                        oldest_idle = s
            if oldest_idle is not None:
                oldest_idle.close()
                _sessions.pop(oldest_idle.id, None)
            else:
                raise CapacityError(f"max {MAX_SESSIONS} concurrent agent sessions (all running)")
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
                         chat_session_id=chat_session_id, mode=mode,
                         panel=panel, effort=effort,
                         web_search=web_search, web_search_template=web_search_template,
                         deep_research=deep_research, deep_research_mode=deep_research_mode,
                         deep_research_template=deep_research_template)
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
