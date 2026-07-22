"""Provider API key vault — POST/GET/DELETE /api/keys.

Endpoints:
  * POST   /api/keys              — store (or update) a provider API key
                                    {provider, key, extra?} -> {ok, provider}
  * GET    /api/keys              — list which providers have keys set
                                    (returns {provider: {env_var, has_key, has_extra}},
                                     NEVER the key values themselves)
  * DELETE /api/keys/<provider>   — delete a provider key

Storage strategy (the user's spec):
  1. Encrypt the key with AES-256-GCM (crypto.encrypt_secret) and store in
     the per-user ``provider_keys`` SQLite table (durable backup + index).
  2. ALSO set the key as a Space secret via
     ``huggingface_hub.HfApi.add_space_secret`` so the backend picks it up
     as an env var on the next restart. The Space secret is the runtime
     source-of-truth — the DB row is the durable backup so a user can
     re-add the key after a Space wipe without re-pasting it.

Auth: same bearer + JWT gate as /api/agent. Identity (user_id) is read
from the X-JWT header. Anonymous callers get 401.

The allowlist of allowed env var names lives in db.PROVIDER_KEY_ALLOWLIST
(SECURITY: a user CANNOT set arbitrary env vars on their Space — only the
provider API key env vars explicitly listed there).
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import db as _dbmod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_id_from_handler(handler) -> str | None:
    """Extract GitHub user_id from the request handler (X-JWT or fallback)."""
    try:
        return handler._require_user_from_jwt()
    except Exception:
        return None


def _json(handler, status: int, body: dict) -> None:
    handler._send_json(status, body)


# ---------------------------------------------------------------------------
# HF Space secret mirror (best-effort)
# ---------------------------------------------------------------------------

def _space_repo_id() -> str | None:
    """Return the HF Space repo id (``owner/space-name``) we're running on,
    or None if not on a HF Space. The HF runtime sets SPACE_AUTHOR_NAME +
    SPACE_REPO_NAME for official spaces; we fall back to building it from
    SPACE_ID (which has the form ``owner/name``)."""
    sid = os.environ.get("SPACE_ID", "").strip()
    if sid and "/" in sid:
        return sid
    author = os.environ.get("SPACE_AUTHOR_NAME", "").strip()
    name = os.environ.get("SPACE_REPO_NAME", "").strip()
    if author and name:
        return f"{author}/{name}"
    return None


def _set_space_secret(env_var: str, value: str) -> tuple[bool, str]:
    """Set a Space secret via the HF API. Returns (ok, error_or_empty).

    Best-effort: silently returns (False, error) if HF_TOKEN is missing
    or the HF API is unreachable. The DB row is still written by the
    caller, so the key is durable — it just won't be live as an env var
    until the user restarts the Space (the Space secret will be set on
    the next successful call).
    """
    repo = _space_repo_id()
    if not repo:
        return (False, "not running on a HF Space (SPACE_ID unset)")
    token = (os.environ.get("HF_TOKEN", "")
             or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()
    if not token:
        return (False, "HF_TOKEN not set")
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        api.add_space_secret(repo_id=repo, key=env_var, value=value)
        return (True, "")
    except ImportError:
        return (False, "huggingface_hub not installed")
    except Exception as exc:
        return (False, f"HF API error: {type(exc).__name__}: {exc}"[:200])


def _delete_space_secret(env_var: str) -> tuple[bool, str]:
    """Delete a Space secret via the HF API. Best-effort."""
    repo = _space_repo_id()
    if not repo:
        return (False, "not running on a HF Space")
    token = (os.environ.get("HF_TOKEN", "")
             or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()
    if not token:
        return (False, "HF_TOKEN not set")
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        # The HF API method is `delete_space_secret` (newer hub versions) —
        # fall back to the underlying POST /api/spaces/{repo}/secrets/delete
        # if the method is missing (older hub versions).
        fn = getattr(api, "delete_space_secret", None)
        if callable(fn):
            fn(repo_id=repo, key=env_var)
            return (True, "")
        # Manual fallback: direct HTTP.
        import urllib.request
        import json
        body = json.dumps({"key": env_var}).encode("utf-8")
        req = urllib.request.Request(
            f"https://huggingface.co/api/spaces/{repo}/secrets/delete",
            data=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return (True, "")
    except ImportError:
        return (False, "huggingface_hub not installed")
    except Exception as exc:
        return (False, f"HF API error: {type(exc).__name__}: {exc}"[:200])


# ---------------------------------------------------------------------------
# HTTP dispatch
# ---------------------------------------------------------------------------

def handle_request(method: str, path: str, body: dict, handler) -> bool:
    """Dispatch /api/keys* routes. Returns True if handled."""
    route = urlsplit(path).path.rstrip("/")
    # Only handle /api/keys* routes
    if route != "/api/keys" and not route.startswith("/api/keys/"):
        return False
    # Auth: same bearer gate as /api/agent. Identity required (no anon keys).
    if not handler._auth_ok():
        _json(handler, 401, {"error": "missing or invalid bearer token"})
        return True
    user_id = _user_id_from_handler(handler)
    if not user_id:
        _json(handler, 401, {"error": "sign-in required to manage API keys"})
        return True

    # -----------------------------------------------------------------
    # GET /api/keys — list which providers the user has keys for
    # -----------------------------------------------------------------
    if method == "GET" and route == "/api/keys":
        keys = _dbmod.list_provider_keys(user_id)
        # Also include the env-var presence as a cross-check (a key may be
        # set as a Space secret but not yet in the DB, e.g. set via the HF
        # UI directly). We never read the value — only check presence.
        env_present: dict[str, bool] = {}
        for prov, env_var in _dbmod.PROVIDER_KEY_ALLOWLIST.items():
            env_present[prov] = bool(os.environ.get(env_var, "").strip())
        _json(handler, 200, {
            "providers": keys,
            "env_present": env_present,
            "allowlist": _dbmod.PROVIDER_KEY_ALLOWLIST,
        })
        return True

    # -----------------------------------------------------------------
    # POST /api/keys — store (or update) a provider key
    # -----------------------------------------------------------------
    if method == "POST" and route == "/api/keys":
        provider = str(body.get("provider", "")).strip().lower()
        key = str(body.get("key", "")).strip()
        extra = body.get("extra")
        if not provider:
            _json(handler, 400, {"error": "'provider' is required"})
            return True
        if not key:
            _json(handler, 400, {"error": "'key' is required"})
            return True
        p = _dbmod.resolve_provider(provider)
        if not p:
            _json(handler, 400, {
                "error": f"unknown provider {provider!r}",
                "allowlist": list(_dbmod.PROVIDER_KEY_ALLOWLIST.keys()),
            })
            return True
        env_var = _dbmod.PROVIDER_KEY_ALLOWLIST[p]
        # Validate `extra` is a dict if present.
        if extra is not None and not isinstance(extra, dict):
            _json(handler, 400, {"error": "'extra' must be an object"})
            return True
        # 1) Encrypt + store in DB (durable backup + index).
        try:
            _dbmod.set_provider_key(
                user_id=user_id, provider=p, key=key, extra=extra)
        except Exception as exc:
            _json(handler, 500, {"error": f"failed to store key: {type(exc).__name__}"})
            return True
        # 2) Mirror to Space secret (best-effort — silent on failure so a
        # missing HF_TOKEN doesn't break the local-only flow).
        secret_ok, secret_err = _set_space_secret(env_var, key)
        extra_secret_results: dict[str, Any] = {}
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    if k in ("CF_ACCOUNT_ID",):
                        ok, err = _set_space_secret(k, v.strip())
                        extra_secret_results[k] = {"ok": ok, "error": err}
        _json(handler, 200, {
            "ok": True,
            "provider": p,
            "env_var": env_var,
            "stored_locally": True,
            "mirrored_to_space": secret_ok,
            "space_secret_error": secret_err if not secret_ok else None,
            "extra": extra_secret_results,
        })
        return True

    # -----------------------------------------------------------------
    # DELETE /api/keys/<provider> — delete a provider key
    # -----------------------------------------------------------------
    if method == "DELETE" and route.startswith("/api/keys/"):
        provider = route[len("/api/keys/"):].strip().lower()
        if not provider:
            _json(handler, 400, {"error": "provider is required"})
            return True
        p = _dbmod.resolve_provider(provider)
        if not p:
            _json(handler, 404, {"error": f"unknown provider {provider!r}"})
            return True
        env_var = _dbmod.PROVIDER_KEY_ALLOWLIST[p]
        deleted = _dbmod.delete_provider_key(user_id, p)
        # Best-effort delete of the Space secret too.
        secret_ok, secret_err = _delete_space_secret(env_var)
        # If the provider had `extra` env vars (e.g. CF_ACCOUNT_ID for
        # cloudflare), also delete those.
        extra_secret_results: dict[str, Any] = {}
        if p == "cloudflare":
            ok, err = _delete_space_secret("CF_ACCOUNT_ID")
            extra_secret_results["CF_ACCOUNT_ID"] = {"ok": ok, "error": err}
        _json(handler, 200, {
            "ok": True,
            "provider": p,
            "deleted_from_db": deleted,
            "deleted_from_space": secret_ok,
            "space_secret_error": secret_err if not secret_ok else None,
            "extra": extra_secret_results,
        })
        return True

    _json(handler, 405, {"error": f"method {method} not allowed on {route}"})
    return True
