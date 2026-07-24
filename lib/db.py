"""SQLite database for GitHub integration: users, workspaces, push logs, registry.

All state is stored in a single ``doomalaysocreate.db`` file in the service's data directory.
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

DB_PATH = Path(os.environ.get("DOOMALAYSOCREATE_DB_PATH", "/data/doomalaysocreate.db"))

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
    db.executescript(SCHEMA)
    db.commit()
    # schema migrations for existing databases
    _migrate(db)


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
    # add 'zai' to conscious_agent tier CHECK constraint
    try:
        db.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE conscious_agent_v2 (
                id                TEXT PRIMARY KEY,
                conscious_id      TEXT NOT NULL REFERENCES conscious(id) ON DELETE CASCADE,
                role              TEXT NOT NULL,
                model             TEXT NOT NULL,
                tier              TEXT NOT NULL CHECK (tier IN ('claude','open')),
                status            TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle','running','waiting','done','failed')),
                worktree_path     TEXT,
                branch            TEXT,
                parent_agent_id   TEXT,
                subscribed_events TEXT NOT NULL DEFAULT '[]',
                is_orchestrator   INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO conscious_agent_v2 SELECT * FROM conscious_agent;
            DROP TABLE conscious_agent;
            ALTER TABLE conscious_agent_v2 RENAME TO conscious_agent;
            CREATE INDEX IF NOT EXISTS idx_conscious_agent_conscious ON conscious_agent(conscious_id);
            CREATE INDEX IF NOT EXISTS idx_conscious_agent_orchestrator ON conscious_agent(conscious_id, is_orchestrator);
            PRAGMA foreign_keys=ON;
        """)
    except sqlite3.OperationalError:
        pass
    # migrate sandbox_path from /tmp to /data for persist across Space restarts
    try:
        new_base = "/data/workspaces/"
        db.execute(
            "UPDATE workspaces SET sandbox_path = ? || substr(sandbox_path, 16) "
            "WHERE sandbox_path LIKE '/tmp/workspace/%'",
            (new_base,))
        db.execute(
            "UPDATE workspaces SET sandbox_path = ? || substr(sandbox_path, 18) "
            "WHERE sandbox_path LIKE '/tmp/workspaces/%'",
            (new_base,))
    except sqlite3.OperationalError:
        pass
    # Phase 4 — provider_keys table: encrypted-at-rest per-user provider API
    # keys (NVIDIA_API_KEY, CF_API_TOKEN, OPENROUTER_API_KEY, etc.). Each row
    # stores the key encrypted with AES-256-GCM (see crypto.encrypt_secret).
    # The Space secret mirror (set via huggingface_hub.HfApi.add_space_secret)
    # is the runtime source-of-truth (so the key is available as an env var
    # to the backend); this DB row is the durable backup + index for listing
    # which providers the user has configured.
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS provider_keys (
                user_id     TEXT NOT NULL,
                provider    TEXT NOT NULL,
                env_var     TEXT NOT NULL,
                key_enc     TEXT NOT NULL,
                extra       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, provider)
            );
            CREATE INDEX IF NOT EXISTS idx_provider_keys_user
                ON provider_keys(user_id);
        """)
    except sqlite3.OperationalError:
        pass
    # REDTEAM: migrate any plaintext values in users.github_token_encrypted /
    # users.hf_token_encrypted to AES-256-GCM. The legacy Fernet values still
    # decrypt transparently (decrypt_secret tries Fernet as a fallback), so
    # this is a soft migration — only plaintext values get re-encrypted.
    # Silently skips columns that don't exist or are already encrypted.
    try:
        _migrate_plaintext_user_tokens(db)
    except Exception:
        pass
    db.commit()


def _migrate_plaintext_user_tokens(db: sqlite3.Connection) -> None:
    """One-time migration: any users.github_token_encrypted or
    users.hf_token_encrypted value that is NOT already an encrypted blob
    (Fernet or v2: AES-GCM) gets encrypted in-place with AES-256-GCM.

    Idempotent: skips values that are already encrypted (``v2:`` prefix or
    Fernet ``gAAAA`` prefix). Silently skips if the columns don't exist.

    This is the REDTEAM fix for "keys currently plain text unencrypted" —
    a very old DB may have plaintext tokens in these columns (from before
    the Fernet helpers existed). This pass re-encrypts them. The new
    decrypt_secret() helper transparently handles Fernet and v2: formats,
    so callers don't notice the migration.
    """
    try:
        import crypto
    except ImportError:
        return
    with _write_lock:
        rows = db.execute(
            "SELECT id, github_token_encrypted, hf_token_encrypted, "
            "hf_refresh_token_encrypted FROM users"
        ).fetchall()
        for r in rows:
            uid = r["id"]
            for col in ("github_token_encrypted", "hf_token_encrypted",
                        "hf_refresh_token_encrypted"):
                val = r[col]
                if not val:
                    continue
                if crypto.is_encrypted(val):
                    continue
                # Plaintext (or unrecognised) — re-encrypt with AES-GCM.
                try:
                    enc = crypto.encrypt_secret(val)
                    db.execute(
                        f"UPDATE users SET {col} = ?, updated_at = ? WHERE id = ?",
                        (enc, _iso_now(), uid))
                except Exception:
                    # Best-effort — don't block startup on one bad row.
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

-- Chat sessions (persistent agent chat history)
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Chat',
    model TEXT,
    workspace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_events_session_seq ON chat_events(session_id, seq);
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


def find_user_workspace_by_repo(user_id: str, full_name: str) -> dict | None:
    """Find a user's workspace by GitHub repo full name (``owner/repo``).

    ``source_repo`` is stored as the clone URL (https://github.com/owner/repo.git
    or https://github.com/owner/repo). We match the trailing ``owner/repo``
    segment so callers can pass either the bare full name or the full clone URL.
    Returns the most recently modified matching workspace, or None.
    """
    if not full_name:
        return None
    # Normalise: accept "owner/repo", "owner/repo.git", full URLs, trailing slash.
    key = full_name.strip().rstrip("/")
    if key.endswith(".git"):
        key = key[:-4]
    if "/" in key:
        key = key.split("/")[-2] + "/" + key.split("/")[-1]
    rows = _db().execute(
        "SELECT * FROM workspaces WHERE user_id = ? ORDER BY last_modified DESC",
        (user_id,)).fetchall()
    for row in rows:
        src = (row["source_repo"] or "").strip().rstrip("/")
        if not src:
            continue
        if src.endswith(".git"):
            src = src[:-4]
        if "/" in src:
            src_norm = src.split("/")[-2] + "/" + src.split("/")[-1]
        else:
            src_norm = src
        if src_norm.lower() == key.lower():
            return dict(row)
    return None


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
# Chat sessions
# ---------------------------------------------------------------------------

def create_chat_session(*, title: str = "New Chat", model: str | None = None,
                        workspace_id: str | None = None) -> dict:
    db = _db()
    sid = _gen_id()
    now = _iso_now()
    with _write_lock:
        db.execute(
            "INSERT INTO chat_sessions (id, title, model, workspace_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, model, workspace_id, now, now))
        db.commit()
    return get_chat_session(sid)


def get_chat_session(session_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_chat_sessions(limit: int = 50) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_chat_session(session_id: str, **fields) -> dict | None:
    allowed = {"title", "model", "workspace_id"}
    updates = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        return get_chat_session(session_id)
    updates.append("updated_at = ?")
    params.append(_iso_now())
    params.append(session_id)
    with _write_lock:
        _db().execute(f"UPDATE chat_sessions SET {', '.join(updates)} WHERE id = ?", params)
        _db().commit()
    return get_chat_session(session_id)


def delete_chat_session(session_id: str) -> bool:
    with _write_lock:
        db = _db()
        cur = db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        db.commit()
        return cur.rowcount > 0


def append_chat_events(session_id: str, events: list[dict]) -> None:
    """Append events to a chat session, skipping events already stored by seq."""
    if not events:
        return
    db = _db()
    with _write_lock:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM chat_events WHERE session_id = ?",
            (session_id,)).fetchone()
        persisted_max = row[0] if row else -1
        insert_count = 0
        for ev in events:
            i = ev.get("i", -1)
            if i <= persisted_max:
                continue
            db.execute(
                "INSERT INTO chat_events (session_id, seq, event_type, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, i, ev.get("type", "unknown"), json.dumps(ev), _iso_now()))
            insert_count += 1
        if insert_count:
            db.commit()


def get_chat_events(session_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT content FROM chat_events WHERE session_id = ? ORDER BY seq ASC",
        (session_id,)).fetchall()
    return [json.loads(r["content"]) for r in rows]



# ---------------------------------------------------------------------------
# Provider API keys (encrypted at rest with AES-256-GCM)
# ---------------------------------------------------------------------------
# Each row stores a per-user provider API key (NVIDIA_API_KEY, CF_API_TOKEN,
# OPENROUTER_API_KEY, etc.). The key is encrypted with crypto.encrypt_secret
# before being written, and decrypted on read with crypto.decrypt_secret.
#
# The runtime source-of-truth for the BACKEND is the Space secret mirror
# (set via huggingface_hub.HfApi.add_space_secret so the key shows up as an
# env var when the backend boots). This DB row is the durable backup +
# the per-user index (so we can list which providers a user has configured
# without leaking the key values themselves).
#
# All functions are no-ops (return None / False / []) if the user_id is
# anonymous, and silently skip if the crypto module is unavailable.

# Allowlist of env vars a user may set via /api/keys. This is the SECURITY
# boundary: a user CANNOT set arbitrary env vars on their Space (which
# would let them overwrite JWT_SECRET, ENCRYPTION_KEY, etc.). The names
# here are the canonical env vars the providers_catalog.json reads.
PROVIDER_KEY_ALLOWLIST: dict[str, str] = {
    # provider_name -> env_var (the canonical env var the backend reads)
    "nvidia":         "NVIDIA_API_KEY",
    "cloudflare":     "CF_API_TOKEN",
    "openrouter":     "OPENROUTER_API_KEY",
    "github-models":  "GITHUB_TOKEN",
    "opencode-zen":   "OPENCODE_ZEN_API_KEY",
    "opencode-go":    "OPENCODE_GO_API_KEY",
    "privatemodeai":  "PRIVATEMODEAI_API_KEY",
    "anthropic":      "ANTHROPIC_API_KEY",
    "tavily":         "TAVILY_API_KEY",
    # cloudflare also needs CF_ACCOUNT_ID (set as `extra` on the cloudflare
    # provider row; not a separate provider entry).
}

# Reverse lookup: env_var -> provider_name (1:1, except GITHUB_TOKEN which
# also accepts GH_TOKEN — handled in resolve_provider).
_ENV_VAR_TO_PROVIDER: dict[str, str] = {v: k for k, v in PROVIDER_KEY_ALLOWLIST.items()}


def resolve_provider(provider: str) -> str | None:
    """Normalise a provider name (case-insensitive, accepts aliases like
    'github' for 'github-models'). Returns the canonical provider name
    from PROVIDER_KEY_ALLOWLIST, or None if not recognised."""
    if not provider:
        return None
    p = provider.strip().lower()
    aliases = {
        "github": "github-models",
        "github_models": "github-models",
        "gh": "github-models",
        "cf": "cloudflare",
        "workersai": "cloudflare",
        "workers-ai": "cloudflare",
        "or": "openrouter",
        "opencode": "opencode-zen",
        "zen": "opencode-zen",
        "pmai": "privatemodeai",
        "privatemode": "privatemodeai",
    }
    p = aliases.get(p, p)
    if p in PROVIDER_KEY_ALLOWLIST:
        return p
    return None


def env_var_for_provider(provider: str) -> str | None:
    """Return the canonical env var for a provider, or None."""
    p = resolve_provider(provider)
    if not p:
        return None
    return PROVIDER_KEY_ALLOWLIST[p]


def set_provider_key(*, user_id: str, provider: str, key: str,
                     extra: dict | None = None) -> dict | None:
    """Encrypt + store a provider API key for a user.

    ``provider`` is the canonical provider name (nvidia, cloudflare, ...).
    ``key`` is the plaintext API key (encrypted before storage).
    ``extra`` is an optional dict of extra env vars to set alongside the
    key (e.g. cloudflare needs CF_ACCOUNT_ID). The dict's KEYS must be in
    the allowlist below; values are stored encrypted.

    Returns the stored row (without the key value) or None on bad input.
    """
    if not user_id or not key:
        return None
    p = resolve_provider(provider)
    if not p:
        return None
    env_var = PROVIDER_KEY_ALLOWLIST[p]
    try:
        import crypto
        key_enc = crypto.encrypt_secret(key)
    except Exception:
        return None
    # `extra` allowlist: only env vars we recognise as safe sidecars for
    # this provider. Currently only CF_ACCOUNT_ID is allowed (for cloudflare).
    extra_safe: dict[str, str] = {}
    if extra and isinstance(extra, dict):
        for k, v in extra.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if k in ("CF_ACCOUNT_ID",) and v.strip():
                try:
                    import crypto
                    extra_safe[k] = crypto.encrypt_secret(v.strip())
                except Exception:
                    pass
    import json as _json
    extra_json = _json.dumps(extra_safe) if extra_safe else None
    db = _db()
    now = _iso_now()
    with _write_lock:
        db.execute(
            "INSERT OR REPLACE INTO provider_keys "
            "(user_id, provider, env_var, key_enc, extra, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, p, env_var, key_enc, extra_json, now, now))
        db.commit()
    return {"user_id": user_id, "provider": p, "env_var": env_var,
            "has_key": True, "has_extra": bool(extra_json),
            "updated_at": now}


def get_provider_key(user_id: str, provider: str) -> dict | None:
    """Fetch + decrypt a provider API key for a user. Returns
    {provider, env_var, key, extra} or None if not set.

    SECURITY: this is the ONLY function that returns the decrypted key
    value. The list_provider_keys() helper returns only ``has_key`` flags
    so we never leak key values in bulk listings."""
    if not user_id:
        return None
    p = resolve_provider(provider)
    if not p:
        return None
    row = _db().execute(
        "SELECT * FROM provider_keys WHERE user_id = ? AND provider = ?",
        (user_id, p)).fetchone()
    if not row:
        return None
    try:
        import crypto
        key = crypto.decrypt_secret(row["key_enc"])
    except Exception:
        return None
    extra: dict[str, str] = {}
    if row["extra"]:
        try:
            import json as _json
            raw = _json.loads(row["extra"])
            for k, v in raw.items():
                try:
                    extra[k] = crypto.decrypt_secret(v)
                except Exception:
                    pass
        except Exception:
            pass
    return {"provider": p, "env_var": row["env_var"], "key": key,
            "extra": extra, "updated_at": row["updated_at"]}


def list_provider_keys(user_id: str) -> dict[str, dict]:
    """List which providers a user has keys for. Returns
    {provider_name: {env_var, has_key, has_extra, updated_at}} — NEVER
    includes the decrypted key value (security: the frontend only needs
    to know IF a key is set, not what it is)."""
    if not user_id:
        return {}
    rows = _db().execute(
        "SELECT * FROM provider_keys WHERE user_id = ? ORDER BY provider",
        (user_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["provider"]] = {
            "env_var": r["env_var"],
            "has_key": True,
            "has_extra": bool(r["extra"]),
            "updated_at": r["updated_at"],
        }
    return out


def delete_provider_key(user_id: str, provider: str) -> bool:
    """Delete a provider API key. Returns True if a row was deleted."""
    if not user_id:
        return False
    p = resolve_provider(provider)
    if not p:
        return False
    with _write_lock:
        cur = _db().execute(
            "DELETE FROM provider_keys WHERE user_id = ? AND provider = ?",
            (user_id, p))
        _db().commit()
        return cur.rowcount > 0



# ---------------------------------------------------------------------------
# HF Dataset DB sync — survives Space restarts on the free tier (no /data)
# ---------------------------------------------------------------------------
# The HF Space free tier has NO persistent storage: /data is wiped on every
# restart (sleep -> wake, rebuild, login). The SQLite DB at /data/doomalaysocreate.db
# holds ALL chat history (chat_sessions, chat_events), per-user workspaces,
# provider keys, and conscious agents -- losing it means users see "blank
# chat sidebar" after every Space wake-up.
#
# The HF Dataset `ScoobyBaby1999/doomalaysocreate-metrics-public` (the same
# one already used for the public template library) IS persistent -- files
# committed to it survive forever. We mirror the DB binary there as
# `doomalaysocreate.db` so the next boot can restore the conversation history.
#
# Throttle: uploads are batched to at most one every 30s. A burst of chat
# messages triggers a sync on the first message and again 30s later if more
# arrive in between. On boot, restore_db_from_dataset() downloads the file
# and replaces /data/doomalaysocreate.db if the dataset version is newer.

# The dataset repo to sync the DB to (same one used for templates + metrics).
DB_SYNC_DATASET_REPO = os.environ.get(
    "DOOMALAYSOCREATE_DB_DATASET",
    "ScoobyBaby1999/doomalaysocreate-metrics-public",
).strip()

# Path inside the dataset repo where the DB binary lives.
DB_SYNC_PATH_IN_REPO = "doomalaysocreate.db"

# Minimum seconds between sync uploads (throttle so we don't hammer HF).
DB_SYNC_MIN_INTERVAL_S = float(os.environ.get("DOOMALAYSOCREATE_DB_SYNC_INTERVAL", "30"))

# Internal state -- last sync time + a daemon thread that batches uploads.
_db_sync_lock = threading.Lock()
_db_sync_last_upload_ts: float = 0.0
_db_sync_pending = False
_db_sync_thread: threading.Thread | None = None
_db_sync_wakeup = threading.Event()


def _hf_token_for_db_sync() -> str:
    """Resolve the HF token to use for DB sync. Prefers HF_TOKEN, then
    HUGGINGFACE_TOKEN, then any per-user token configured in the users
    table (last resort -- used when the Space has no env-var token but a
    user has HF-linked their account). Empty string if no token found."""
    tok = (os.environ.get("HF_TOKEN", "")
           or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()
    if tok:
        return tok
    # Best-effort: scan users table for any HF token (used on duped Spaces
    # where the operator hasn't set HF_TOKEN but a user has HF-linked).
    try:
        import crypto  # local import -- only needed for the fallback path
        db = _db()
        rows = db.execute(
            "SELECT hf_token_encrypted FROM users "
            "WHERE hf_token_encrypted IS NOT NULL "
            "AND hf_token_encrypted != '' LIMIT 1").fetchall()
        for r in rows:
            enc = r["hf_token_encrypted"]
            try:
                plain = crypto.decrypt_secret(enc)
                if plain:
                    return plain.strip()
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _hf_available_for_db_sync() -> bool:
    """True iff we have a token AND the huggingface_hub library is importable."""
    if not _hf_token_for_db_sync():
        return False
    try:
        import huggingface_hub  # noqa: F401
        return True
    except ImportError:
        return False


def restore_db_from_dataset() -> bool:
    """Download the DB from the HF dataset on boot.

    Replaces /data/doomalaysocreate.db IFF the dataset copy is newer than
    the local copy (compared by mtime). Called once at startup BEFORE
    ``init_db()`` opens the SQLite connection so the restored file is the
    one SQLite sees.

    Returns True if the DB was restored, False otherwise (no copy on the
    dataset, network error, local copy is newer, etc.). Never raises.
    """
    if not _hf_available_for_db_sync():
        return False
    token = _hf_token_for_db_sync()
    try:
        from huggingface_hub import hf_hub_download, HfApi
        from huggingface_hub.utils import (
            RepositoryNotFoundError,
            RevisionNotFoundError,
            EntryNotFoundError,
        )
    except ImportError:
        return False
    # Ensure the dataset repo exists (creates it public if missing).
    api = HfApi(token=token)
    try:
        api.create_repo(repo_id=DB_SYNC_DATASET_REPO, repo_type="dataset",
                        private=False, exist_ok=True)
    except Exception:
        # Network error / rate limit -- proceed to attempt download anyway.
        pass
    # Download the DB binary. On a fresh boot /data is wiped so the local
    # DB doesn't exist -- the downloaded copy always wins.
    try:
        downloaded_path = hf_hub_download(
            repo_id=DB_SYNC_DATASET_REPO,
            filename=DB_SYNC_PATH_IN_REPO,
            repo_type="dataset",
            token=token,
        )
    except (RepositoryNotFoundError, RevisionNotFoundError, EntryNotFoundError):
        # First boot -- dataset or file doesn't exist yet. Nothing to restore.
        return False
    except Exception:
        # Network error -- non-fatal, proceed with local DB.
        return False
    if not downloaded_path or not os.path.exists(downloaded_path):
        return False
    # Compare mtimes. If the dataset version is newer (or local doesn't
    # exist), replace the local DB. We close any open SQLite connection
    # first so the file can be overwritten cleanly.
    try:
        remote_mtime = os.path.getmtime(downloaded_path)
    except OSError:
        remote_mtime = 0.0
    local_exists = DB_PATH.exists()
    if local_exists:
        try:
            local_mtime = os.path.getmtime(DB_PATH)
        except OSError:
            local_mtime = 0.0
        # If local is newer (e.g. a quick sleep/wake cycle where the in-
        # memory state was just flushed), keep local. Use a 1s epsilon to
        # avoid floating-point tie issues.
        if local_mtime > remote_mtime + 1.0:
            return False
    # Close any open connection so we can overwrite the file.
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(downloaded_path, DB_PATH)
        try:
            print(f"[db] restored {DB_PATH} from HF dataset "
                  f"({os.path.getsize(DB_PATH)} bytes)", flush=True)
        except Exception:
            pass
        return True
    except Exception:
        return False


def sync_db_to_dataset(force: bool = False) -> bool:
    """Queue a DB sync to the HF dataset. Throttled to at most one upload
    every ``DB_SYNC_MIN_INTERVAL_S`` seconds (default 30s).

    Returns True if the sync was queued (or completed synchronously if
    the throttle window has elapsed), False if HF is unavailable or the
    upload failed. Safe to call from any thread (e.g. after every chat
    message -- the throttle batches them).

    Pass ``force=True`` to bypass the throttle (used by flush_db_sync()
    on SIGTERM for a final flush).
    """
    global _db_sync_last_upload_ts, _db_sync_pending, _db_sync_thread
    if not _hf_available_for_db_sync():
        return False
    if not DB_PATH.exists():
        return False
    now = time.time()
    with _db_sync_lock:
        if not force and (now - _db_sync_last_upload_ts) < DB_SYNC_MIN_INTERVAL_S:
            # Throttled -- mark a pending sync and ensure the worker wakes
            # up after the throttle window elapses.
            _db_sync_pending = True
            _db_sync_wakeup.set()
            _ensure_db_sync_worker()
            return True
        # We're going to upload now -- clear the pending flag and reset the
        # timer under the lock so concurrent callers don't double-upload.
        _db_sync_pending = False
        _db_sync_last_upload_ts = now
    # Do the actual upload OUTSIDE the lock so concurrent callers don't
    # block on the network round-trip (which can take 5-30s for a multi-MB
    # DB file). The lock above only guards the throttle decision.
    return _do_db_sync_upload()


def _ensure_db_sync_worker() -> None:
    """Start the daemon worker that flushes the pending sync when the
    throttle window elapses. Idempotent -- only starts one thread."""
    global _db_sync_thread
    if _db_sync_thread is not None and _db_sync_thread.is_alive():
        return
    _db_sync_thread = threading.Thread(target=_db_sync_worker_loop,
                                       daemon=True,
                                       name="db-sync-worker")
    _db_sync_thread.start()


def _db_sync_worker_loop() -> None:
    """Background loop that flushes pending syncs after the throttle window.

    Wakes up when ``_db_sync_wakeup`` is set (by a throttled caller), waits
    out the remaining throttle window, then uploads. Keeps looping until
    there are no more pending syncs."""
    while True:
        # Wait for someone to signal a pending sync.
        _db_sync_wakeup.wait(timeout=60.0)
        _db_sync_wakeup.clear()
        # Drain pending syncs in a loop -- each iteration waits for the
        # throttle window to elapse, then uploads.
        while True:
            wait_s = 0.0
            with _db_sync_lock:
                if not _db_sync_pending:
                    break
                now = time.time()
                wait_s = (_db_sync_last_upload_ts + DB_SYNC_MIN_INTERVAL_S) - now
                if wait_s <= 0:
                    # Window elapsed -- upload now.
                    _db_sync_pending = False
                    _db_sync_last_upload_ts = now
            if wait_s > 0:
                # Sleep outside the lock so other callers can mark more
                # pending syncs while we wait (they'll coalesce into one
                # upload when the window elapses).
                time.sleep(min(wait_s, 5.0))
                continue
            # Window elapsed -- upload.
            _do_db_sync_upload()


def _do_db_sync_upload() -> bool:
    """Actually upload the DB file to the HF dataset. Network round-trip
    happens here. Best-effort: logs failures, never raises."""
    if not _hf_available_for_db_sync():
        return False
    if not DB_PATH.exists():
        return False
    token = _hf_token_for_db_sync()
    try:
        from huggingface_hub import HfApi, upload_file
        from huggingface_hub.utils import HfHubHTTPError
    except ImportError:
        return False
    api = HfApi(token=token)
    try:
        api.create_repo(repo_id=DB_SYNC_DATASET_REPO, repo_type="dataset",
                        private=False, exist_ok=True)
    except Exception:
        pass
    # Read the DB file as binary and upload. We use upload_file with
    # path_or_fileobj=bytes so we don't need to worry about file handles
    # or SQLite WAL mode locking the file. The bytes snapshot is atomic.
    try:
        with open(DB_PATH, "rb") as f:
            db_bytes = f.read()
    except Exception:
        return False
    try:
        upload_file(
            path_or_fileobj=db_bytes,
            path_in_repo=DB_SYNC_PATH_IN_REPO,
            repo_id=DB_SYNC_DATASET_REPO,
            repo_type="dataset",
            token=token,
            commit_message="auto-sync doomalaysocreate.db",
        )
        return True
    except HfHubHTTPError as exc:
        try:
            print(f"[db] sync upload failed: {type(exc).__name__}: "
                  f"{str(exc)[:200]}", flush=True)
        except Exception:
            pass
        return False
    except Exception:
        return False


def flush_db_sync() -> None:
    """Force-flush any pending DB sync. Called on SIGTERM shutdown so the
    very last message a user sent before the Space slept isn't lost."""
    try:
        sync_db_to_dataset(force=True)
    except Exception:
        pass
