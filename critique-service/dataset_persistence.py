"""HF Dataset persistent storage + OAuth for the loom critique service.

Persists the SQLite DB to a private HF Dataset so data survives Space rebuilds.
Also provides HF OAuth helpers so users can authorize dataset access.

Tier 3 extension: also persists the ``.brain/`` folder for each active
Conscious workspace as ``brains/<workspace_id>.tar.gz`` (one tarball per
workspace, uploaded every 120s alongside ``loom.db``; re-downloaded on boot).
See TIER3_PLAN.md §7.2 for the spec.

Env vars required:
    HF_CLIENT_ID       — HF OAuth App client ID
    HF_CLIENT_SECRET   — HF OAuth App client secret
    ENCRYPTION_KEY     — Fernet key for token encryption (auto-generated if absent)

Env vars optional:
    LOOM_DATASET_SUFFIX  — suffix for the dataset name (default: main)
                           The full name is {hf_username}/loom-priv-{suffix}
"""
from __future__ import annotations

import io
import json
import os
import tarfile
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
        detail = exc.read().decode(errors="replace")[:200]
        # don't include the full response — it may contain tokens (audit H4)
        raise RuntimeError(f"HF token exchange failed (HTTP {exc.code})") from exc
    token = data.get("access_token", "").strip()
    if not token:
        # log only the keys present, not the values (which may contain tokens)
        raise RuntimeError(f"HF token exchange returned no access_token (keys: {list(data.keys())})")
    return data


def get_hf_user(token: str) -> dict:
    """Fetch authenticated user info from HF API."""
    return _hf_api("/api/whoami-v2", token=token)


def _valid_user_id(user_id: str | None) -> bool:
    """Return True if user_id looks like a valid 16-char hex ID.

    Rejects JWT strings (contain dots) and other garbage that might
    be accidentally passed by the frontend.
    """
    if not user_id or not isinstance(user_id, str):
        return False
    if len(user_id) != 16:
        return False
    if "." in user_id:
        return False
    try:
        int(user_id, 16)
        return True
    except ValueError:
        return False


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
    if user_id is None or not _valid_user_id(user_id):
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
            print(f"[dataset_persistence] token refresh failed: {type(exc).__name__}", flush=True)
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

    Tier 3 extension: after the DB download, also restores every active
    Conscious's ``.brain/`` folder (§7.4). If the workspace sandbox doesn't
    exist (Space was rebuilt), it's recreated from the workspace's
    ``source_repo`` via the existing token-safe ``_clone_repo`` (Tier 1).
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
    # Tier 3 — restore .brain/ folders for all active Conscious workspaces.
    _restore_all_brains(token, ds_name)
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
        print(f"[dataset_persistence] upload failed: {type(exc).__name__}", flush=True)


def _start_upload_scheduler(token: str, ds_name: str, interval: int = 120) -> None:
    """Start a daemon thread that uploads the DB every ``interval`` seconds.

    Tier 3 extension: also uploads every active Conscious's ``.brain/`` folder
    (one tarball per workspace). Single-threaded; no new locks. Failures in
    one workspace don't block others (each wrapped in try/except, logged).
    """
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
            # Tier 3 — also sync brains
            try:
                _upload_all_brains(token, ds_name)
            except Exception as exc:
                print(f"[dataset_persistence] brain sync failed: {type(exc).__name__}", flush=True)

    _upload_scheduler = threading.Thread(target=loop, daemon=True)
    _upload_scheduler.start()


# ---------------------------------------------------------------------------
# Tier 3 — .brain/ folder sync (§7.2, §7.4)
# ---------------------------------------------------------------------------

BRAIN_SYNC_INTERVAL_S = 120  # same as DB; shares the scheduler


def _tar_brain(brain_path: Path) -> bytes:
    """Pack a .brain/ folder into a tar.gz bytes blob. Skips if path missing."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(brain_path), arcname=".brain")
    return buf.getvalue()


def _untar_brain(blob: bytes, dest_workspace: Path) -> None:
    """Extract a tar.gz brain blob into ``dest_workspace`` (.brain/ lands there).

    Phase 5 security: uses ``filter='data'`` (Python 3.12+) to prevent
    path-traversal via malicious tarball entries (audit L4). On older Pythons
    where the filter param doesn't exist, falls back to manual member
    validation.
    """
    buf = io.BytesIO(blob)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        try:
            # Python 3.12+ — built-in data filter blocks path traversal
            tar.extractall(str(dest_workspace), filter="data")
        except TypeError:
            # older Python — manual validation: reject members with .. or abs paths
            dest = dest_workspace.resolve()
            for member in tar.getmembers():
                member_path = (dest_workspace / member.name).resolve()
                if not str(member_path).startswith(str(dest)):
                    raise RuntimeError(f"tar member escapes dest: {member.name}")
            tar.extractall(str(dest_workspace))


def upload_brain(token: str, ds_name: str, workspace_id: str,
                 brain_path: Path) -> None:
    """Upload the .brain/ folder for one workspace to the HF Dataset.

    Packs .brain/ as a tar.gz, uploads as ``brains/<workspace_id>.tar.gz``.
    Idempotent: overwrites the previous upload. Skips if brain_path missing.

    Per TIER3_PLAN §7.2: one tarball per workspace per 120s tick (not per-file)
    to keep HF Dataset upload calls bounded.

    Phase 3: after a successful upload, stamps ``conscious.last_synced_at`` for
    every active conscious in this workspace so the API can surface stale
    syncs (>10min) — TIER3_PLAN §17 risk mitigation.

    Phase 5 security: scans .brain/ for known secret patterns BEFORE upload.
    If secrets are found, the upload is REFUSED (TIER3_PLAN §17: "refuse to
    commit if any file in .brain/ matches a secret pattern"). This is the
    second defense layer — commit_brain scans first, this scans before HF.
    """
    if not brain_path.is_dir():
        return
    # Security: scan for secrets before uploading to HF (audit C1)
    try:
        import brain as _brain_mod
        findings = _brain_mod.scan_for_secrets(brain_path)
        if findings:
            print(f"[dataset_persistence] BRAIN UPLOAD BLOCKED for {workspace_id}: "
                  f"{len(findings)} secret(s) detected in .brain/", flush=True)
            return  # refuse to upload
    except Exception:
        pass  # scan failure must never block — but log it
    try:
        blob = _tar_brain(brain_path)
    except Exception as exc:
        print(f"[dataset_persistence] tar brain failed for {workspace_id}: "
              f"{type(exc).__name__}",  # don't leak details (audit H4)
              flush=True)
        return
    from huggingface_hub import upload_file
    from huggingface_hub.utils import HfHubHTTPError
    try:
        upload_file(
            path_or_fileobj=blob,
            path_in_repo=f"brains/{workspace_id}.tar.gz",
            repo_id=ds_name,
            repo_type="dataset",
            token=token,
            commit_message=f"auto-sync brain {workspace_id}",
        )
    except HfHubHTTPError as exc:
        print(f"[dataset_persistence] brain upload failed for {workspace_id}: {type(exc).__name__}",
              flush=True)
        return
    # Phase 3 — stamp last_synced_at on every active conscious in this workspace
    try:
        import conscious_db
        now = conscious_db._iso_now()
        with _db_write_lock_for_sync():
            _db_for_sync().execute(
                "UPDATE conscious SET last_synced_at = ?, updated_at = ? "
                "WHERE workspace_id = ? AND status = 'active'",
                (now, now, workspace_id))
            _db_for_sync().commit()
    except Exception as exc:
        print(f"[dataset_persistence] stamp last_synced_at failed for {workspace_id}: {type(exc).__name__}",
              flush=True)


# Phase 3 — local helpers to avoid circular import of db._write_lock at module
# load time. conscious_db imports db, and dataset_persistence imports
# conscious_db lazily inside functions; these helpers mirror that pattern.
def _db_for_sync():
    import db as _db
    return _db._db()


def _db_write_lock_for_sync():
    import db as _db
    return _db._write_lock


def download_brain(token: str, ds_name: str, workspace_id: str,
                   dest_workspace: Path) -> None:
    """Download and extract ``brains/<workspace_id>.tar.gz`` into dest_workspace.

    No-op if the file doesn't exist in the dataset (first run for that workspace).
    """
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import RepositoryNotFoundError, RevisionNotFoundError, EntryNotFoundError
    try:
        path = hf_hub_download(
            repo_id=ds_name,
            filename=f"brains/{workspace_id}.tar.gz",
            token=token,
            repo_type="dataset",
        )
    except (RepositoryNotFoundError, RevisionNotFoundError, EntryNotFoundError, OSError):
        return  # first run — no brain yet
    try:
        with open(path, "rb") as f:
            blob = f.read()
        dest_workspace.mkdir(parents=True, exist_ok=True)
        _untar_brain(blob, dest_workspace)
    except Exception as exc:
        print(f"[dataset_persistence] brain extract failed for {workspace_id}: {type(exc).__name__}",
              flush=True)


def _upload_all_brains(token: str, ds_name: str) -> None:
    """Walk all active Conscious workspaces and upload each .brain/ folder."""
    try:
        import conscious_db
        items = conscious_db.list_active_conscious_workspaces()
    except Exception:
        return  # conscious tables not initialized yet
    for item in items:
        ws_id = item["workspace_id"]
        sandbox = item.get("sandbox_path") or ""
        if not sandbox:
            continue
        brain_path = Path(sandbox) / ".brain"
        try:
            upload_brain(token, ds_name, ws_id, brain_path)
        except Exception as exc:
            print(f"[dataset_persistence] brain upload error for {ws_id}: {type(exc).__name__}",
                  flush=True)
            continue


def _restore_all_brains(token: str, ds_name: str) -> None:
    """On boot, restore .brain/ for every Conscious in the restored DB.

    For each Conscious row: if the workspace sandbox doesn't exist, recreate
    it from the workspace's source_repo via Tier 1's token-safe ``_clone_repo``
    (in github_integration); then extract the brain tarball into it.
    """
    try:
        import conscious_db
        items = conscious_db.list_active_conscious_workspaces()
    except Exception:
        return  # conscious tables not initialized
    for item in items:
        ws_id = item["workspace_id"]
        sandbox = item.get("sandbox_path") or ""
        if not sandbox:
            continue
        sandbox_path = Path(sandbox)
        try:
            if not sandbox_path.is_dir():
                # recreate the workspace from source_repo (Tier 1 token-safe clone)
                ws = db.get_workspace(ws_id)
                if ws and ws.get("source_repo") and ws.get("user_id"):
                    try:
                        import github_integration
                        github_integration._clone_repo(
                            ws["user_id"], ws["source_repo"],
                            branch=ws.get("source_branch"),
                            dest=str(sandbox_path),
                            branches=None)
                    except Exception as exc:
                        print(f"[dataset_persistence] workspace re-clone failed "
                              f"for {ws_id}: {type(exc).__name__}", flush=True)
                        continue
                else:
                    continue
            download_brain(token, ds_name, ws_id, sandbox_path)
        except Exception as exc:
            print(f"[dataset_persistence] brain restore error for {ws_id}: {type(exc).__name__}",
                  flush=True)
            continue


def shutdown_upload() -> None:
    """Signal the upload scheduler to stop and do one final upload.

    Tier 3: also does a final brain sync so the latest brain state survives
    a Space rebuild.
    """
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
            _upload_all_brains(_current_hf_token, ds_name)
        except Exception:
            pass
