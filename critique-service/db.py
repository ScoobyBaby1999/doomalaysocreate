"""SQLite database for GitHub integration: users, workspaces, push logs, registry.

All state is stored in a single ``loom.db`` file in the service's data directory.
The schema is created/migrated on first access via ``init_db()``.  Thread-safe:
SQLite is accessed with ``check_same_thread=False`` and all writes go through a
serializing ``threading.Lock``.

stdlib-only at import time; ``cryptography`` (for token encryption) is only
imported when actually encrypting/decrypting tokens.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(os.environ.get("LOOM_DB_PATH",
                              Path(__file__).resolve().parent / "loom.db"))

_write_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    """Create tables if they don't exist."""
    db = _db()
    db.executescript(SCHEMA)
    db.commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    github_id       INTEGER UNIQUE,
    github_username TEXT,
    github_token_encrypted TEXT,
    hf_id           TEXT,
    hf_username     TEXT,
    hf_token_encrypted TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    source_repo     TEXT,
    source_branch   TEXT,
    current_branch  TEXT NOT NULL DEFAULT 'main',
    sandbox_path    TEXT NOT NULL,
    hf_space_id     TEXT,
    auto_sync       INTEGER NOT NULL DEFAULT 0,
    visibility      TEXT NOT NULL DEFAULT 'private'
                    CHECK (visibility IN ('public', 'private')),
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_modified   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS push_logs (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    target_repo     TEXT NOT NULL,
    target_branch   TEXT NOT NULL,
    commit_sha      TEXT NOT NULL,
    commit_message  TEXT,
    push_type       TEXT NOT NULL CHECK (push_type IN ('direct', 'pr')),
    pr_number       INTEGER,
    pr_url          TEXT,
    approved_by_user INTEGER NOT NULL DEFAULT 0,
    auto_approved   INTEGER NOT NULL DEFAULT 0,
    files_changed   INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS registry (
    workspace_id    TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    indexed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    owner_username  TEXT NOT NULL,
    owner_avatar    TEXT DEFAULT '',
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    source_repo     TEXT,
    hf_space_url    TEXT
);

CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id);
CREATE INDEX IF NOT EXISTS idx_push_logs_workspace ON push_logs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_registry_indexed ON registry(indexed_at);
"""


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def upsert_user(*, github_id: int | None = None, github_username: str | None = None,
                github_token_encrypted: str | None = None,
                hf_id: str | None = None, hf_username: str | None = None,
                hf_token_encrypted: str | None = None) -> dict:
    """Create or update a user by github_id or hf_id. Returns the user row."""
    db = _db()
    now = _iso_now()
    with _write_lock:
        # find existing
        user = None
        if github_id is not None:
            row = db.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
            if row:
                user = dict(row)
        if user is None and hf_id is not None:
            row = db.execute("SELECT * FROM users WHERE hf_id = ?", (hf_id,)).fetchone()
            if row:
                user = dict(row)
        if user is None:
            uid = _gen_id()
            db.execute(
                "INSERT INTO users (id, github_id, github_username, github_token_encrypted, "
                "hf_id, hf_username, hf_token_encrypted, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, github_id, github_username, github_token_encrypted,
                 hf_id, hf_username, hf_token_encrypted, now, now))
            db.commit()
            return get_user(uid)
        # update
        updates = []
        params = []
        if github_id is not None:
            updates.append("github_id = ?")
            params.append(github_id)
        if github_username is not None:
            updates.append("github_username = ?")
            params.append(github_username)
        if github_token_encrypted is not None:
            updates.append("github_token_encrypted = ?")
            params.append(github_token_encrypted)
        if hf_id is not None:
            updates.append("hf_id = ?")
            params.append(hf_id)
        if hf_username is not None:
            updates.append("hf_username = ?")
            params.append(hf_username)
        if hf_token_encrypted is not None:
            updates.append("hf_token_encrypted = ?")
            params.append(hf_token_encrypted)
        if updates:
            updates.append("updated_at = ?")
            params.append(now)
            params.append(user["id"])
            db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            db.commit()
    return get_user(user["id"])


def get_user(user_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_github_id(github_id: int) -> dict | None:
    row = _db().execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_hf_id(hf_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM users WHERE hf_id = ?", (hf_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_session_id(session_id: str) -> dict | None:
    """Session ID IS the user ID for now (simple session model)."""
    return get_user(session_id)


def delete_github_token(user_id: str) -> None:
    with _write_lock:
        db = _db()
        db.execute("UPDATE users SET github_id=NULL, github_username=NULL, "
                   "github_token_encrypted=NULL, updated_at=? WHERE id=?",
                   (_iso_now(), user_id))
        db.commit()


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def create_workspace(user_id: str, *, title: str, source_repo: str | None = None,
                     source_branch: str | None = None, visibility: str = "private",
                     description: str = "", auto_sync: bool = False,
                     sandbox_path: str = "", hf_space_id: str | None = None) -> dict:
    db = _db()
    wid = _gen_id()
    now = _iso_now()
    with _write_lock:
        db.execute(
            "INSERT INTO workspaces (id, user_id, source_repo, source_branch, "
            "sandbox_path, hf_space_id, auto_sync, visibility, title, description, "
            "created_at, last_modified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, user_id, source_repo, source_branch, sandbox_path, hf_space_id,
             int(auto_sync), visibility, title, description, now, now))
        db.commit()
    return get_workspace(wid)


def get_workspace(workspace_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def list_user_workspaces(user_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM workspaces WHERE user_id = ? ORDER BY last_modified DESC",
        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def update_workspace(workspace_id: str, **fields) -> dict | None:
    allowed = {"title", "description", "visibility", "current_branch",
               "hf_space_id", "auto_sync", "last_modified", "sandbox_path"}
    updates = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "auto_sync":
            v = int(v)
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        return get_workspace(workspace_id)
    updates.append("last_modified = ?")
    params.append(_iso_now())
    params.append(workspace_id)
    with _write_lock:
        _db().execute(f"UPDATE workspaces SET {', '.join(updates)} WHERE id = ?", params)
        _db().commit()
    return get_workspace(workspace_id)


def delete_workspace(workspace_id: str) -> bool:
    with _write_lock:
        db = _db()
        db.execute("DELETE FROM registry WHERE workspace_id = ?", (workspace_id,))
        cur = db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        db.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Push logs
# ---------------------------------------------------------------------------

def log_push(user_id: str, workspace_id: str, *, target_repo: str, target_branch: str,
             commit_sha: str, commit_message: str = "", push_type: str = "direct",
             pr_number: int | None = None, pr_url: str | None = None,
             approved_by_user: bool = False, auto_approved: bool = False,
             files_changed: int = 0) -> dict:
    db = _db()
    pid = _gen_id()
    with _write_lock:
        db.execute(
            "INSERT INTO push_logs (id, workspace_id, user_id, target_repo, target_branch, "
            "commit_sha, commit_message, push_type, pr_number, pr_url, approved_by_user, "
            "auto_approved, files_changed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, workspace_id, user_id, target_repo, target_branch, commit_sha,
             commit_message, push_type, pr_number, pr_url, int(approved_by_user),
             int(auto_approved), files_changed, _iso_now()))
        db.commit()
    row = _db().execute("SELECT * FROM push_logs WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else {}


def list_push_logs(workspace_id: str, limit: int = 50) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM push_logs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
        (workspace_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Registry (public spaces)
# ---------------------------------------------------------------------------

def register_public_workspace(workspace_id: str, *, owner_username: str,
                              title: str, description: str = "",
                              source_repo: str | None = None,
                              hf_space_url: str | None = None,
                              owner_avatar: str = "") -> dict:
    db = _db()
    with _write_lock:
        db.execute(
            "INSERT OR REPLACE INTO registry (workspace_id, indexed_at, owner_username, "
            "owner_avatar, title, description, source_repo, hf_space_url) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (workspace_id, _iso_now(), owner_username, owner_avatar,
             title, description, source_repo, hf_space_url))
        db.commit()
    row = _db().execute("SELECT * FROM registry WHERE workspace_id = ?",
                        (workspace_id,)).fetchone()
    return dict(row) if row else {}


def unregister_public_workspace(workspace_id: str) -> None:
    with _write_lock:
        _db().execute("DELETE FROM registry WHERE workspace_id = ?", (workspace_id,))
        _db().commit()


def list_public_workspaces(*, page: int = 1, per_page: int = 20,
                           sort: str = "recent", search: str = "") -> dict:
    db = _db()
    where = "WHERE 1=1"
    params: list = []
    if search:
        where += " AND (r.title LIKE ? ESCAPE '\\' OR r.description LIKE ? ESCAPE '\\' OR r.owner_username LIKE ? ESCAPE '\\')"
        # escape LIKE wildcards to prevent pattern injection
        q = f"%{search.replace('%', '\\%').replace('_', '\\_')}%"
        params += [q, q, q]
    order = "r.indexed_at DESC" if sort == "recent" else "r.indexed_count DESC"
    #   indexed_count doesn't exist yet — fall back to indexed_at
    order = "r.indexed_at DESC"
    total = db.execute(f"SELECT COUNT(*) FROM registry r {where}", params).fetchone()[0]
    offset = (max(1, page) - 1) * per_page
    rows = db.execute(
        f"SELECT r.* FROM registry r {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total,
            "page": page, "per_page": per_page}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
