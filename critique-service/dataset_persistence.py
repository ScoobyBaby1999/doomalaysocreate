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
from pathlib import Path
from urllib.parse import urlencode

import crypto
import db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HF_AUTHORIZE_URL = "https://huggingface.co/oauth/authorize"
HF_TOKEN_URL = "https://huggingface.co/oauth/token"
HF_API_BASE = "https://huggingface.co"
HF_SCOPES = "openid profile write"

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

def make_hf_authorize_url(state: str) -> str:
    """Build the HF OAuth authorize URL with state token."""
    params = urlencode({
        "client_id": os.environ.get("HF_CLIENT_ID", ""),
        "scope": HF_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return f"{HF_AUTHORIZE_URL}?{params}"


def exchange_hf_code(code: str) -> str:
    """Exchange OAuth code for access token. Returns the raw token string.

    Raises RuntimeError on failure.
    """
    body = urlencode({
        "client_id": os.environ.get("HF_CLIENT_ID", ""),
        "client_secret": os.environ.get("HF_CLIENT_SECRET", ""),
        "code": code,
        "grant_type": "authorization_code",
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
    return token


def get_hf_user(token: str) -> dict:
    """Fetch authenticated user info from HF API."""
    return _hf_api("/api/whoami-v2", token=token)


def upsert_user_from_hf(hf_token: str) -> dict:
    """Fetch HF user info, upsert into DB, return the user row."""
    info = get_hf_user(hf_token)
    encrypted = crypto.encrypt_token(hf_token)
    return db.upsert_user(
        hf_id=info["name"],
        hf_username=info["name"],
        hf_token_encrypted=encrypted,
    )


def _hf_token_for_user(user_id: str) -> str:
    """Retrieve and decrypt a user's stored HF token. Raises if missing."""
    user = db.get_user(user_id)
    if not user or not user.get("hf_token_encrypted"):
        raise RuntimeError("HF not connected — please link your Hugging Face account")
    return crypto.decrypt_token(user["hf_token_encrypted"])


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
            _hf_api(f"/api/repos/{ds_name}", method="POST", token=token, body={
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
