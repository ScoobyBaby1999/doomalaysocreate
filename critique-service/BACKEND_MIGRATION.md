# Backend Migration Guide — Chat Session Security & Model Fix

## Overview

This document describes the exact changes needed on the backend (`c` branch) to support the redesigned chat UI. The main issues being fixed:

1. **Chat sessions not scoped to users** — anyone can see/delete anyone's chats
2. **Model selection not working** — logical model IDs from the frontend don't resolve to physical slots
3. **Missing event persistence endpoint** — needed for reliable chat history

## Changes Required

### 1. `db.py` — Add user_id to chat_sessions

Add migration function after `_migrate()`:

```python
def _migrate_chat_sessions_user_id(db: sqlite3.Connection) -> None:
    """Add user_id column to chat_sessions if it doesn't exist."""
    try:
        db.execute("ALTER TABLE chat_sessions ADD COLUMN user_id TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
```

Call it in `init_db()`:
```python
def init_db() -> None:
    db = _db()
    db.executescript(SCHEMA)
    db.commit()
    _migrate(db)
    _migrate_chat_sessions_user_id(db)  # NEW
```

Update `create_chat_session`:
```python
def create_chat_session(*, title: str = "New Chat", model: str | None = None,
                        workspace_id: str | None = None,
                        user_id: str | None = None) -> dict:
    db = _db()
    sid = _gen_id()
    now = _iso_now()
    with _write_lock:
        db.execute(
            "INSERT INTO chat_sessions (id, title, model, workspace_id, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, title, model, workspace_id, user_id, now, now))
        db.commit()
    return get_chat_session(sid)
```

Update `list_chat_sessions`:
```python
def list_chat_sessions(user_id: str | None = None, limit: int = 50) -> list[dict]:
    db = _db()
    if user_id:
        rows = db.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? OR user_id IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM chat_sessions WHERE user_id IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
```

Update `append_chat_events` (add session timestamp update):
```python
        if insert_count:
            # Update the session's updated_at timestamp
            db.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (_iso_now(), session_id))
            db.commit()
```

### 2. `critique_service.py` — Secure chat handlers

Replace all chat session handlers to require user authentication:

**`_handle_chat_sessions_list`:**
```python
def _handle_chat_sessions_list(self) -> None:
    user_id = self._require_user_from_jwt()
    sessions = db.list_chat_sessions(user_id=user_id)
    self._send_json(200, {"sessions": sessions})
```

**`_handle_chat_session_get`:**
```python
def _handle_chat_session_get(self, session_id: str) -> None:
    user_id = self._require_user_from_jwt()
    cs = db.get_chat_session(session_id)
    if not cs:
        self._send_json(404, {"error": "chat session not found"})
        return
    if cs.get("user_id") and cs["user_id"] != user_id:
        self._send_json(403, {"error": "not your session"})
        return
    self._send_json(200, cs)
```

**`_handle_chat_session_events`:**
```python
def _handle_chat_session_events(self, session_id: str) -> None:
    user_id = self._require_user_from_jwt()
    cs = db.get_chat_session(session_id)
    if not cs:
        self._send_json(404, {"error": "chat session not found"})
        return
    if cs.get("user_id") and cs["user_id"] != user_id:
        self._send_json(403, {"error": "not your session"})
        return
    events = db.get_chat_events(session_id)
    self._send_json(200, {"session_id": session_id, "events": events})
```

**`_handle_chat_session_create`:**
```python
def _handle_chat_session_create(self, payload: dict) -> None:
    title = str(payload.get("title", "New Chat")).strip() or "New Chat"
    model = str(payload.get("model", "")).strip() or None
    workspace_id = str(payload.get("workspace_id", "")).strip() or None
    user_id = self._require_user_from_jwt()
    cs = db.create_chat_session(title=title, model=model, workspace_id=workspace_id, user_id=user_id)
    self._send_json(201, cs)
```

**`_handle_chat_session_update`:**
```python
def _handle_chat_session_update(self, session_id: str, payload: dict) -> None:
    user_id = self._require_user_from_jwt()
    cs = db.get_chat_session(session_id)
    if not cs:
        self._send_json(404, {"error": "chat session not found"})
        return
    if cs.get("user_id") and cs["user_id"] != user_id:
        self._send_json(403, {"error": "not your session"})
        return
    fields = {}
    if "title" in payload:
        title = str(payload["title"]).strip()
        if title:
            fields["title"] = title
    if "model" in payload:
        fields["model"] = str(payload["model"]).strip() or None
    updated = db.update_chat_session(session_id, **fields)
    if not updated:
        self._send_json(404, {"error": "chat session not found"})
        return
    self._send_json(200, updated)
```

**`_handle_chat_session_delete`:**
```python
def _handle_chat_session_delete(self, session_id: str) -> None:
    user_id = self._require_user_from_jwt()
    cs = db.get_chat_session(session_id)
    if not cs:
        self._send_json(404, {"error": "chat session not found"})
        return
    if cs.get("user_id") and cs["user_id"] != user_id:
        self._send_json(403, {"error": "not your session"})
        return
    ok = db.delete_chat_session(session_id)
    if not ok:
        self._send_json(404, {"error": "chat session not found"})
        return
    self._send_json(200, {"deleted": session_id})
```

### 3. Fix model resolution in `_handle_agent_post`

Replace the model resolution block:
```python
        model = payload.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        if model:
            resolved = None
            for m in agent_sessions.agent_models():
                if (m["model"] == model or 
                    m["model"].split("/")[-1] == model or
                    m["model"].endswith("/" + model)):
                    resolved = m["model"]
                    break
            # If not found as physical slot, try resolving via panel's logical models
            if not resolved and "/" not in model:
                panel = self.server.panel
                spec = panel.logical_models.get(model)
                if spec and spec.get("candidates"):
                    for cand in spec["candidates"]:
                        provider = cand.get("provider", "")
                        model_id = cand.get("model", "")
                        if provider and model_id:
                            resolved = f"{provider}/{model_id}"
                            break
            if resolved:
                model = resolved
            # If still not resolved, let backend try - don't fail
```

### 4. Pass user_id when auto-creating chat sessions

In `_handle_agent_post`, find:
```python
        if not session_id and not chat_session_id:
            cs = db.create_chat_session(model=model, workspace_id=workspace_id)
            chat_session_id = cs["id"]
```

Replace with:
```python
        if not session_id and not chat_session_id:
            user_id = self._require_user_from_jwt()
            cs = db.create_chat_session(model=model, workspace_id=workspace_id, user_id=user_id)
            chat_session_id = cs["id"]
```

### 5. Add persist endpoint

Add in `do_POST` chat session section:
```python
        if route.startswith("/api/chat/sessions/") and route.endswith("/persist"):
            if not self._auth_ok():
                self._send_json(401, {"error": "missing or invalid bearer token"})
                return
            sid = route[len("/api/chat/sessions/"):-len("/persist")]
            payload = self._read_json_body()
            if payload is None:
                return
            user_id = self._require_user_from_jwt()
            cs = db.get_chat_session(sid)
            if not cs:
                self._send_json(404, {"error": "chat session not found"})
                return
            if cs.get("user_id") and cs["user_id"] != user_id:
                self._send_json(403, {"error": "not your session"})
                return
            events = payload.get("events", [])
            if events:
                db.append_chat_events(sid, events)
            self._send_json(200, {"persisted": len(events)})
            return
```

## Summary

| Issue | Fix |
|-------|-----|
| Sessions visible to all users | Add `user_id` column, filter all queries |
| Model select not working | Resolve logical IDs via `panel.logical_models` |
| Events lost on refresh | Add `/persist` endpoint for delta-sync |
| Sessions disappear | User-scoped sessions + proper auth checks |
