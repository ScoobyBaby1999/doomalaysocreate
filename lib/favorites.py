"""Shared heart/favorite/download system for templates AND workspaces.

Both templates and workspaces use this single module to:
  * track per-user hearts (favorites) in SQLite
  * track per-user downloads in SQLite
  * filter by favorites
  * provide aggregate counts for public display

Why a shared module?
--------------------
The user said: "The template and workspace system should share a lot of
functionality so you can build with both of them in mind. They both need
some method of having heart ... Downloaded repos/templates should persist
locally."

This module replaces the older per-domain tables (``template_hearts``,
``template_downloads``) with two generic, polymorphic tables keyed on
``(user_id, item_type, item_id)`` where ``item_type`` is one of
``"template" | "workspace"``. The per-user tracking stays in local
SQLite (so it survives Space restarts via the existing
dataset_persistence.upload_db() loop). Aggregate / global counts live in
the public HF Dataset (see ``public_dataset.py``).

Schema (created idempotently on first call)::

    CREATE TABLE IF NOT EXISTS favorites (
        user_id     TEXT NOT NULL,
        item_type   TEXT NOT NULL,   -- 'template' | 'workspace'
        item_id     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, item_type, item_id)
    );

    CREATE TABLE IF NOT EXISTS downloads (
        user_id     TEXT NOT NULL,
        item_type   TEXT NOT NULL,
        item_id     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, item_type, item_id)
    );

Backward compatibility: the legacy ``template_hearts`` /
``template_downloads`` tables are NOT migrated automatically — the
old per-template counts (the ``hearts`` / ``downloads`` columns on the
``templates`` table) keep their aggregate values. New heart/download
events from this module simply flow into the new generic tables AND
the public dataset's aggregate counters (see ``public_dataset.py``).

This module is thread-safe (uses ``db._write_lock`` for writes).
"""
from __future__ import annotations

import threading
import time

import db as _dbmod

_write_lock = _dbmod._write_lock

# Valid item types. Add "workspace" once the public workspace library lands.
VALID_ITEM_TYPES = ("template", "workspace")

_schema_initialized = False
_schema_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id     TEXT NOT NULL,
    item_type   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_user
    ON favorites(user_id, item_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_favorites_item
    ON favorites(item_type, item_id);

CREATE TABLE IF NOT EXISTS downloads (
    user_id     TEXT NOT NULL,
    item_type   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_downloads_user
    ON downloads(user_id, item_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_downloads_item
    ON downloads(item_type, item_id);
"""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_schema() -> None:
    db = _dbmod._db()
    db.executescript(SCHEMA)
    db.commit()


def _ensure_schema_once() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if not _schema_initialized:
            _ensure_schema()
            _schema_initialized = True


def _check_item_type(item_type: str) -> None:
    if item_type not in VALID_ITEM_TYPES:
        raise ValueError(
            f"item_type must be one of {VALID_ITEM_TYPES}, got {item_type!r}"
        )


# ---------------------------------------------------------------------------
# Hearts (favorites)
# ---------------------------------------------------------------------------

def heart(user_id, item_type, item_id):
    """Toggle heart on an item. Returns (hearted, count_for_this_user).

    ``count_for_this_user`` is the number of items of this type the user
    has hearted in total — NOT the aggregate count for this specific item.
    The aggregate global count is tracked in ``public_dataset.py``.

    If ``user_id`` is falsy (anonymous), this is a no-op and returns
    ``(False, 0)``.
    """
    if not user_id or not item_id:
        return (False, 0)
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    now = _iso_now()
    with _write_lock:
        row = db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND item_type = ? AND item_id = ?",
            (user_id, item_type, item_id)).fetchone()
        if row:
            db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND item_type = ? AND item_id = ?",
                (user_id, item_type, item_id))
            db.commit()
            hearted = False
        else:
            db.execute(
                "INSERT OR REPLACE INTO favorites (user_id, item_type, item_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, item_type, item_id, now))
            db.commit()
            hearted = True
    # Best-effort total count of items of this type the user has hearted.
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM favorites WHERE user_id = ? AND item_type = ?",
            (user_id, item_type)).fetchone()
        count = int(row["c"]) if row else 0
    except Exception:
        count = 0
    return (hearted, count)


def unheart(user_id, item_type, item_id):
    """Force-remove a heart. Returns True if a row was deleted.

    Idempotent — calling this when the user has not hearted the item is a
    no-op returning False. Useful for cleanup when an item is deleted.
    """
    if not user_id or not item_id:
        return False
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    with _write_lock:
        cur = db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND item_type = ? AND item_id = ?",
            (user_id, item_type, item_id))
        db.commit()
        return cur.rowcount > 0


def is_favorited(user_id, item_type, item_id):
    """Check if a user has hearted an item. False for anonymous users."""
    if not user_id or not item_id:
        return False
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    row = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND item_type = ? AND item_id = ?",
        (user_id, item_type, item_id)).fetchone()
    return row is not None


def list_favorites(user_id, item_type, *, limit=500):
    """List all item_ids of this type the user has hearted, newest first.

    Returns ``[]`` for anonymous users. ``limit`` caps the result count
    (default 500 — enough for any realistic UI page).
    """
    if not user_id:
        return []
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    rows = db.execute(
        "SELECT item_id FROM favorites WHERE user_id = ? AND item_type = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, item_type, max(1, int(limit)))).fetchall()
    return [r["item_id"] for r in rows]


def annotate_favorited(user_id, item_type, items, *, id_key="id"):
    """Annotate each item dict in-place with a ``hearted`` boolean flag.

    Bulk-optimised: ONE SELECT for the whole page keyed on the ids in
    ``items``. Anonymous users get ``hearted=False`` on every item.

    This is the fix for Bug 4 ("heart/download don't persist"): list
    endpoints used to return only aggregate counts, so the frontend
    couldn't render the right per-user heart state after a reload.
    """
    if not items:
        return
    for it in items:
        it["hearted"] = False
    if not user_id:
        return
    ids = [it.get(id_key) for it in items if it.get(id_key)]
    if not ids:
        return
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    placeholders = ",".join("?" * len(ids))
    try:
        rows = db.execute(
            "SELECT item_id FROM favorites "
            "WHERE user_id = ? AND item_type = ? AND item_id IN (" + placeholders + ")",
            [user_id, item_type, *ids]).fetchall()
        hearted_ids = {r["item_id"] for r in rows}
        for it in items:
            if it.get(id_key) in hearted_ids:
                it["hearted"] = True
    except Exception:
        # Best-effort: never break listing on a flag-lookup failure.
        pass


# ---------------------------------------------------------------------------
# Downloads (idempotent per user)
# ---------------------------------------------------------------------------

def download(user_id, item_type, item_id):
    """Record a download. Idempotent per (user, item).

    Returns True if this is a NEW download (first time this user has
    downloaded this item), False if the user has already downloaded it
    before (or if ``user_id`` is anonymous/None).

    The caller uses the return value to decide whether to bump the
    aggregate download count (in SQLite templates table AND the public
    HF dataset).
    """
    if not user_id or not item_id:
        return False
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    now = _iso_now()
    with _write_lock:
        row = db.execute(
            "SELECT 1 FROM downloads WHERE user_id = ? AND item_type = ? AND item_id = ?",
            (user_id, item_type, item_id)).fetchone()
        if row:
            db.commit()
            return False
        db.execute(
            "INSERT OR REPLACE INTO downloads (user_id, item_type, item_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, item_type, item_id, now))
        db.commit()
        return True


def is_downloaded(user_id, item_type, item_id):
    """Check if a user has downloaded an item. False for anonymous users."""
    if not user_id or not item_id:
        return False
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    row = db.execute(
        "SELECT 1 FROM downloads WHERE user_id = ? AND item_type = ? AND item_id = ?",
        (user_id, item_type, item_id)).fetchone()
    return row is not None


def annotate_downloaded(user_id, item_type, items, *, id_key="id"):
    """Annotate each item dict in-place with a ``downloaded`` boolean flag.

    Bulk-optimised (single SELECT). Companion to :func:`annotate_favorited`.
    """
    if not items:
        return
    for it in items:
        it["downloaded"] = False
    if not user_id:
        return
    ids = [it.get(id_key) for it in items if it.get(id_key)]
    if not ids:
        return
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    placeholders = ",".join("?" * len(ids))
    try:
        rows = db.execute(
            "SELECT item_id FROM downloads "
            "WHERE user_id = ? AND item_type = ? AND item_id IN (" + placeholders + ")",
            [user_id, item_type, *ids]).fetchall()
        downloaded_ids = {r["item_id"] for r in rows}
        for it in items:
            if it.get(id_key) in downloaded_ids:
                it["downloaded"] = True
    except Exception:
        pass


def purge_item(item_type, item_id):
    """Delete ALL favorites + downloads rows for a given item.

    Called when an item is permanently deleted (e.g. a template is
    removed by its owner). Returns the total number of rows deleted.

    Safe to call with non-existent items (returns 0).
    """
    if not item_id:
        return 0
    _check_item_type(item_type)
    _ensure_schema_once()
    db = _dbmod._db()
    total = 0
    with _write_lock:
        cur = db.execute(
            "DELETE FROM favorites WHERE item_type = ? AND item_id = ?",
            (item_type, item_id))
        total += cur.rowcount
        cur = db.execute(
            "DELETE FROM downloads WHERE item_type = ? AND item_id = ?",
            (item_type, item_id))
        total += cur.rowcount
        db.commit()
    return total


def annotate_user_flags(user_id, item_type, items, *, id_key="id"):
    """Annotate BOTH ``hearted`` and ``downloaded`` in one pass.

    Used by ``template_library._annotate_user_flags`` and (eventually) the
    workspace library to keep the per-call SELECT count down to exactly
    two (one per flag) regardless of page size.
    """
    annotate_favorited(user_id, item_type, items, id_key=id_key)
    annotate_downloaded(user_id, item_type, items, id_key=id_key)
