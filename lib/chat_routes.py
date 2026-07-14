"""Chat session persistence — self-contained CRUD + events store.

Provides the /api/chat/sessions/* family of endpoints that the frontend
AgentChat relies on for crash-safe multi-conversation history.

Storage: SQLite via the existing db._db() connection (same DB file as the
rest of the service). Schema is created idempotently on first request.

Auth: same bearer token gate as the rest of /api/agent. Identity
(GitHub user_id) is read from the X-JWT header via the request handler's
_require_user_from_jwt() — if absent, sessions are stored as anonymous
(user_id IS NULL) so the chat panel works even without GitHub connected.

Wired into critique_service.py via handle_request(method, path, headers, body, handler).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

# Reuse the existing DB connection helpers (same pattern as conscious_db.py).
# db.py is intentionally not committed but is present on the deploy target.
import db as _dbmod

_write_lock = _dbmod._write_lock


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Schema (idempotent — safe to call on every request)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT 'New Chat',
    model        TEXT,
    workspace_id TEXT,
    user_id      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_events (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chat_events_session ON chat_events(session_id, seq);
"""


def _ensure_schema() -> None:
    db = _dbmod._db()
    db.executescript(SCHEMA)
    db.commit()


def _migrate_user_id() -> None:
    """Add user_id column to legacy chat_sessions tables (one-time)."""
    db = _dbmod._db()
    try:
        db.execute("ALTER TABLE chat_sessions ADD COLUMN user_id TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


_schema_initialized = False
_schema_lock = threading.Lock()


def _ensure_schema_once() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if not _schema_initialized:
            _ensure_schema()
            _migrate_user_id()
            _schema_initialized = True


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_chat_session(*, title: str = "New Chat", model: str | None = None,
                        workspace_id: str | None = None,
                        user_id: str | None = None) -> dict:
    _ensure_schema_once()
    db = _dbmod._db()
    sid = _gen_id()
    now = _iso_now()
    with _write_lock:
        db.execute(
            "INSERT INTO chat_sessions (id, title, model, workspace_id, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, title, model, workspace_id, user_id, now, now))
        db.commit()
    return get_chat_session(sid) or {
        "id": sid, "title": title, "model": model,
        "workspace_id": workspace_id, "user_id": user_id,
        "created_at": now, "updated_at": now,
    }


def get_chat_session(session_id: str) -> dict | None:
    _ensure_schema_once()
    row = _dbmod._db().execute(
        "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_chat_sessions(user_id: str | None = None, limit: int = 100) -> list[dict]:
    _ensure_schema_once()
    db = _dbmod._db()
    if user_id:
        rows = db.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? OR user_id IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_chat_session(session_id: str, *, title: str | None = None,
                        model: str | None = None,
                        workspace_id: str | None = None) -> dict | None:
    _ensure_schema_once()
    fields: list[str] = []
    params: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if workspace_id is not None:
        fields.append("workspace_id = ?")
        params.append(workspace_id)
    if not fields:
        return get_chat_session(session_id)
    fields.append("updated_at = ?")
    params.append(_iso_now())
    params.append(session_id)
    with _write_lock:
        _dbmod._db().execute(
            f"UPDATE chat_sessions SET {', '.join(fields)} WHERE id = ?", params)
        _dbmod._db().commit()
    return get_chat_session(session_id)


def delete_chat_session(session_id: str) -> bool:
    _ensure_schema_once()
    with _write_lock:
        db = _dbmod._db()
        cur = db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        db.execute("DELETE FROM chat_events WHERE session_id = ?", (session_id,))
        db.commit()
        return cur.rowcount > 0


def append_chat_events(session_id: str, events: list[dict]) -> int:
    """Append events to a chat session, skipping already-stored seq numbers.

    Returns the number of newly inserted events.
    """
    _ensure_schema_once()
    if not events:
        return 0
    db = _dbmod._db()
    inserted = 0
    with _write_lock:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM chat_events WHERE session_id = ?",
            (session_id,)).fetchone()
        persisted_max = row[0] if row else -1
        for ev in events:
            i = ev.get("i", -1)
            if i == -1 or i <= persisted_max:
                # Use the next available seq if event has no seq
                i = persisted_max + 1
            if i <= persisted_max:
                continue
            db.execute(
                "INSERT OR REPLACE INTO chat_events (session_id, seq, event_type, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, i, ev.get("type", "unknown"),
                 json.dumps(ev, ensure_ascii=False), _iso_now()))
            persisted_max = i
            inserted += 1
        if inserted:
            db.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (_iso_now(), session_id))
            db.commit()
    return inserted


def get_chat_events(session_id: str) -> list[dict]:
    _ensure_schema_once()
    rows = _dbmod._db().execute(
        "SELECT content FROM chat_events WHERE session_id = ? ORDER BY seq ASC",
        (session_id,)).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["content"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------
# Auto-title from first user message
# ---------------------------------------------------------------------------

def _auto_title(text: str, max_len: int = 48) -> str:
    s = " ".join(text.split())
    if len(s) <= max_len:
        return s or "New Chat"
    return s[: max_len - 1].rstrip() + "\u2026"


def maybe_set_title(session_id: str, events: list[dict]) -> None:
    """If the session still has the default title, derive one from the first
    user event in the batch."""
    cs = get_chat_session(session_id)
    if not cs:
        return
    if cs.get("title") and cs["title"] != "New Chat":
        return
    for ev in events:
        if ev.get("type") == "user" and ev.get("text"):
            update_chat_session(session_id, title=_auto_title(ev["text"]))
            return


# ---------------------------------------------------------------------------
# HTTP dispatch — called from critique_service.Handler
# ---------------------------------------------------------------------------

def _user_id_from_handler(handler) -> str | None:
    """Extract GitHub user_id from the request handler (X-JWT or fallback)."""
    try:
        return handler._require_user_from_jwt()
    except Exception:
        return None


def _json(handler, status: int, body: dict) -> None:
    handler._send_json(status, body)


def _ok_session(handler, cs: dict | None, status: int = 200) -> None:
    if cs is None:
        _json(handler, 404, {"error": "chat session not found"})
        return
    _json(handler, status, cs)


def _check_ownership(handler, cs: dict | None, user_id: str | None) -> bool:
    """Returns True if access is allowed (owner or anonymous/legacy session)."""
    if cs is None:
        _json(handler, 404, {"error": "chat session not found"})
        return False
    sid_user = cs.get("user_id")
    # Allow access if session has no owner (legacy) or owner is this user.
    if sid_user and user_id and sid_user != user_id:
        _json(handler, 403, {"error": "not your session"})
        return False
    return True


def handle_request(method: str, path: str, body: dict, handler) -> bool:
    """Dispatch chat session routes. Returns True if handled."""
    from urllib.parse import urlsplit
    route = urlsplit(path).path.rstrip("/")

    # Only handle /api/chat/sessions* routes
    if route != "/api/chat/sessions" and not route.startswith("/api/chat/sessions/"):
        return False

    # Auth: same bearer gate as /api/agent. Identity optional (X-JWT).
    if not handler._auth_ok():
        _json(handler, 401, {"error": "missing or invalid bearer token"})
        return True

    user_id = _user_id_from_handler(handler)

    # -----------------------------------------------------------------
    # GET routes
    # -----------------------------------------------------------------
    if method == "GET":
        if route == "/api/chat/sessions":
            sessions = list_chat_sessions(user_id=user_id)
            _json(handler, 200, {"sessions": sessions})
            return True
        # /api/chat/sessions/<id>
        if route.startswith("/api/chat/sessions/"):
            parts = route[len("/api/chat/sessions/"):].split("/")
            sid = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            cs = get_chat_session(sid)
            if not _check_ownership(handler, cs, user_id):
                return True
            if sub == "events":
                events = get_chat_events(sid)
                _json(handler, 200, {"session_id": sid, "events": events})
                return True
            if sub == "":
                _ok_session(handler, cs)
                return True
            _json(handler, 404, {"error": "unknown sub-route"})
            return True

    # -----------------------------------------------------------------
    # POST routes
    # -----------------------------------------------------------------
    if method == "POST":
        if route == "/api/chat/sessions":
            title = str(body.get("title", "New Chat")).strip() or "New Chat"
            model = str(body.get("model", "")).strip() or None
            workspace_id = str(body.get("workspace_id", "")).strip() or None
            cs = create_chat_session(title=title, model=model,
                                     workspace_id=workspace_id, user_id=user_id)
            _ok_session(handler, cs, status=201)
            return True
        if route.startswith("/api/chat/sessions/"):
            parts = route[len("/api/chat/sessions/"):].split("/")
            sid = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            cs = get_chat_session(sid)
            if not _check_ownership(handler, cs, user_id):
                return True
            if sub == "update":
                fields: dict[str, Any] = {}
                if "title" in body:
                    t = str(body["title"]).strip()
                    if t:
                        fields["title"] = t
                if "model" in body:
                    fields["model"] = str(body["model"]).strip() or None
                if "workspace_id" in body:
                    fields["workspace_id"] = str(body["workspace_id"]).strip() or None
                updated = update_chat_session(sid, **fields)
                _ok_session(handler, updated)
                return True
            if sub == "persist":
                events = body.get("events", [])
                if not isinstance(events, list):
                    _json(handler, 400, {"error": "'events' must be a list"})
                    return True
                # Filter to events that have a type — these are the only ones
                # worth persisting (skip ephemeral delta events).
                persistable = [e for e in events if isinstance(e, dict) and e.get("type")]
                # Strip deltas — keep only terminal forms (assistant, thinking,
                # tool_use, tool_result, status, panel, user). The frontend
                # already coalesces deltas into a single assistant message
                # before persisting, but be defensive.
                persistable = [e for e in persistable if e.get("type") not in
                               ("assistant_delta", "thinking_delta")]
                count = append_chat_events(sid, persistable)
                maybe_set_title(sid, persistable)
                _json(handler, 200, {"persisted": count})
                return True
        _json(handler, 404, {"error": "unknown POST route"})
        return True

    # -----------------------------------------------------------------
    # DELETE routes
    # -----------------------------------------------------------------
    if method == "DELETE":
        if route.startswith("/api/chat/sessions/"):
            sid = route[len("/api/chat/sessions/"):]
            if "/" in sid:
                _json(handler, 404, {"error": "unknown route"})
                return True
            cs = get_chat_session(sid)
            if not _check_ownership(handler, cs, user_id):
                return True
            ok = delete_chat_session(sid)
            if not ok:
                _json(handler, 404, {"error": "chat session not found"})
                return True
            _json(handler, 200, {"deleted": sid})
            return True

    _json(handler, 405, {"error": f"method {method} not allowed"})
    return True
