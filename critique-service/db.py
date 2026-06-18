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

DB_PATH = Path(os.environ.get("LOOM_DB_PATH", "/data/loom.db"))

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
    """Create tables if they don't exist.  Runs migrations for existing tables."""
    db = _db()
    try:
        db.executescript(SCHEMA)
        db.commit()
    except Exception as exc:
        # If the bulk executescript fails (e.g., a syntax error in one table
        # definition on an older DB), try creating each table individually
        # so one failure doesn't block all tables.
        print(f"[db] init_db executescript failed, trying individual: {exc}", flush=True)
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                try:
                    db.execute(stmt)
                except Exception:
                    pass  # table/column already exists or syntax issue — skip
        db.commit()
    # schema migrations for existing databases
    _migrate(db)
    # cleanup expired sessions on boot
    try:
        cleanup_expired_sessions()
    except Exception:
        pass


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns that may not exist in older DBs."""
    for col in (
        "hf_refresh_token_encrypted",
        "hf_token_expires_at",
    ):
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    # source_branches is on workspaces, not users
    try:
        db.execute("ALTER TABLE workspaces ADD COLUMN source_branches TEXT")
    except sqlite3.OperationalError:
        pass
    # Phase 3 — last_synced_at on conscious (for stale HF Dataset sync detection)
    try:
        db.execute("ALTER TABLE conscious ADD COLUMN last_synced_at TEXT")
    except sqlite3.OperationalError:
        pass
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
    hf_refresh_token_encrypted TEXT,
    hf_token_expires_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    source_repo     TEXT,
    source_branch   TEXT,
    source_branches TEXT,
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

-- Server-side sessions (Fix 7 + server-side sessions phase)
-- Replaces localStorage JWT storage with HttpOnly cookie + server-side session.
-- The jwt_payload_json stores the full JWT payload (including encrypted
-- GitHub/HF tokens) so the backend can reconstruct user identity without
-- the JWT ever touching the client after the one-time exchange.
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,               -- session_id (random, not the JWT)
    user_id         TEXT NOT NULL REFERENCES users(id),
    jwt_payload     TEXT NOT NULL,                  -- JSON: the full JWT payload (encrypted tokens inside)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,                  -- 7 days from creation
    revoked         INTEGER NOT NULL DEFAULT 0,     -- 1 = revoked (logout)
    last_used_at    TEXT                            -- updated on each request (for idle timeout)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at, revoked);

-- ===========================================================================
-- Tier 3 — Conscious: Multi-Agent Shared-Brain Architecture (Phase 1)
-- 7 new tables, all prefixed conscious_. See TIER3_PLAN.md §9 for the spec.
-- FK ordering note: conscious_proposal is defined BEFORE conscious_blackboard_entry
-- because the latter references proposal(id). SQLite allows forward refs within
-- the same executescript as long as the referenced table exists by the time the
-- script finishes — confirmed safe.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS conscious (
    id                    TEXT PRIMARY KEY,
    workspace_id          TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_user_id         TEXT NOT NULL REFERENCES users(id),
    title                 TEXT NOT NULL DEFAULT 'Untitled Conscious',
    goal                  TEXT NOT NULL DEFAULT '',
    orchestrator_agent_id TEXT,
    cost_ceiling_usd      REAL NOT NULL DEFAULT 0,
    cost_spent_usd        REAL NOT NULL DEFAULT 0,
    brain_commit_policy   TEXT NOT NULL DEFAULT 'on' CHECK (brain_commit_policy IN ('on','off')),
    graphiti_enabled      INTEGER NOT NULL DEFAULT 0,
    max_agents            INTEGER NOT NULL DEFAULT 8,
    status                TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
    last_synced_at        TEXT,                          -- Phase 3: last HF Dataset brain sync timestamp (null = never)
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conscious_workspace ON conscious(workspace_id);
CREATE INDEX IF NOT EXISTS idx_conscious_owner ON conscious(owner_user_id);

CREATE TABLE IF NOT EXISTS conscious_agent (
    id                TEXT PRIMARY KEY,
    conscious_id      TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    role              TEXT NOT NULL,
    model             TEXT NOT NULL,
    tier              TEXT NOT NULL CHECK (tier IN ('claude','open')),
    status            TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle','running','waiting','done','failed')),
    worktree_path     TEXT,
    branch            TEXT,
    parent_agent_id   TEXT REFERENCES conscious_agent(id),
    subscribed_events TEXT NOT NULL DEFAULT '[]',
    is_orchestrator   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conscious_agent_conscious ON conscious_agent(conscious_id);
CREATE INDEX IF NOT EXISTS idx_conscious_agent_orchestrator ON conscious_agent(conscious_id, is_orchestrator);

-- conscious_proposal is defined BEFORE conscious_blackboard_entry (forward FK).
CREATE TABLE IF NOT EXISTS conscious_proposal (
    id                      TEXT PRIMARY KEY,
    conscious_id            TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    proposer_agent_id       TEXT NOT NULL REFERENCES conscious_agent(id),
    section                 TEXT NOT NULL,
    key                     TEXT NOT NULL,
    value                   TEXT NOT NULL,
    reason                  TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','committed','rejected','expired')),
    committed_by_agent_id   TEXT REFERENCES conscious_agent(id),
    rejection_reason        TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposal_conscious ON conscious_proposal(conscious_id, status);

CREATE TABLE IF NOT EXISTS conscious_blackboard_entry (
    id                      TEXT PRIMARY KEY,
    conscious_id            TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    section                 TEXT NOT NULL,
    key                     TEXT NOT NULL,
    value                   TEXT NOT NULL,
    author_agent_id         TEXT NOT NULL REFERENCES conscious_agent(id),
    committed_by_agent_id   TEXT NOT NULL REFERENCES conscious_agent(id),
    proposal_id             TEXT REFERENCES conscious_proposal(id),
    version                 INTEGER NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(conscious_id, section, key, version)
);
CREATE INDEX IF NOT EXISTS idx_bb_conscious ON conscious_blackboard_entry(conscious_id);
CREATE INDEX IF NOT EXISTS idx_bb_section_key ON conscious_blackboard_entry(conscious_id, section, key, version);

CREATE TABLE IF NOT EXISTS conscious_drawer_entry (
    id                TEXT PRIMARY KEY,
    conscious_id      TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    invoke_id         TEXT NOT NULL,
    from_agent_id     TEXT NOT NULL REFERENCES conscious_agent(id),
    to_agent_id       TEXT NOT NULL REFERENCES conscious_agent(id),
    kind              TEXT NOT NULL CHECK (kind IN ('invoke','delegate')),
    task              TEXT NOT NULL,
    inputs            TEXT NOT NULL DEFAULT '{}',
    result            TEXT NOT NULL DEFAULT '',
    result_path       TEXT,
    files_path        TEXT,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed','timeout')),
    started_at        TEXT,
    completed_at      TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drawer_conscious ON conscious_drawer_entry(conscious_id, status);
CREATE INDEX IF NOT EXISTS idx_drawer_to_agent ON conscious_drawer_entry(to_agent_id, status);

CREATE TABLE IF NOT EXISTS conscious_message (
    id              TEXT PRIMARY KEY,
    conscious_id    TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    from_agent_id   TEXT NOT NULL REFERENCES conscious_agent(id),
    to_agent_id     TEXT,
    body            TEXT NOT NULL,
    read_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_msg_conscious ON conscious_message(conscious_id, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_to_agent ON conscious_message(to_agent_id, read_at);

CREATE TABLE IF NOT EXISTS conscious_task (
    id                TEXT PRIMARY KEY,
    conscious_id      TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    assignee_agent_id TEXT REFERENCES conscious_agent(id),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','in_progress','done','failed','blocked')),
    depends_on        TEXT NOT NULL DEFAULT '[]',
    cost_ceiling_usd  REAL,
    cost_spent_usd    REAL NOT NULL DEFAULT 0,
    claimed_at        TEXT,
    completed_at      TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_conscious ON conscious_task(conscious_id, status);
"""


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def upsert_user(*, user_id: str | None = None, github_id: int | None = None, github_username: str | None = None,
                github_token_encrypted: str | None = None,
                hf_id: str | None = None, hf_username: str | None = None,
                hf_token_encrypted: str | None = None,
                hf_refresh_token_encrypted: str | None = None,
                hf_token_expires_at: str | None = None) -> dict:
    """Create or update a user by user_id (internal UUID), github_id, or hf_id.
    Returns the user row. user_id takes precedence for lookup."""
    db = _db()
    now = _iso_now()
    with _write_lock:
        # find existing
        user = None
        if user_id is not None:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row:
                user = dict(row)
        if user is None and github_id is not None:
            row = db.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
            if row:
                user = dict(row)
        if user is None and hf_id is not None:
            row = db.execute("SELECT * FROM users WHERE hf_id = ?", (hf_id,)).fetchone()
            if row:
                user = dict(row)
        if user is None:
            uid = user_id if user_id else _gen_id()
            db.execute(
                "INSERT INTO users (id, github_id, github_username, github_token_encrypted, "
                "hf_id, hf_username, hf_token_encrypted, "
                "hf_refresh_token_encrypted, hf_token_expires_at, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, github_id, github_username, github_token_encrypted,
                 hf_id, hf_username, hf_token_encrypted,
                 hf_refresh_token_encrypted, hf_token_expires_at, now, now))
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
        if hf_refresh_token_encrypted is not None:
            updates.append("hf_refresh_token_encrypted = ?")
            params.append(hf_refresh_token_encrypted)
        if hf_token_expires_at is not None:
            updates.append("hf_token_expires_at = ?")
            params.append(hf_token_expires_at)
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
                     source_branch: str | None = None,
                     source_branches: list[str] | None = None,
                     visibility: str = "private",
                     description: str = "", auto_sync: bool = False,
                     sandbox_path: str = "", hf_space_id: str | None = None) -> dict:
    db = _db()
    wid = _gen_id()
    now = _iso_now()
    branches_json = json.dumps(source_branches) if source_branches else None
    with _write_lock:
        db.execute(
            "INSERT INTO workspaces (id, user_id, source_repo, source_branch, "
            "source_branches, sandbox_path, hf_space_id, auto_sync, visibility, "
            "title, description, created_at, last_modified) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, user_id, source_repo, source_branch, branches_json,
             sandbox_path, hf_space_id,
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
               "hf_space_id", "auto_sync", "last_modified", "sandbox_path",
               "source_branches"}
    updates = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "auto_sync":
            v = int(v)
        elif k == "source_branches" and isinstance(v, list):
            v = json.dumps(v)
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
        escaped = search.replace('%', '\\%').replace('_', '\\_')
        q = "%" + escaped + "%"
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


# ---------------------------------------------------------------------------
# Server-side sessions (Fix 7 + server-side sessions phase)
# ---------------------------------------------------------------------------

SESSION_DURATION_DAYS = 7
SESSION_IDLE_TIMEOUT_HOURS = 48  # session expires if not used for 48 hours

def create_session(user_id: str, jwt_payload: dict) -> dict:
    """Create a new server-side session. Returns the session row.
    The jwt_payload is stored as JSON (contains encrypted provider tokens)."""
    import secrets as _secrets
    import json as _json
    sid = _secrets.token_urlsafe(32)
    now = _iso_now()
    # compute expiry: 7 days from now
    import time as _time
    from datetime import datetime, timezone, timedelta
    exp_dt = datetime.now(timezone.utc) + timedelta(days=SESSION_DURATION_DAYS)
    expires_at = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write_lock:
        _db().execute(
            "INSERT INTO sessions (id, user_id, jwt_payload, created_at, "
            "expires_at, revoked, last_used_at) VALUES (?,?,?,?,?,?,?)",
            (sid, user_id, _json.dumps(jwt_payload), now, expires_at, 0, now))
        _db().commit()
    return get_session(sid) or {}


def get_session(session_id: str) -> dict | None:
    """Get a session by ID. Returns None if not found, expired, or revoked.
    Also checks the idle timeout (last_used_at must be within 48 hours)."""
    import json as _json
    row = _db().execute(
        "SELECT * FROM sessions WHERE id = ? AND revoked = 0",
        (session_id,)).fetchone()
    if not row:
        return None
    s = dict(row)
    # check absolute expiry
    import time as _time
    try:
        exp = _time.mktime(_time.strptime(s["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if _time.time() > exp:
            return None
    except Exception:
        return None
    # check idle timeout
    if s.get("last_used_at"):
        try:
            last = _time.mktime(_time.strptime(s["last_used_at"], "%Y-%m-%dT%H:%M:%SZ"))
            if _time.time() - last > SESSION_IDLE_TIMEOUT_HOURS * 3600:
                return None
        except Exception:
            pass
    # parse the JWT payload
    try:
        s["payload"] = _json.loads(s["jwt_payload"])
    except Exception:
        s["payload"] = {}
    return s


def touch_session(session_id: str) -> None:
    """Update last_used_at to now (called on every authenticated request)."""
    with _write_lock:
        _db().execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?",
            (_iso_now(), session_id))
        _db().commit()


def revoke_session(session_id: str) -> None:
    """Revoke a session (logout)."""
    with _write_lock:
        _db().execute(
            "UPDATE sessions SET revoked = 1 WHERE id = ?",
            (session_id,))
        _db().commit()


def revoke_all_user_sessions(user_id: str) -> int:
    """Revoke all sessions for a user (e.g., password change, security incident).
    Returns the number of sessions revoked."""
    with _write_lock:
        cur = _db().execute(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
            (user_id,))
        _db().commit()
        return cur.rowcount


def cleanup_expired_sessions() -> int:
    """Delete sessions that have expired. Call periodically (e.g., on each
    init_db or via a scheduler). Returns the number deleted."""
    now = _iso_now()
    with _write_lock:
        cur = _db().execute(
            "DELETE FROM sessions WHERE expires_at < ? OR revoked = 1",
            (now,))
        _db().commit()
        return cur.rowcount
