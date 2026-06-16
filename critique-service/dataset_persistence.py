"""HF Dataset persistent storage + OAuth for the loom critique service.

Persists the SQLite DB to a private HF Dataset so data survives Space rebuilds.
Also provides HF OAuth helpers so users can authorize dataset access.

Env vars required:
    HF_CLIENT_ID       — HF OAuth App client ID
    HF_CLIENT_SECRET   — HF OAuth App client secret
    ENCRYPTION_KEY     — Fernet key for token encryption (auto-generated if absent)

Env vars optional:
    LOOM_DATASET_SUFFIX  — suffix for the dataset name (default: main)
                           The full name is {hf_username}/loom-priv-{suffix}
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import crypto
import db
import jwt_auth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HF_AUTHORIZE_URL = "https://huggingface.co/oauth/authorize"
HF_TOKEN_URL = "https://huggingface.co/oauth/token"
HF_API_BASE = "https://huggingface.co"
HF_SCOPES = "openid profile write-repos"

# ---------------------------------------------------------------------------
# HF API helpers
# ---------------------------------------------------------------------------

def _hf_api(path: str, *, token: str = "", method: str = "GET",
             body: dict | None = None) -> dict:
    """Raw HF Hub API call via stdlib urllib."""
    url = f"{HF_API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HF API {path} failed (HTTP {exc.code}): {detail}") from exc


def _hf_configured() -> bool:
    return bool(
        os.environ.get("HF_CLIENT_ID", "").strip()
        and os.environ.get("HF_CLIENT_SECRET", "").strip()
    )


# ---------------------------------------------------------------------------
# Space name detection
# ---------------------------------------------------------------------------

def _space_name() -> str:
    """Return the Space repo name for dataset naming."""
    name = os.environ.get("SPACE_NAME", "").strip()
    if name:
        return name
    sid = os.environ.get("SPACE_ID", "").strip()
    if sid and "/" in sid:
        return sid.split("/")[-1]
    return "main"


# ---------------------------------------------------------------------------
# HF OAuth flow
# ---------------------------------------------------------------------------

def make_hf_authorize_url(state: str, redirect_uri: str = "") -> str:
    """Build the HF OAuth authorize URL with state token.

    ``redirect_uri`` must match the registered callback URL exactly.
    """
    params = urlencode({
        "client_id": os.environ.get("HF_CLIENT_ID", ""),
        "redirect_uri": redirect_uri,
        "scope": HF_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return f"{HF_AUTHORIZE_URL}?{params}"


def exchange_hf_code(code: str, redirect_uri: str = "") -> dict:
    """Exchange OAuth code for tokens. Returns dict with access_token, refresh_token, expires_in.

    ``redirect_uri`` must match the value used in the authorize request.
    Raises RuntimeError on failure.
    """
    body = urlencode({
        "client_id": os.environ.get("HF_CLIENT_ID", ""),
        "client_secret": os.environ.get("HF_CLIENT_SECRET", ""),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(HF_TOKEN_URL, data=body, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HF token exchange failed (HTTP {exc.code}): {detail}") from exc
    token = data.get("access_token", "").strip()
    if not token:
        raise RuntimeError(f"HF token exchange returned no access_token: {data}")
    return data


def get_hf_user(token: str) -> dict:
    """Fetch authenticated user info from HF API."""
    return _hf_api("/api/whoami-v2", token=token)


def upsert_user_from_hf(token_data: dict, user_id: str | None = None) -> dict:
    """Fetch HF user info, upsert into DB, return the user row.

    ``token_data`` must contain ``access_token``, and may contain
    ``refresh_token`` and ``expires_in`` from the token exchange response.
    If ``user_id`` is provided, look up existing user by that internal UUID first.
    If found, use their github_id for the upsert to ensure merge works.
    If ``user_id`` is None, derives a deterministic ID from the HF username.
    """
    hf_token = token_data["access_token"]
    info = get_hf_user(hf_token)
    encrypted = crypto.encrypt_token(hf_token)
    refresh_encrypted = crypto.encrypt_token(token_data["refresh_token"]) if token_data.get("refresh_token") else None
    expires_at = ""
    if token_data.get("expires_in"):
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))).isoformat()

    # Derive a deterministic ID if none provided
    if user_id is None:
        user_id = jwt_auth.derive_user_id_from_hf(info["name"])

    github_id = None
    if user_id:
        existing = db.get_user(user_id)
        if existing:
            github_id = existing.get("github_id")
    return db.upsert_user(
        user_id=user_id,
        github_id=github_id,
        hf_id=info["name"],
        hf_username=info["name"],
        hf_token_encrypted=encrypted,
        hf_refresh_token_encrypted=refresh_encrypted,
        hf_token_expires_at=expires_at,
    )


def _hf_token_for_user(user_id: str) -> str:
    """Retrieve and decrypt a user's stored HF token.

    Auto-refreshes the token if it expires within 7 days and a refresh
    token is available.  Raises RuntimeError if no token is stored.
    """
    user = db.get_user(user_id)
    if not user or not user.get("hf_token_encrypted"):
        raise RuntimeError("HF not connected — please link your Hugging Face account")
    expires_at = user.get("hf_token_expires_at") or ""
    if _hf_token_expires_soon(expires_at) and user.get("hf_refresh_token_encrypted"):
        try:
            _refresh_hf_token(user_id, user)
        except Exception as exc:
            print(f"[dataset_persistence] token refresh failed: {exc}", flush=True)
    user = db.get_user(user_id)
    return crypto.decrypt_token(user["hf_token_encrypted"])


def _hf_token_expires_soon(expires_at: str, within_days: int = 7) -> bool:
    """Return True if the token expires within ``within_days`` or is already expired."""
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
        return expiry - timedelta(days=within_days) < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def _refresh_hf_token(user_id: str, user: dict) -> None:
    """Refresh a user's HF token using the stored refresh token.

    Updates the DB with new access/refresh tokens and expiry.
    """
    refresh_encrypted = user.get("hf_refresh_token_encrypted")
    if not refresh_encrypted:
        raise RuntimeError("no refresh token stored")
    old_refresh = crypto.decrypt_token(refresh_encrypted)
    body = urlencode({
        "client_id": os.environ.get("HF_CLIENT_ID", ""),
        "client_secret": os.environ.get("HF_CLIENT_SECRET", ""),
        "grant_type": "refresh_token",
        "refresh_token": old_refresh,
    }).encode()
    req = urllib.request.Request(HF_TOKEN_URL, data=body, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HF token refresh failed (HTTP {exc.code}): {detail}") from exc
    new_token = data.get("access_token", "").strip()
    if not new_token:
        raise RuntimeError(f"HF token refresh returned no access_token: {data}")
    encrypted = crypto.encrypt_token(new_token)
    new_refresh_encrypted = None
    if data.get("refresh_token"):
        new_refresh_encrypted = crypto.encrypt_token(data["refresh_token"])
    expires_at = ""
    if data.get("expires_in"):
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))).isoformat()
    db.upsert_user(
        hf_id=user["hf_id"],
        hf_token_encrypted=encrypted,
        hf_refresh_token_encrypted=new_refresh_encrypted,
        hf_token_expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Dataset persistence
# ---------------------------------------------------------------------------

_upload_scheduler: threading.Thread | None = None
_shutdown_event = threading.Event()
_current_hf_token: str = ""


def dataset_name(hf_username: str) -> str:
    """Return the full dataset repo name for a user.

    Format: {hf_username}/loom-priv-{suffix}
    """
    suffix = os.environ.get("LOOM_DATASET_SUFFIX", "").strip() or _space_name()
    return f"{hf_username}/loom-priv-{suffix}"


def init_persistence(user_id: str) -> None:
    """Initialize dataset persistence for the current user.

    Ensures the private dataset repo exists, downloads the latest DB,
    and starts the periodic upload scheduler.
    """
    global _current_hf_token
    token = _hf_token_for_user(user_id)
    _current_hf_token = token
    user = db.get_user(user_id)
    hf_user = user.get("hf_username", "") if user else ""
    if not hf_user:
        info = get_hf_user(token)
        hf_user = info.get("name", "")

    ds_name = dataset_name(hf_user)

    try:
        _hf_api(f"/api/repos/{ds_name}", token=token)
    except RuntimeError as exc:
        if "404" in str(exc):
            _hf_api("/api/repos", method="POST", token=token, body={
                "name": ds_name,
                "private": True,
                "type": "dataset",
            })
        else:
            raise

    _download_db(token, ds_name)
    _start_upload_scheduler(token, ds_name)


def _download_db(token: str, ds_name: str) -> None:
    """Download loom.db from the dataset repo to /data/loom.db if it exists."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import RepositoryNotFoundError, RevisionNotFoundError
    try:
        path = hf_hub_download(
            repo_id=ds_name,
            filename="loom.db",
            token=token,
            repo_type="dataset",
        )
        import shutil
        shutil.copy2(path, db.DB_PATH)
    except (RepositoryNotFoundError, RevisionNotFoundError, OSError):
        pass


def upload_db(token: str, ds_name: str) -> None:
    """Upload the current loom.db to the dataset repo."""
    if not db.DB_PATH.exists():
        return
    from huggingface_hub import upload_file
    from huggingface_hub.utils import HfHubHTTPError
    try:
        upload_file(
            path_or_fileobj=str(db.DB_PATH),
            path_in_repo="loom.db",
            repo_id=ds_name,
            repo_type="dataset",
            token=token,
            commit_message="auto-sync loom.db",
        )
    except HfHubHTTPError as exc:
        print(f"[dataset_persistence] upload failed: {exc}", flush=True)


def _start_upload_scheduler(token: str, ds_name: str, interval: int = 120) -> None:
    """Start a daemon thread that uploads the DB every ``interval`` seconds."""
    global _upload_scheduler
    if _upload_scheduler is not None:
        return

    def loop():
        while not _shutdown_event.is_set():
            if _shutdown_event.wait(interval):
                break
            try:
                upload_db(token, ds_name)
            except Exception:
                pass

    _upload_scheduler = threading.Thread(target=loop, daemon=True)
    _upload_scheduler.start()


def shutdown_upload() -> None:
    """Signal the upload scheduler to stop and do one final upload."""
    global _upload_scheduler
    _shutdown_event.set()
    if _upload_scheduler is not None:
        _upload_scheduler.join(timeout=10)
        _upload_scheduler = None

    if _current_hf_token:
        try:
            info = get_hf_user(_current_hf_token)
            ds_name = dataset_name(info.get("name", ""))
            upload_db(_current_hf_token, ds_name)
        except Exception:
            pass
