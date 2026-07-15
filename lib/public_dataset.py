"""Public template + workspace storage via HuggingFace Datasets.

Stores PUBLIC templates as JSONL in a HF dataset repo so they're
accessible to all users across all Spaces. Hearts/downloads are
aggregated here (per-user tracking stays in local SQLite via
``favorites.py``).

Uses the ``huggingface_hub`` library for CRUD. Falls back gracefully
when offline or when no HF token is available (reads cached copy or
returns an empty result).

Design
------
* One JSONL file per kind: ``templates/templates.jsonl`` (and eventually
  ``workspaces/workspaces.jsonl``). Each line is a JSON object
  representing one public item with its current aggregate
  ``hearts`` and ``downloads`` counts.
* Reads (``list_public_templates``) download just that file (or use a
  cached copy from ``hf_hub_download`` with cache_dir).
* Writes (``publish_template`` / ``unpublish_template`` /
  ``add_heart`` / ``remove_heart`` / ``increment_downloads``) use a
  read-modify-write cycle guarded by a module-level lock. They:
    1. fetch the current JSONL (or start from empty if missing)
    2. mutate the in-memory list
    3. re-upload the JSONL atomically (``upload_file`` is atomic on the
       HF side — a commit either lands or doesn't)
  This is fine for the expected write rate (~1 write/sec at peak). For
  higher rates we'd switch to per-item JSON files + a merge step.
* ``HF_TOKEN`` env var is required for writes (anonymous reads work via
  the public dataset URL).
* ``PUBLIC_DATASET_REPO`` env var overrides the default repo name.

Graceful degradation
--------------------
* If ``HF_TOKEN`` is unset OR ``huggingface_hub`` import fails OR the
  dataset repo doesn't exist, ``list_public_templates`` returns ``[]``
  and the publish/heart/download mutators are silent no-ops (logged via
  ``debug_log.log_event``). The local SQLite templates table remains the
  source of truth for the user's OWN templates — only the GLOBAL PUBLIC
  library is degraded.
* Network errors are caught and logged; they never propagate to the
  caller. A read failure on the public dataset does not crash the
  template library.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# Default dataset repo. Override with the PUBLIC_DATASET_REPO env var.
# This is a SEPARATE dataset from the per-user private dataset
# ({hf_user}/doomalaysocreate-priv-{suffix}) used by dataset_persistence.py
# and from the metrics dataset (METRICS_PUBLIC_HF_REPO).
DATASET_REPO = os.environ.get(
    "PUBLIC_DATASET_REPO",
    "ScoobyBaby1999/doomalaysocreate-templates",
).strip()
HF_TOKEN = (os.environ.get("HF_TOKEN", "")
            or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()

# Path inside the dataset repo where the public templates JSONL lives.
TEMPLATES_PATH_IN_REPO = "templates/templates.jsonl"

# Cache TTL for reads (seconds). The cached JSONL is reused for this many
# seconds before re-fetching from HF. Set to 0 to disable caching.
READ_CACHE_TTL = int(os.environ.get("PUBLIC_DATASET_CACHE_TTL", "30"))

# Module-level write lock — serialises all read-modify-write cycles so
# concurrent heart/unheart events don't lose updates. Within a single
# Space process this is sufficient; cross-Space races are rare (the
# dataset is append-mostly and we re-fetch the latest revision on every
# write) and resolve on the next read.
_write_lock = threading.Lock()

# In-memory read cache: (timestamp, list_of_dicts).
_read_cache = None  # type: tuple[float, list[dict]] | None
_read_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: str, **fields: Any) -> None:
    """Best-effort structured log via the existing debug_log module."""
    try:
        import debug_log
        debug_log.log_event(event, **fields)
    except Exception:
        # Never break on logging failure.
        pass


def _hf_available() -> bool:
    """True iff we have both a token and the huggingface_hub library."""
    if not HF_TOKEN:
        return False
    try:
        import huggingface_hub  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_dataset_exists(api, repo_id: str) -> bool:
    """Create the dataset repo (public) if it doesn't yet exist.

    Returns True on success, False on failure (which is non-fatal — the
    first list_public_templates call will just return an empty list).
    """
    try:
        api.create_repo(repo_id=repo_id, repo_type="dataset",
                        private=False, exist_ok=True)
        return True
    except Exception as exc:
        _log("public_dataset_create_failed",
             repo=repo_id, error=repr(exc)[:200])
        return False


def _fetch_jsonl() -> list:
    """Download and parse the public templates JSONL.

    Returns ``[]`` if the file doesn't exist yet (first publish), the
    dataset is unreachable, or the file is empty/corrupt. Never raises.
    Uses an in-memory read cache (``READ_CACHE_TTL`` seconds) so rapid
    successive reads don't hammer the HF API.
    """
    global _read_cache
    # Fast path: serve from cache if fresh.
    with _read_cache_lock:
        if _read_cache is not None:
            ts, items = _read_cache
            if (time.time() - ts) < READ_CACHE_TTL:
                return list(items)
    if not _hf_available():
        return []
    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import (
            RepositoryNotFoundError,
            RevisionNotFoundError,
            EntryNotFoundError,
        )
    except ImportError:
        return []
    try:
        path = hf_hub_download(
            repo_id=DATASET_REPO,
            filename=TEMPLATES_PATH_IN_REPO,
            repo_type="dataset",
            token=HF_TOKEN,
            cache_dir=os.environ.get("PUBLIC_DATASET_CACHE_DIR") or None,
        )
    except (RepositoryNotFoundError, RevisionNotFoundError, EntryNotFoundError):
        # First run — dataset or file doesn't exist yet. Cache the empty
        # result so we don't re-attempt every request.
        with _read_cache_lock:
            _read_cache = (time.time(), [])
        return []
    except OSError as exc:
        # Transient network error — don't cache, let the next call retry.
        _log("public_dataset_fetch_net_error", error=repr(exc)[:200])
        return []
    except Exception as exc:
        _log("public_dataset_fetch_error", error=repr(exc)[:200])
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue  # skip corrupt lines
    except OSError as exc:
        _log("public_dataset_read_error", error=repr(exc)[:200])
        return []
    with _read_cache_lock:
        _read_cache = (time.time(), list(items))
    return items


def _invalidate_cache() -> None:
    """Drop the read cache so the next read fetches fresh data."""
    global _read_cache
    with _read_cache_lock:
        _read_cache = None


def _upload_jsonl(items: list) -> bool:
    """Write the templates JSONL back to the dataset (atomic commit).

    Returns True on success, False on failure (logged, never raised).
    """
    if not _hf_available():
        return False
    try:
        from huggingface_hub import HfApi, upload_file
    except ImportError:
        return False
    api = HfApi(token=HF_TOKEN)
    _ensure_dataset_exists(api, DATASET_REPO)
    # Serialise to JSONL (one object per line, UTF-8, no trailing newline
    # on the last line is fine — readers handle it).
    payload = "\n".join(json.dumps(it, ensure_ascii=False) for it in items)
    if payload:
        payload += "\n"
    payload_bytes = payload.encode("utf-8")
    try:
        upload_file(
            path_or_fileobj=payload_bytes,
            path_in_repo=TEMPLATES_PATH_IN_REPO,
            repo_id=DATASET_REPO,
            repo_type="dataset",
            token=HF_TOKEN,
            commit_message="auto-sync public templates",
        )
        _invalidate_cache()
        return True
    except Exception as exc:
        _log("public_dataset_upload_error", error=repr(exc)[:200])
        return False


def _find(items: list, template_id: str):
    """Linear scan for a template by id. Returns (index, item_or_None)."""
    for i, it in enumerate(items):
        if it.get("id") == template_id:
            return (i, it)
    return (-1, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_public_templates(sort="hearts", query=None, kind=None,
                          limit=50, offset=0):
    """Read public templates from the HF dataset JSONL file.

    Returns (templates, total). ``total`` is the count BEFORE pagination.
    Templates are sorted by:
      * ``"hearts"``   — most hearted first (default), ties broken by
                         downloads then recency.
      * ``"recent"``   — newest first (by ``published_at`` then
                         ``created_at``).
      * ``"relevant"`` — exact-name-match boost, then hearts.

    Filtering:
      * ``query`` — case-insensitive substring match on name, description,
                    markdown, tags.
      * ``kind``  — exact match on ``kind`` (e.g. ``"websearch"``).

    Pagination via ``limit`` (1..200) and ``offset`` (>=0).

    On any failure (no token, network down, dataset missing) returns
    ``([], 0)`` — the caller falls back to local SQLite.
    """
    items = _fetch_jsonl()
    # Filter by kind.
    if kind:
        items = [it for it in items if it.get("kind") == kind]
    # Filter by text query.
    has_query = bool(query and query.strip())
    if has_query:
        q = query.strip().lower()
        def _matches(it: dict) -> bool:
            for field in ("name", "description", "markdown"):
                v = it.get(field)
                if isinstance(v, str) and q in v.lower():
                    return True
            tags = it.get("tags") or []
            if isinstance(tags, list):
                for t in tags:
                    if isinstance(t, str) and q in t.lower():
                        return True
            elif isinstance(tags, str) and q in tags.lower():
                return True
            return False
        items = [it for it in items if _matches(it)]
    total = len(items)
    # Sort.
    if sort == "recent":
        items.sort(key=lambda it: it.get("published_at") or it.get("created_at") or "",
                   reverse=True)
    elif sort == "relevant":
        if has_query:
            q = query.strip().lower()
            items.sort(key=lambda it: (
                0 if (isinstance(it.get("name"), str) and it["name"].lower().startswith(q)) else 1,
                -(int(it.get("hearts") or 0)),
                -(it.get("published_at") or it.get("created_at") or ""),
            ))
        else:
            # No query -> "relevant" degenerates to "hearts".
            sort = "hearts"
    if sort == "hearts":
        items.sort(key=lambda it: (
            -(int(it.get("hearts") or 0)),
            -(int(it.get("downloads") or 0)),
            -(it.get("published_at") or it.get("created_at") or ""),
        ))
    # Paginate.
    page_limit = max(1, min(int(limit), 200))
    page_offset = max(0, int(offset))
    page = items[page_offset: page_offset + page_limit]
    return (page, total)


def publish_template(template_dict: dict) -> bool:
    """Add or update a template in the public dataset.

    ``template_dict`` should include at least ``id``, ``name``,
    ``markdown``, ``kind``, ``author_id``, ``author_name``,
    ``hearts`` (int), ``downloads`` (int), and ``created_at`` /
    ``updated_at`` ISO timestamps. We stamp ``published_at`` on first
    publish and preserve it across updates.

    Returns True on success, False on failure (logged, never raised).
    """
    if not template_dict or not template_dict.get("id"):
        return False
    tid = template_dict["id"]
    with _write_lock:
        items = _fetch_jsonl()
        i, existing = _find(items, tid)
        # Build the public-record dict from the input. We keep only the
        # fields needed for the public listing (no per-user data, no
        # secrets). ``markdown`` IS included so the frontend can render
        # a preview without a second round-trip.
        now = _iso_now()
        record = {
            "id": tid,
            "name": template_dict.get("name", ""),
            "description": template_dict.get("description") or "",
            "markdown": template_dict.get("markdown", ""),
            "kind": template_dict.get("kind", "custom"),
            "tags": template_dict.get("tags") or [],
            "author_id": template_dict.get("author_id") or "anonymous",
            "author_name": template_dict.get("author_name") or "anonymous",
            "hearts": int(template_dict.get("hearts") or 0),
            "downloads": int(template_dict.get("downloads") or 0),
            "is_public": bool(template_dict.get("is_public", True)),
            "created_at": template_dict.get("created_at") or now,
            "updated_at": template_dict.get("updated_at") or now,
        }
        if existing and existing.get("published_at"):
            record["published_at"] = existing["published_at"]
        else:
            record["published_at"] = now
        if i >= 0:
            items[i] = record
        else:
            items.append(record)
        return _upload_jsonl(items)


def unpublish_template(template_id: str) -> bool:
    """Remove a template from the public dataset.

    Returns True on success, False on failure or if the template was not
    in the dataset (both are non-fatal).
    """
    if not template_id:
        return False
    with _write_lock:
        items = _fetch_jsonl()
        i, existing = _find(items, template_id)
        if i < 0:
            return True  # already absent — treat as success
        del items[i]
        return _upload_jsonl(items)


def add_heart(template_id: str) -> bool:
    """Increment the global heart count for a template in the public dataset."""
    return _bump_counter(template_id, "hearts", +1)


def remove_heart(template_id: str) -> bool:
    """Decrement the global heart count (clamped at 0)."""
    return _bump_counter(template_id, "hearts", -1)


def increment_downloads(template_id: str) -> bool:
    """Increment the global download count for a template."""
    return _bump_counter(template_id, "downloads", +1)


def _bump_counter(template_id: str, field: str, delta: int) -> bool:
    """Read-modify-write a single counter on one template record.

    Returns True on success, False on failure (logged, never raised).
    """
    if not template_id:
        return False
    with _write_lock:
        items = _fetch_jsonl()
        i, existing = _find(items, template_id)
        if i < 0 or existing is None:
            # Not in the public dataset — silently no-op. The local
            # SQLite count still gets bumped by the caller.
            return False
        current = int(existing.get(field) or 0)
        existing[field] = max(0, current + delta)
        existing["updated_at"] = _iso_now()
        items[i] = existing
        return _upload_jsonl(items)


def get_template(template_id: str):
    """Fetch one public template by id. Returns None if not found / unavailable."""
    if not template_id:
        return None
    items = _fetch_jsonl()
    _, it = _find(items, template_id)
    return it


def invalidate_cache() -> None:
    """Drop the in-memory read cache (used by tests / explicit refresh)."""
    _invalidate_cache()
