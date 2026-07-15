"""Template library — user-created reusable templates for websearch,
deepresearch, judge, chat, and custom workflows.

Provides the /api/templates* family of endpoints (replacing the old
orchestrator-schematic /api/templates, which has moved to
/api/orchestrator/templates):

  * GET    /api/templates                  — list the caller's templates
                                              (private + published).
  * GET    /api/templates/explore          — browse public templates with
                                              sort (hearts|recent|relevant),
                                              text search, kind filter.
  * GET    /api/templates/<id>             — fetch one template (full markdown).
  * POST   /api/templates                  — create a new template.
  * PATCH  /api/templates/<id>             — update (owner only).
  * DELETE /api/templates/<id>             — delete (owner only).
  * POST   /api/templates/<id>/heart       — toggle heart.
  * POST   /api/templates/<id>/download    — download (creates a local copy
                                              for the caller so they can
                                              edit their own version).
  * POST   /api/templates/<id>/publish     — publish to the global library.
  * POST   /api/templates/<id>/unpublish   — unpublish.

Storage: SQLite via the existing db._db() connection (same DB file as the
rest of the service). Schema is created idempotently on first request.

Auth: same bearer token gate as the rest of /api/agent. Identity (GitHub
user_id) is read from the X-JWT header via the request handler's
_require_user_from_jwt(). Anonymous users (no JWT) get user_id=None and
can browse/explore but cannot create/heart/download.

The default catalog (11 system templates: Breadth Search, Deep Dive,
Compare & Contrast, Fact Check, Default Deep Research, ReAct Loop,
Extended Thinking, Critique/Verify/Improve/Debate Panels) is seeded
idempotently on first use via seed_defaults().

Wired into critique_service.py via handle_request(method, path, body, handler).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import db as _dbmod
import favorites as _favs
import public_dataset as _pub

_write_lock = _dbmod._write_lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: str, **fields: Any) -> None:
    """Best-effort structured log via debug_log (never raises)."""
    try:
        import debug_log
        debug_log.log_event(event, **fields)
    except Exception:
        pass


VALID_KINDS = ("websearch", "deepresearch", "judge", "chat", "custom")
VALID_SORTS = ("hearts", "recent", "relevant")
VALID_FILTERS = ("favorites",)  # additive filter values for list endpoints


# ---------------------------------------------------------------------------
# Schema (idempotent — safe to call on every request)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id           TEXT PRIMARY KEY,
    author_id    TEXT NOT NULL,
    author_name  TEXT,
    name         TEXT NOT NULL,
    description  TEXT,
    markdown     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    tags         TEXT,
    is_public    INTEGER NOT NULL DEFAULT 0,
    hearts       INTEGER NOT NULL DEFAULT 0,
    downloads    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_author ON templates(author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_templates_public ON templates(is_public, hearts DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_templates_kind ON templates(kind, is_public);

CREATE TABLE IF NOT EXISTS template_hearts (
    template_id  TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (template_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_template_hearts_user ON template_hearts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS template_downloads (
    template_id  TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (template_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_template_downloads_user ON template_downloads(user_id, created_at DESC);
"""


def _ensure_schema() -> None:
    db = _dbmod._db()
    db.executescript(SCHEMA)
    db.commit()


_schema_initialized = False
_schema_lock = threading.Lock()


def _ensure_schema_once() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if not _schema_initialized:
            _ensure_schema()
            # Shared favorites/downloads schema (templates + workspaces).
            _favs._ensure_schema_once()
            # One-time best-effort migration of legacy per-template
            # hearts/downloads rows into the new generic tables.
            _migrate_legacy_favorites()
            seed_defaults()
            _schema_initialized = True


def _migrate_legacy_favorites() -> None:
    """One-time best-effort migration: copy rows from the legacy
    ``template_hearts`` / ``template_downloads`` tables into the new
    generic ``favorites`` / ``downloads`` tables managed by
    ``favorites.py``.

    Idempotent (uses INSERT OR IGNORE). Silently skips if the legacy
    tables don't exist or are empty. Never raises — a migration failure
    must not block first-request template initialization.
    """
    db = _dbmod._db()
    now = _iso_now()
    try:
        with _write_lock:
            # Legacy hearts -> generic favorites (item_type='template')
            try:
                rows = db.execute(
                    "SELECT template_id, user_id, created_at FROM template_hearts"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                db.execute(
                    "INSERT OR IGNORE INTO favorites "
                    "(user_id, item_type, item_id, created_at) "
                    "VALUES (?, 'template', ?, ?)",
                    (r["user_id"], r["template_id"], r["created_at"] or now))
            # Legacy downloads -> generic downloads (item_type='template')
            try:
                rows = db.execute(
                    "SELECT template_id, user_id, created_at FROM template_downloads"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                db.execute(
                    "INSERT OR IGNORE INTO downloads "
                    "(user_id, item_type, item_id, created_at) "
                    "VALUES (?, 'template', ?, ?)",
                    (r["user_id"], r["template_id"], r["created_at"] or now))
            db.commit()
    except Exception as exc:
        _log("template_legacy_fav_migration_failed", error=repr(exc)[:200])


# ---------------------------------------------------------------------------
# Row -> dict mapper
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Parse tags JSON -> list
    raw_tags = d.get("tags")
    if isinstance(raw_tags, str) and raw_tags:
        try:
            d["tags"] = json.loads(raw_tags)
        except (json.JSONDecodeError, ValueError):
            d["tags"] = []
    elif raw_tags is None:
        d["tags"] = []
    # Booleans (SQLite stores as 0/1)
    d["is_public"] = bool(d.get("is_public", 0))
    return d


def _normalize_tags(tags: Any) -> str:
    """Coerce tags input (list, comma-separated string, or None) into a JSON
    array string for storage. Strips empties + duplicates, lowercases."""
    if tags is None:
        return "[]"
    if isinstance(tags, str):
        # comma-separated
        parts = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
    else:
        return "[]"
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl not in seen:
            seen.add(pl)
            out.append(p)
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_template(*, user_id: str | None, name: str,
                    markdown: str, kind: str,
                    description: str | None = None,
                    tags: Any = None,
                    is_public: bool = False,
                    author_name: str | None = None) -> dict:
    """Create a new template. Returns the template dict."""
    _ensure_schema_once()
    if not name or not name.strip():
        raise ValueError("'name' is required")
    if not markdown or not markdown.strip():
        raise ValueError("'markdown' is required")
    if kind not in VALID_KINDS:
        raise ValueError(f"'kind' must be one of {VALID_KINDS}")
    db = _dbmod._db()
    tid = _gen_id()
    now = _iso_now()
    author_id = user_id or "anonymous"
    author = author_name or _author_label_for(user_id)
    with _write_lock:
        db.execute(
            "INSERT INTO templates (id, author_id, author_name, name, description, "
            "markdown, kind, tags, is_public, hearts, downloads, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (tid, author_id, author, name.strip(),
             (description or "").strip() or None,
             markdown, kind, _normalize_tags(tags),
             1 if is_public else 0, now, now))
        db.commit()
    row = db.execute("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
    return _row_to_dict(row) or {}


def list_my_templates(user_id: str | None, *, limit: int = 200,
                      filter: str | None = None) -> list[dict]:
    """List the caller's own templates (private + published), newest first.

    Each returned template dict includes per-user ``hearted`` and
    ``downloaded`` boolean flags (always False for anonymous callers) so the
    frontend can render the correct heart/download button state after a
    page reload — without these flags the UI couldn't tell which templates
    the current user has already hearted/downloaded (Bug 4).

    ``filter='favorites'`` returns the templates the caller has hearted
    (across ALL templates — not just their own), newest heart first.
    Anonymous callers get an empty list with this filter (no per-user
    state to filter on)."""
    _ensure_schema_once()
    db = _dbmod._db()
    filter = (filter or "").strip().lower() or None
    # Filter-by-favorites: return every template the user has hearted,
    # regardless of authorship (the user's "favorites" view).
    if filter == "favorites":
        if not user_id:
            return []
        fav_ids = _favs.list_favorites(user_id, "template", limit=limit)
        if not fav_ids:
            return []
        placeholders = ",".join("?" * len(fav_ids))
        rows = db.execute(
            "SELECT * FROM templates WHERE id IN (" + placeholders + ") "
            "ORDER BY created_at DESC LIMIT ?",
            [*fav_ids, limit]).fetchall()
        out = [_row_to_dict(r) for r in rows]
        _annotate_user_flags(out, user_id)
        # Enforce visibility: drop templates the caller can't see (private
        # templates authored by someone else — defensive; should not happen
        # because hearting requires visibility, but guard anyway).
        out = [t for t in out
               if t.get("is_public") or _is_owner(t, user_id)]
        return out
    if user_id:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
    else:
        # Anonymous: only return anonymous-authored templates.
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'anonymous' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    out = [_row_to_dict(r) for r in rows]
    _annotate_user_flags(out, user_id)
    return out


def list_public_templates(*, sort: str = "hearts", query: str | None = None,
                          kind: str | None = None,
                          limit: int = 50, offset: int = 0,
                          user_id: str | None = None,
                          filter: str | None = None) -> tuple[list[dict], int]:
    """List public templates. Returns (templates, total). Sort by hearts
    (most hearted), recent (newest), or relevant (text-search relevance via
    LIKE on name+description+markdown+tags).

    Each returned template dict includes per-user ``hearted`` and
    ``downloaded`` boolean flags for ``user_id`` (always False when
    ``user_id`` is None) so the frontend can render the correct
    heart/download button state after a page reload (Bug 4).

    Source-of-truth: the public HF dataset (``public_dataset.py``).
    Falls back to local SQLite if the dataset is unreachable / empty
    (so the system default templates remain browsable offline).

    ``filter='favorites'`` returns only public templates the caller has
    hearted (Anonymous callers get an empty result with this filter)."""
    _ensure_schema_once()
    if sort not in VALID_SORTS:
        sort = "hearts"
    filter = (filter or "").strip().lower() or None
    page_limit = max(1, min(int(limit), 200))
    page_offset = max(0, int(offset))

    # ----- Try the public HF dataset first (source of truth) -----
    out: list[dict] = []
    total = 0
    try:
        pub_items, pub_total = _pub.list_public_templates(
            sort=sort, query=query, kind=kind,
            limit=page_limit, offset=page_offset)
    except Exception as exc:
        _log("template_public_list_failed", error=repr(exc)[:200])
        pub_items, pub_total = [], 0
    if pub_items:
        out = pub_items
        total = pub_total
        # Normalize: public-dataset records use list-typed tags + a bool
        # is_public; the rest of the code expects the same shape as a local
        # SQLite row (post _row_to_dict).
        for it in out:
            if it.get("tags") is None:
                it["tags"] = []
            if "is_public" not in it:
                it["is_public"] = True
        _annotate_user_flags(out, user_id)
        # Apply favorites filter post-hoc (only items the user has hearted).
        if filter == "favorites":
            if not user_id:
                return ([], 0)
            fav_ids = set(_favs.list_favorites(user_id, "template", limit=2000))
            out = [t for t in out if t.get("id") in fav_ids]
            total = len(out)
        return (out, total)

    # ----- Fallback: local SQLite (dataset unreachable / empty) -----
    db = _dbmod._db()
    where = "is_public = 1"
    params: list[Any] = []
    if kind and kind in VALID_KINDS:
        where += " AND kind = ?"
        params.append(kind)
    # Text search: LIKE on name + description + markdown + tags.
    has_query = bool(query and query.strip())
    if has_query:
        q = "%" + query.strip() + "%"
        where += " AND (name LIKE ? OR description LIKE ? OR markdown LIKE ? OR tags LIKE ?)"
        params.extend([q, q, q, q])
    # Favorites filter (local fallback): restrict to the user's hearted ids.
    if filter == "favorites":
        if not user_id:
            return ([], 0)
        fav_ids = _favs.list_favorites(user_id, "template", limit=2000)
        if not fav_ids:
            return ([], 0)
        placeholders = ",".join("?" * len(fav_ids))
        where += " AND id IN (" + placeholders + ")"
        params.extend(fav_ids)

    # Total count (for pagination).
    count_row = db.execute(
        f"SELECT COUNT(*) AS c FROM templates WHERE {where}", params).fetchone()
    total = int(count_row["c"]) if count_row else 0

    # Sort order.
    if sort == "hearts":
        order = "hearts DESC, downloads DESC, created_at DESC"
    elif sort == "recent":
        order = "created_at DESC"
    else:  # relevant — give exact-name matches a boost, then hearts.
        order = "CASE WHEN name LIKE ? THEN 0 ELSE 1 END, hearts DESC, created_at DESC"
        if has_query:
            # Insert the exact-name-match param at the front of the ORDER BY
            # params (after the WHERE params).
            params = list(params) + [query.strip() + "%"]
        else:
            # No query -> "relevant" degenerates to "hearts".
            order = "hearts DESC, downloads DESC, created_at DESC"

    rows = db.execute(
        f"SELECT * FROM templates WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_limit, page_offset]).fetchall()
    out = [_row_to_dict(r) for r in rows]
    _annotate_user_flags(out, user_id)
    return out, total


def _annotate_user_flags(templates: list[dict], user_id: str | None) -> None:
    """Annotate each template dict with per-user ``hearted`` and
    ``downloaded`` boolean flags (in-place). Anonymous callers (no
    user_id) get both flags set to False for every template.

    This is the fix for Bug 4 (heart/download don't persist): the
    list endpoints previously returned only the aggregate hearts/downloads
    counts, so the frontend couldn't tell which templates the current
    user had already hearted/downloaded — after a reload every heart
    button appeared unhearted even though the row was in
    ``template_hearts``. We do ONE bulk SELECT per table (keyed on the
    template ids in the page) so this stays O(page_size) not O(N²).

    Delegates to the shared ``favorites.annotate_user_flags`` so the same
    per-user flag logic is reused by templates AND workspaces (the user
    asked for these systems to share functionality)."""
    # The shared helper initialises both flags to False, handles the
    # anonymous case, and does the bulk SELECT. We keep this wrapper for
    # backward compat with the rest of this module.
    _favs.annotate_user_flags(user_id, "template", templates, id_key="id")


def get_template(template_id: str) -> dict | None:
    """Fetch one template by id (full markdown). Returns None if not found.

    Tries local SQLite first; if not found, falls back to the public HF
    dataset (for templates the caller has not downloaded/created locally
    but that have been published globally by another user)."""
    _ensure_schema_once()
    row = _dbmod._db().execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    if row:
        return _row_to_dict(row)
    # Fallback: public HF dataset (best-effort, never raises).
    try:
        pub = _pub.get_template(template_id)
        if pub:
            # Normalize: ensure is_public is set (everything in the public
            # dataset is public by definition) and tags is a list.
            pub = dict(pub)
            pub.setdefault("is_public", True)
            if pub.get("tags") is None:
                pub["tags"] = []
            return pub
    except Exception as exc:
        _log("template_public_get_failed",
             template_id=template_id, error=repr(exc)[:200])
    return None


def update_template(template_id: str, user_id: str | None, *,
                    name: str | None = None, description: str | None = None,
                    markdown: str | None = None, kind: str | None = None,
                    tags: Any = None, is_public: bool | None = None) -> dict | None:
    """Update a template (owner only). Returns the updated template or None
    if not found / not owned by caller."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        return None
    if not _is_owner(existing, user_id):
        return None  # caller will surface a 403
    fields: list[str] = []
    params: list[Any] = []
    if name is not None and name.strip():
        fields.append("name = ?")
        params.append(name.strip())
    if description is not None:
        fields.append("description = ?")
        params.append(description.strip() or None)
    if markdown is not None and markdown.strip():
        fields.append("markdown = ?")
        params.append(markdown)
    if kind is not None and kind in VALID_KINDS:
        fields.append("kind = ?")
        params.append(kind)
    if tags is not None:
        fields.append("tags = ?")
        params.append(_normalize_tags(tags))
    if is_public is not None:
        fields.append("is_public = ?")
        params.append(1 if is_public else 0)
    if not fields:
        return existing
    fields.append("updated_at = ?")
    params.append(_iso_now())
    params.append(template_id)
    with _write_lock:
        db.execute(
            f"UPDATE templates SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()
    return get_template(template_id)


def delete_template(template_id: str, user_id: str | None) -> bool | None:
    """Delete a template (owner only). Returns True if deleted, False if not
    found, None if not owned by caller.

    Also removes the template from the public HF dataset (best-effort) and
    purges all per-user favorites/downloads rows via the shared
    ``favorites.purge_item`` helper."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        return False
    if not _is_owner(existing, user_id):
        return None
    with _write_lock:
        db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        # Legacy tables (kept for backward compat with any older code paths).
        try:
            db.execute("DELETE FROM template_hearts WHERE template_id = ?", (template_id,))
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("DELETE FROM template_downloads WHERE template_id = ?", (template_id,))
        except sqlite3.OperationalError:
            pass
        db.commit()
    # Shared generic tables (templates + workspaces).
    try:
        _favs.purge_item("template", template_id)
    except Exception as exc:
        _log("template_purge_favorites_failed",
             template_id=template_id, error=repr(exc)[:200])
    # Public HF dataset (best-effort — silent no-op if unreachable).
    try:
        _pub.unpublish_template(template_id)
    except Exception as exc:
        _log("template_unpublish_on_delete_failed",
             template_id=template_id, error=repr(exc)[:200])
    return True


def heart_template(template_id: str, user_id: str | None) -> tuple[bool, int]:
    """Toggle heart on a template. Returns (hearted, hearts_count).

    Per-user state lives in the shared ``favorites`` table (so the same
    code path serves templates AND workspaces). The local SQLite
    ``templates.hearts`` column is bumped as a per-Space aggregate cache
    (used when the public HF dataset is unreachable). The GLOBAL aggregate
    count lives in the public HF dataset (``public_dataset.add_heart`` /
    ``remove_heart``) so hearts persist ACROSS users AND across Spaces.

    If the caller is anonymous (no user_id), the heart is not recorded
    but the count is still returned (no-op toggle)."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        raise KeyError(template_id)
    if not user_id:
        # Anonymous can't heart — return current count, hearted=False.
        return (False, int(existing.get("hearts") or 0))
    # Per-user toggle (shared favorites table — same logic for workspaces).
    hearted, _ = _favs.heart(user_id, "template", template_id)
    # Bump the local SQLite aggregate count (per-Space cache).
    try:
        with _write_lock:
            if hearted:
                db.execute(
                    "UPDATE templates SET hearts = hearts + 1 WHERE id = ?",
                    (template_id,))
            else:
                db.execute(
                    "UPDATE templates SET hearts = MAX(hearts - 1, 0) WHERE id = ?",
                    (template_id,))
            db.commit()
            row = db.execute(
                "SELECT hearts FROM templates WHERE id = ?", (template_id,)).fetchone()
        count = int(row["hearts"]) if row else 0
    except Exception:
        count = int(existing.get("hearts") or 0)
    # Bump the GLOBAL aggregate count in the public HF dataset (best-effort).
    # We attempt this regardless of is_public — if the template isn't in
    # the public dataset, ``_bump_counter`` silently no-ops.
    try:
        if hearted:
            _pub.add_heart(template_id)
        else:
            _pub.remove_heart(template_id)
    except Exception as exc:
        _log("template_public_heart_failed",
             template_id=template_id, hearted=hearted, error=repr(exc)[:200])
    return (hearted, count)


def download_template(template_id: str, user_id: str | None) -> dict:
    """Download a template: increment its download count (idempotent per
    user) and create a LOCAL COPY for the caller so they have their own
    editable version. Returns the local copy.

    Per-user download tracking lives in the shared ``downloads`` table
    (``favorites.download``). The local SQLite ``templates.downloads``
    column is bumped as a per-Space aggregate cache. The GLOBAL aggregate
    count lives in the public HF dataset
    (``public_dataset.increment_downloads``) so the count persists
    across users AND across Spaces.

    Raises KeyError if the template doesn't exist.
    Raises PermissionError if the template is private and not owned by
    the caller (private templates are only downloadable by their owner)."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        raise KeyError(template_id)
    # Private templates are only downloadable by their owner.
    if not existing.get("is_public") and not _is_owner(existing, user_id):
        raise PermissionError("template is private")
    # Record the download (idempotent per user) in the shared downloads
    # table. Returns True iff this is a NEW download (first time this user
    # has downloaded this item) — only then do we bump the aggregate
    # counts (local cache + global HF dataset).
    is_new_download = False
    if user_id:
        try:
            is_new_download = _favs.download(user_id, "template", template_id)
        except Exception as exc:
            _log("template_fav_download_failed",
                 template_id=template_id, error=repr(exc)[:200])
            is_new_download = False
    if is_new_download:
        # Bump local SQLite aggregate count (per-Space cache).
        try:
            with _write_lock:
                db.execute(
                    "UPDATE templates SET downloads = downloads + 1 WHERE id = ?",
                    (template_id,))
                db.commit()
        except Exception as exc:
            _log("template_local_download_bump_failed",
                 template_id=template_id, error=repr(exc)[:200])
        # Bump the GLOBAL aggregate count in the public HF dataset.
        try:
            _pub.increment_downloads(template_id)
        except Exception as exc:
            _log("template_public_download_failed",
                 template_id=template_id, error=repr(exc)[:200])
    # Create a LOCAL COPY for the caller (so they have their own editable
    # version). The copy is private, attributed to the caller, and credits
    # the original via a "(copy of <id>)" suffix on the name + a tag.
    # tags is already a list (parsed by _row_to_dict), so pass it through
    # directly to create_template which re-normalizes it.
    tags_value = existing.get("tags") or []
    if isinstance(tags_value, str):
        try:
            tags_value = json.loads(tags_value)
        except (json.JSONDecodeError, ValueError):
            tags_value = []
    local = create_template(
        user_id=user_id,
        name=f"{existing['name']} (copy)",
        description=existing.get("description"),
        markdown=existing["markdown"],
        kind=existing["kind"],
        tags=tags_value,
        is_public=False,
        author_name=_author_label_for(user_id))
    return local


def publish_template(template_id: str, user_id: str | None) -> dict | None:
    """Publish a template to the global library (owner only). Returns the
    updated template or None if not found / not owned.

    Pushes the template to the public HF dataset (best-effort — silent
    no-op if the dataset is unreachable, in which case the local SQLite
    row is still marked is_public=1 and will be served via the local
    fallback in ``list_public_templates``)."""
    tpl = update_template(template_id, user_id, is_public=True)
    if tpl is None:
        return None
    # Best-effort push to the public HF dataset. Failures are logged but
    # do not affect the local publish (the local SQLite row is already
    # is_public=1; the public dataset will catch up on the next publish
    # attempt or via a future sync job).
    try:
        _pub.publish_template(tpl)
    except Exception as exc:
        _log("template_publish_to_public_failed",
             template_id=template_id, error=repr(exc)[:200])
    return tpl


def unpublish_template(template_id: str, user_id: str | None) -> dict | None:
    """Unpublish a template from the global library (owner only).

    Removes the template from the public HF dataset (best-effort)."""
    tpl = update_template(template_id, user_id, is_public=False)
    if tpl is None:
        return None
    try:
        _pub.unpublish_template(template_id)
    except Exception as exc:
        _log("template_unpublish_from_public_failed",
             template_id=template_id, error=repr(exc)[:200])
    return tpl


def _is_owner(template: dict, user_id: str | None) -> bool:
    """True if the caller owns the template. System templates
    (author_id='system') are not owned by any user (only the system can
    modify them, which we don't expose via HTTP)."""
    if not user_id:
        return False
    return template.get("author_id") == user_id


def _author_label_for(user_id: str | None) -> str:
    """Best-effort display name for a user_id. Falls back to 'you' for
    authenticated users and 'anonymous' for unauthenticated."""
    if not user_id:
        return "anonymous"
    try:
        user = _dbmod.get_user(user_id)
        if user:
            uname = (user.get("github_username") or user.get("name")
                     or user.get("email") or "").strip()
            if uname:
                return uname
    except Exception:
        pass
    return "you"


# ---------------------------------------------------------------------------
# Default template catalog (seeded idempotently on first use)
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATES: list[dict] = [
    # --- websearch ---
    {
        "name": "Breadth Search",
        "description": "Decompose a question into 4-6 sub-topics, search each in "
                       "parallel via sub-agents, then synthesize a comprehensive "
                       "answer with citations.",
        "markdown": (
            "# Breadth Search\n\n"
            "Decompose the user's question into 4-6 independent sub-topics. "
            "For each sub-topic, run a parallel web search via a sub-agent. "
            "Collect the top sources from each sub-search. Synthesize a "
            "comprehensive answer that integrates all sub-topic findings, "
            "citing sources inline. Surface areas of consensus and areas "
            "where sources disagree.\n\n"
            "## Steps\n"
            "1. Decompose: break the question into 4-6 sub-topics.\n"
            "2. Search: for each sub-topic, run a web search and collect "
            "the top 5 results.\n"
            "3. Read: fetch the full content of the top 2 results per "
            "sub-topic.\n"
            "4. Synthesize: write a comprehensive answer integrating all "
            "findings, with inline citations.\n"
            "5. Surface gaps: note any sub-topics where evidence was thin "
            "or contradictory.\n"
        ),
        "kind": "websearch",
        "tags": ["breadth", "parallel", "synthesis", "citations"],
    },
    {
        "name": "Deep Dive",
        "description": "Drill into one thread, follow citations up to 3 hops, "
                       "then summarize with evidence.",
        "markdown": (
            "# Deep Dive\n\n"
            "Pick the single most authoritative source for the question. "
            "Read it fully, extract its key citations, and follow each "
            "citation up to 3 hops deep. At each hop, evaluate whether the "
            "cited source supports the original claim. Summarize the chain "
            "of evidence with confidence ratings.\n\n"
            "## Steps\n"
            "1. Seed: find the most authoritative source for the question.\n"
            "2. Read: fetch + read the full source.\n"
            "3. Follow citations: extract up to 5 key citations and fetch "
            "each (hop 1). For each hop-1 source, extract + fetch up to 3 "
            "citations (hop 2). Continue to hop 3 for the most central "
            "claims.\n"
            "4. Evaluate: at each hop, rate whether the cited source "
            "supports the original claim (SUPPORTS / REFUTES / MIXED / "
            "UNVERIFIED).\n"
            "5. Summarize: write a deep summary with the chain of evidence "
            "and confidence ratings.\n"
        ),
        "kind": "websearch",
        "tags": ["depth", "citations", "evidence", "chain"],
    },
    {
        "name": "Compare & Contrast",
        "description": "Find multiple perspectives on a question, contrast "
                       "them, surface consensus and disagreement.",
        "markdown": (
            "# Compare & Contrast\n\n"
            "Find 3-5 distinct perspectives on the question (different "
            "sources, different viewpoints, different methodologies). For "
            "each perspective, summarize the core claim + supporting "
            "evidence. Then build a comparison matrix and surface (a) areas "
            "of consensus and (b) areas of disagreement with the reasons.\n\n"
            "## Steps\n"
            "1. Survey: find 3-5 sources representing distinct perspectives.\n"
            "2. Extract: for each source, extract the core claim + 2-3 "
            "supporting evidence points.\n"
            "3. Matrix: build a comparison table (perspective x claim x "
            "evidence).\n"
            "4. Consensus: identify claims where most sources agree.\n"
            "5. Disagreement: identify claims where sources disagree, and "
            "explain WHY they disagree (different data, different "
            "assumptions, different values).\n"
            "6. Synthesize: write a balanced summary that presents the "
            "consensus + the disagreement.\n"
        ),
        "kind": "websearch",
        "tags": ["compare", "contrast", "perspectives", "consensus"],
    },
    {
        "name": "Fact Check",
        "description": "Extract claims, verify each against independent "
                       "sources, rate confidence TRUE/FALSE/MIXED/UNVERIFIED.",
        "markdown": (
            "# Fact Check\n\n"
            "Extract every factual claim from the input. For each claim, "
            "find 2-3 INDEPENDENT sources (not citing each other) and "
            "verify. Rate each claim TRUE / FALSE / MIXED / UNVERIFIED "
            "with a confidence score (0-100%) and the evidence.\n\n"
            "## Steps\n"
            "1. Extract claims: parse the input into a list of discrete "
            "factual claims.\n"
            "2. For each claim, search for 2-3 independent sources.\n"
            "3. For each source, evaluate whether it supports or refutes "
            "the claim.\n"
            "4. Rate: TRUE (all sources support), FALSE (all sources "
            "refute), MIXED (sources disagree), UNVERIFIED (no reliable "
            "sources found).\n"
            "5. Confidence: 0-100% based on source quality + agreement.\n"
            "6. Report: a table of claim x verdict x confidence x evidence.\n"
        ),
        "kind": "websearch",
        "tags": ["fact-check", "verify", "claims", "confidence"],
    },
    # --- deepresearch ---
    {
        "name": "Default Deep Research",
        "description": "Multi-step search -> read -> synthesize with citations.",
        "markdown": (
            "# Default Deep Research\n\n"
            "A multi-step research workflow: search broadly, read the top "
            "results, generate follow-up questions, search again to fill "
            "gaps, then synthesize a comprehensive report with citations.\n\n"
            "## Steps\n"
            "1. Initial search: 3-5 broad queries covering the question.\n"
            "2. Read: fetch + read the top 5 results across all queries.\n"
            "3. Follow-ups: generate 3-5 follow-up questions based on gaps "
            "in the initial reading.\n"
            "4. Second search: run the follow-up queries, read top results.\n"
            "5. Synthesize: write a comprehensive research report with "
            "inline citations + a sources list.\n"
        ),
        "kind": "deepresearch",
        "tags": ["research", "multi-step", "synthesize", "citations"],
    },
    {
        "name": "ReAct Loop",
        "description": "Reason -> Act -> Observe cycle until the question is "
                       "answered.",
        "markdown": (
            "# ReAct Loop\n\n"
            "Run the Reason -> Act -> Observe loop until the question is "
            "fully answered or the step budget is exhausted. At each step: "
            "REASON about what to do next, ACT (call a tool: web_search, "
            "web_fetch, calculator, etc.), OBSERVE the result, and decide "
            "whether another step is needed.\n\n"
            "## Steps\n"
            "1. Reason: given the question + prior observations, what's the "
            "next best action?\n"
            "2. Act: call the chosen tool with the right arguments.\n"
            "3. Observe: capture the tool's output.\n"
            "4. Loop: repeat until the question is answered OR the step "
            "budget (e.g., 10 steps) is hit.\n"
            "5. Final answer: synthesize the answer from the observations.\n"
        ),
        "kind": "deepresearch",
        "tags": ["react", "loop", "agent", "tools"],
    },
    {
        "name": "Extended Thinking",
        "description": "Single long-form reasoning pass with maximum thinking "
                       "budget.",
        "markdown": (
            "# Extended Thinking\n\n"
            "A single long-form reasoning pass with the maximum thinking "
            "budget the model supports. No tool calls, no multi-step "
            "searches — just deep, careful reasoning about the question. "
            "Best for math, logic, design, and analysis questions where "
            "the answer can be derived from the question itself.\n\n"
            "## Steps\n"
            "1. Engage maximum reasoning/thinking budget.\n"
            "2. Think step-by-step about the question, exploring multiple "
            "approaches.\n"
            "3. Consider edge cases + counterexamples.\n"
            "4. Arrive at a final answer with a clear justification.\n"
        ),
        "kind": "deepresearch",
        "tags": ["thinking", "reasoning", "long-form", "max-budget"],
    },
    # --- judge ---
    {
        "name": "Critique Panel",
        "description": "N judges independently critique the answer for "
                       "issues, risks, and improvements.",
        "markdown": (
            "# Critique Panel\n\n"
            "Fan the answer out to N independent judges (different model "
            "families for diversity). Each judge critiques the answer for: "
            "(a) factual errors, (b) logical gaps, (c) missing context, "
            "(d) risks/downsides, (e) concrete improvements. Merge the "
            "critiques (dedupe overlapping points) into a single review.\n\n"
            "## Steps\n"
            "1. Pick N diverse judges from the roster (different providers "
            "+ model families).\n"
            "2. Each judge independently critiques the answer using the "
            "critiquer role.\n"
            "3. Merge critiques with dedupe (overlapping points are "
            "combined).\n"
            "4. Return the merged critique.\n"
        ),
        "kind": "judge",
        "tags": ["critique", "panel", "review", "issues"],
    },
    {
        "name": "Verify Panel",
        "description": "N judges vote PASS/FAIL with reasons.",
        "markdown": (
            "# Verify Panel\n\n"
            "Fan the answer out to N independent judges. Each judge votes "
            "PASS or FAIL with a one-paragraph reason. Tally the votes; "
            "the verdict is PASS only if a majority vote PASS. Surface the "
            "dissenting opinions.\n\n"
            "## Steps\n"
            "1. Pick N diverse judges.\n"
            "2. Each judge votes PASS/FAIL with a reason.\n"
            "3. Tally: majority rules.\n"
            "4. Return verdict + vote tally + reasons (including "
            "dissenting).\n"
        ),
        "kind": "judge",
        "tags": ["verify", "panel", "vote", "pass-fail"],
    },
    {
        "name": "Improve Panel",
        "description": "N judges propose concrete improvements.",
        "markdown": (
            "# Improve Panel\n\n"
            "Fan the answer out to N independent judges. Each judge "
            "proposes 2-3 concrete, actionable improvements (not vague "
            "suggestions — specific edits, additions, or rewrites). Merge "
            "the proposals (dedupe overlapping ones) into a single "
            "improvement list.\n\n"
            "## Steps\n"
            "1. Pick N diverse judges.\n"
            "2. Each judge proposes 2-3 concrete improvements.\n"
            "3. Merge with dedupe.\n"
            "4. Return the prioritized improvement list.\n"
        ),
        "kind": "judge",
        "tags": ["improve", "panel", "transform", "actionable"],
    },
    {
        "name": "Debate Panel",
        "description": "N judges argue opposing sides, then converge.",
        "markdown": (
            "# Debate Panel\n\n"
            "Split N judges into two camps: pro and con. Each camp argues "
            "its side with evidence + reasoning. After the opening "
            "arguments, each camp rebuts the other. Finally, the judges "
            "converge on a synthesis that acknowledges the strongest "
            "points from both sides.\n\n"
            "## Steps\n"
            "1. Split N judges into pro + con camps.\n"
            "2. Opening arguments: each camp presents its case.\n"
            "3. Rebuttals: each camp rebuts the other's case.\n"
            "4. Convergence: the judges synthesize a balanced view that "
            "acknowledges the strongest points from both sides.\n"
            "5. Return the debate transcript + the synthesis.\n"
        ),
        "kind": "judge",
        "tags": ["debate", "panel", "pro-con", "synthesis"],
    },
]


def seed_defaults() -> int:
    """Seed the default system templates if the templates table is empty.
    Idempotent: only inserts if the table is empty. Returns the number of
    templates inserted (0 if already seeded)."""
    db = _dbmod._db()
    # Check if ANY system template exists (idempotent re-seed guard).
    row = db.execute(
        "SELECT COUNT(*) AS c FROM templates WHERE author_id = 'system'").fetchone()
    if row and int(row["c"]) > 0:
        return 0
    now = _iso_now()
    inserted = 0
    with _write_lock:
        for tpl in DEFAULT_TEMPLATES:
            # Skip if a system template with the same name already exists
            # (defensive — handles partial seeds from a crashed earlier run).
            existing = db.execute(
                "SELECT 1 FROM templates WHERE author_id = 'system' AND name = ?",
                (tpl["name"],)).fetchone()
            if existing:
                continue
            tid = _gen_id()
            db.execute(
                "INSERT INTO templates (id, author_id, author_name, name, description, "
                "markdown, kind, tags, is_public, hearts, downloads, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?)",
                (tid, "system", "doomalaysocreate", tpl["name"],
                 tpl.get("description"), tpl["markdown"], tpl["kind"],
                 _normalize_tags(tpl.get("tags")),
                 now, now))
            inserted += 1
        db.commit()
    return inserted


def list_default_templates(*, kind: str | None = None) -> list[dict]:
    """Return the system default templates, optionally filtered by kind.
    Used by /api/roster to populate the tool popovers in the frontend."""
    _ensure_schema_once()
    db = _dbmod._db()
    if kind and kind in VALID_KINDS:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'system' AND is_public = 1 "
            "AND kind = ? ORDER BY name ASC",
            (kind,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'system' AND is_public = 1 "
            "ORDER BY kind ASC, name ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


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


def _parse_path(path: str) -> tuple[str, str, str]:
    """Parse a /api/templates* path into (route, template_id, sub_action).

    Examples:
      /api/templates            -> ("/api/templates", "", "")
      /api/templates/explore    -> ("/api/templates/explore", "", "")
      /api/templates/abc123     -> ("/api/templates/<id>", "abc123", "")
      /api/templates/abc123/heart -> ("/api/templates/<id>/<sub>", "abc123", "heart")
    """
    route = urlsplit(path).path.rstrip("/")
    if route == "/api/templates":
        return ("/api/templates", "", "")
    if route == "/api/templates/explore":
        return ("/api/templates/explore", "", "")
    if route.startswith("/api/templates/"):
        rest = route[len("/api/templates/"):]
        parts = rest.split("/")
        if len(parts) == 1:
            return ("/api/templates/<id>", parts[0], "")
        if len(parts) == 2:
            return ("/api/templates/<id>/<sub>", parts[0], parts[1])
    return ("", "", "")


def handle_request(method: str, path: str, body: dict, handler) -> bool:
    """Dispatch /api/templates* routes. Returns True if handled."""
    route, tid, sub = _parse_path(path)
    if not route:
        return False
    # Auth: same bearer gate as /api/agent.
    if not handler._auth_ok():
        _json(handler, 401, {"error": "missing or invalid bearer token"})
        return True
    user_id = _user_id_from_handler(handler)

    # -----------------------------------------------------------------
    # GET routes
    # -----------------------------------------------------------------
    if method == "GET":
        if route == "/api/templates":
            q = parse_qs(urlsplit(path).query)
            filter = (q.get("filter", [None])[0] or "").strip().lower() or None
            tpls = list_my_templates(user_id, filter=filter)
            _json(handler, 200, {"templates": tpls})
            return True
        if route == "/api/templates/explore":
            q = parse_qs(urlsplit(path).query)
            sort = (q.get("sort", ["hearts"])[0] or "hearts").strip().lower()
            query = (q.get("query", [None])[0] or "").strip() or None
            kind = (q.get("kind", [None])[0] or "").strip() or None
            filter = (q.get("filter", [None])[0] or "").strip().lower() or None
            try:
                limit = int(q.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            try:
                offset = int(q.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            tpls, total = list_public_templates(
                sort=sort, query=query, kind=kind, limit=limit, offset=offset,
                user_id=user_id, filter=filter)
            _json(handler, 200, {"templates": tpls, "total": total})
            return True
        if route == "/api/templates/<id>":
            tpl = get_template(tid)
            if tpl is None:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            # Visibility: private templates are only visible to their owner.
            if not tpl.get("is_public") and not _is_owner(tpl, user_id):
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            # Annotate with per-user hearted/downloaded flags (Bug 4) so the
            # frontend can render the correct button state when a user opens
            # a template detail view directly via URL.
            _annotate_user_flags([tpl], user_id)
            _json(handler, 200, {"template": tpl})
            return True
        _json(handler, 404, {"error": "unknown GET route"})
        return True

    # -----------------------------------------------------------------
    # POST routes
    # -----------------------------------------------------------------
    if method == "POST":
        if route == "/api/templates":
            name = str(body.get("name", "")).strip()
            if not name:
                _json(handler, 400, {"error": "'name' (non-empty string) is required"})
                return True
            markdown = body.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                _json(handler, 400, {"error": "'markdown' (non-empty string) is required"})
                return True
            kind = str(body.get("kind", "custom")).strip().lower()
            if kind not in VALID_KINDS:
                _json(handler, 400, {"error": f"'kind' must be one of {VALID_KINDS}"})
                return True
            description = body.get("description")
            if description is not None and not isinstance(description, str):
                _json(handler, 400, {"error": "'description' must be a string"})
                return True
            tags = body.get("tags")
            is_public = bool(body.get("is_public", False))
            try:
                tpl = create_template(
                    user_id=user_id, name=name, description=description,
                    markdown=markdown, kind=kind, tags=tags, is_public=is_public)
            except ValueError as e:
                _json(handler, 400, {"error": str(e)})
                return True
            _json(handler, 201, {"template": tpl})
            return True
        if route == "/api/templates/<id>/<sub>":
            if sub == "heart":
                try:
                    hearted, hearts = heart_template(tid, user_id)
                except KeyError:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                _json(handler, 200, {"hearted": hearted, "hearts": hearts})
                return True
            if sub == "download":
                # Fetch the template first to enforce visibility.
                existing = get_template(tid)
                if existing is None:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                if not existing.get("is_public") and not _is_owner(existing, user_id):
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                try:
                    local_copy = download_template(tid, user_id)
                except KeyError:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                except PermissionError:
                    _json(handler, 403, {"error": "template is private"})
                    return True
                _json(handler, 200, {"template": local_copy})
                return True
            if sub == "publish":
                tpl = publish_template(tid, user_id)
                if tpl is None:
                    existing = get_template(tid)
                    if existing is None:
                        _json(handler, 404, {"error": f"no such template {tid!r}"})
                    else:
                        _json(handler, 403, {"error": "only the owner can publish"})
                    return True
                _json(handler, 200, {"template": tpl})
                return True
            if sub == "unpublish":
                tpl = unpublish_template(tid, user_id)
                if tpl is None:
                    existing = get_template(tid)
                    if existing is None:
                        _json(handler, 404, {"error": f"no such template {tid!r}"})
                    else:
                        _json(handler, 403, {"error": "only the owner can unpublish"})
                    return True
                _json(handler, 200, {"template": tpl})
                return True
            _json(handler, 404, {"error": f"unknown POST sub-route {sub!r}"})
            return True
        _json(handler, 404, {"error": "unknown POST route"})
        return True

    # -----------------------------------------------------------------
    # PATCH routes
    # -----------------------------------------------------------------
    if method == "PATCH":
        if route == "/api/templates/<id>":
            # Build the update kwargs from the body (only owner can update).
            kwargs: dict[str, Any] = {}
            for k in ("name", "description", "markdown", "kind", "tags"):
                if k in body:
                    kwargs[k] = body[k]
            if "is_public" in body:
                kwargs["is_public"] = bool(body["is_public"])
            if not kwargs:
                _json(handler, 400, {"error": "no updatable fields supplied"})
                return True
            try:
                tpl = update_template(tid, user_id, **kwargs)
            except ValueError as e:
                _json(handler, 400, {"error": str(e)})
                return True
            if tpl is None:
                existing = get_template(tid)
                if existing is None:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                else:
                    _json(handler, 403, {"error": "only the owner can update"})
                return True
            _json(handler, 200, {"template": tpl})
            return True
        _json(handler, 404, {"error": "unknown PATCH route"})
        return True

    # -----------------------------------------------------------------
    # DELETE routes
    # -----------------------------------------------------------------
    if method == "DELETE":
        if route == "/api/templates/<id>":
            try:
                result = delete_template(tid, user_id)
            except KeyError:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            if result is None:
                _json(handler, 403, {"error": "only the owner can delete"})
                return True
            if not result:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            _json(handler, 200, {"ok": True})
            return True
        _json(handler, 404, {"error": "unknown DELETE route"})
        return True

    _json(handler, 405, {"error": f"method {method} not allowed"})
    return True
