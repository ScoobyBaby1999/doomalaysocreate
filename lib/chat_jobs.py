"""Chat job queue + monitor — SQLite-backed async job runner for chat.

Ports the Next.js ``/api/chat/queue`` + ``/api/monitor`` endpoints:

  * POST   /api/chat/queue        — enqueue a chat job (prompt, provider, model,
                                     effort, mode, template). Returns {jobId}.
  * GET    /api/chat/queue?spaceId= — list queued/running/recent jobs for a space.
  * PATCH  /api/chat/queue         — cancel a job (body: {jobId}).
  * GET    /api/monitor?spaceId=   — SSE stream that polls for queued jobs, runs
                                     them, and streams live progress events.

Storage: SQLite via the existing db._db() connection (same DB file as the rest
of the service). Schema is created idempotently on first request.

Auth: same bearer token gate as the rest of /api/agent. Identity (GitHub
user_id) is read from the X-JWT header (None = anonymous job).

The runner uses the SAME panel + scheduler + research_templates the chat
endpoint uses, so jobs get the same model routing, reasoning params, and
privacy posture as interactive chat.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from typing import Any

import httpx

import db as _dbmod

_write_lock = _dbmod._write_lock


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_jobs (
    id              TEXT PRIMARY KEY,
    space_id        TEXT,
    user_id         TEXT,
    conversation_id TEXT,
    kind            TEXT NOT NULL DEFAULT 'chat',
    status          TEXT NOT NULL DEFAULT 'queued',
    prompt          TEXT NOT NULL,
    provider        TEXT,
    model           TEXT,
    effort          TEXT,
    mode            TEXT,
    template        TEXT,
    result          TEXT,
    error           TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning INTEGER NOT NULL DEFAULT 0,
    progress        REAL NOT NULL DEFAULT 0,
    stage           TEXT,
    suggestions     TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_jobs_space ON chat_jobs(space_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_jobs_status ON chat_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_jobs_user ON chat_jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_jobs_conversation ON chat_jobs(conversation_id);
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
            _schema_initialized = True


# ---------------------------------------------------------------------------
# Valid kinds + statuses
# ---------------------------------------------------------------------------

VALID_KINDS = ("chat", "research", "deep_research", "panel")
VALID_STATUSES = ("queued", "running", "complete", "error", "cancelled")
VALID_EFFORTS = ("low", "med", "high", "max")
VALID_TEMPLATES = ("breadth", "deep_dive", "compare", "fact_check")
VALID_MODES = ("default", "react", "extended_thinking")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def enqueue_job(*, prompt: str, space_id: str | None = None,
                user_id: str | None = None, conversation_id: str | None = None,
                kind: str = "chat", provider: str | None = None,
                model: str | None = None, effort: str | None = None,
                mode: str | None = None, template: str | None = None) -> dict:
    """Insert a new chat job row and return it. Status defaults to 'queued'."""
    _ensure_schema_once()
    if not prompt or not isinstance(prompt, str):
        raise ValueError("prompt (non-empty string) is required")
    if kind not in VALID_KINDS:
        kind = "chat"
    jid = _gen_id()
    now = _iso_now()
    db = _dbmod._db()
    with _write_lock:
        db.execute(
            "INSERT INTO chat_jobs (id, space_id, user_id, conversation_id, kind, "
            "status, prompt, provider, model, effort, mode, template, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (jid, space_id, user_id, conversation_id, kind,
             prompt, provider, model, effort, mode, template, now))
        db.commit()
    return get_job(jid) or {"id": jid, "status": "queued", "prompt": prompt,
                            "kind": kind, "created_at": now}


def get_job(job_id: str) -> dict | None:
    _ensure_schema_once()
    row = _dbmod._db().execute(
        "SELECT * FROM chat_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def list_jobs(*, space_id: str | None = None, user_id: str | None = None,
               limit: int = 50) -> list[dict]:
    """List jobs, optionally filtered by space_id (and user_id for ownership).

    Returns jobs in NEWEST-FIRST order. Includes queued/running + recently
    complete/error/cancelled.
    """
    _ensure_schema_once()
    db = _dbmod._db()
    if space_id:
        rows = db.execute(
            "SELECT * FROM chat_jobs WHERE space_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (space_id, limit)).fetchall()
    elif user_id:
        rows = db.execute(
            "SELECT * FROM chat_jobs WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM chat_jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_queued_jobs(*, limit: int = 20) -> list[dict]:
    """All jobs in 'queued' status, oldest-first (FIFO)."""
    _ensure_schema_once()
    rows = _dbmod._db().execute(
        "SELECT * FROM chat_jobs WHERE status = 'queued' "
        "ORDER BY created_at ASC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def cancel_job(job_id: str) -> dict | None:
    """Mark a queued/running job as cancelled. No-op if already terminal."""
    _ensure_schema_once()
    now = _iso_now()
    with _write_lock:
        cur = _dbmod._db().execute(
            "UPDATE chat_jobs SET status = 'cancelled', completed_at = ?, "
            "stage = COALESCE(stage, 'cancelled') "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (now, job_id))
        _dbmod._db().commit()
        if cur.rowcount == 0:
            return get_job(job_id)
    return get_job(job_id)


def update_job(job_id: str, *, status: str | None = None,
               result: dict | None = None, error: str | None = None,
               cost_usd: float | None = None,
               tokens_in: int | None = None, tokens_out: int | None = None,
               tokens_reasoning: int | None = None,
               progress: float | None = None, stage: str | None = None,
               suggestions: list[dict] | None = None,
               started_at: str | None = None,
               completed_at: str | None = None) -> dict | None:
    """Update job fields. Only sets fields that are not None."""
    _ensure_schema_once()
    fields: list[str] = []
    params: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if result is not None:
        fields.append("result = ?")
        params.append(json.dumps(result, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        params.append(error[:2000])
    if cost_usd is not None:
        fields.append("cost_usd = ?")
        params.append(float(cost_usd))
    if tokens_in is not None:
        fields.append("tokens_in = ?")
        params.append(int(tokens_in))
    if tokens_out is not None:
        fields.append("tokens_out = ?")
        params.append(int(tokens_out))
    if tokens_reasoning is not None:
        fields.append("tokens_reasoning = ?")
        params.append(int(tokens_reasoning))
    if progress is not None:
        fields.append("progress = ?")
        params.append(float(progress))
    if stage is not None:
        fields.append("stage = ?")
        params.append(stage[:120])
    if suggestions is not None:
        fields.append("suggestions = ?")
        params.append(json.dumps(suggestions, ensure_ascii=False))
    if started_at is not None:
        fields.append("started_at = ?")
        params.append(started_at)
    if completed_at is not None:
        fields.append("completed_at = ?")
        params.append(completed_at)
    if not fields:
        return get_job(job_id)
    params.append(job_id)
    with _write_lock:
        _dbmod._db().execute(
            f"UPDATE chat_jobs SET {', '.join(fields)} WHERE id = ?", params)
        _dbmod._db().commit()
    return get_job(job_id)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Parse JSON fields back to objects.
    for k in ("result", "suggestions"):
        v = d.get(k)
        if isinstance(v, str) and v:
            try:
                d[k] = json.loads(v)
            except (ValueError, TypeError):
                pass
    return d


# ---------------------------------------------------------------------------
# Job runner — used by the /api/monitor SSE endpoint
# ---------------------------------------------------------------------------

# Single background worker — the monitor endpoint polls for queued jobs and
# runs them one at a time (per space). A global lock prevents two monitor
# connections from running the same job.
_runner_lock = threading.Lock()
_running_jobs: set[str] = set()


def _claim_job(job_id: str) -> bool:
    """Atomically claim a job for running. Returns True if claimed."""
    with _runner_lock:
        if job_id in _running_jobs:
            return False
        _running_jobs.add(job_id)
        return True


def _release_job(job_id: str) -> None:
    with _runner_lock:
        _running_jobs.discard(job_id)


def run_job(job: dict, *, panel, emit) -> dict:
    """Run one chat job to completion. Emits SSE events via `emit`.

    The job's `kind` decides which path runs:
      - "chat"          : single LLM call via scheduler.call_slot
      - "research"      : research_templates.run_template (template required)
      - "deep_research" : research_templates.run_deep_research (mode required)
      - "panel"         : judge panel fan-out via jobs.run_panel_slots

    The emit callback receives {"event": "...", ...} dicts. The monitor SSE
    endpoint wraps these into SSE frames.
    """
    jid = job["id"]
    if not _claim_job(jid):
        emit({"event": "job_skipped", "jobId": jid, "reason": "already running"})
        return job

    now = _iso_now()
    update_job(jid, status="running", started_at=now, stage="starting", progress=0.01)
    emit({"event": "job_started", "jobId": jid, "job": get_job(jid)})

    try:
        result = asyncio.run(_run_job_async(job, panel=panel, emit=emit))
        return result
    except Exception as e:  # noqa: BLE001
        update_job(jid, status="error", error=f"{type(e).__name__}: {str(e)[:300]}",
                   completed_at=_iso_now(), progress=1.0, stage="error")
        emit({"event": "job_error", "jobId": jid,
              "error": f"{type(e).__name__}: {str(e)[:300]}"})
        return get_job(jid) or job
    finally:
        _release_job(jid)


async def _run_job_async(job: dict, *, panel, emit) -> dict:
    """The actual async work — picks a slot + dispatches by kind."""
    jid = job["id"]
    prompt = job["prompt"]
    kind = job.get("kind", "chat")
    effort = job.get("effort") or "med"
    template = job.get("template")
    mode = job.get("mode")
    provider = job.get("provider")
    model = job.get("model")

    # Pick a slot for the job. Prefer the explicit provider/model; else fall
    # back to the panel's default_panel[0] (a logical model with cross-provider
    # failover).
    picked = None
    logical = None
    extra_body = None
    who = None
    if provider and model:
        who = f"{provider}/{model}"
        picked = panel.resolve(who)
        if picked is None:
            # Try as a logical model name.
            logical, candidates = panel.resolve_candidates(model)
            if candidates:
                from scheduler import SlotScheduler
                picked = panel.scheduler.pick_slot_from(candidates, _PICK_ROLE) \
                    if hasattr(panel.scheduler, "pick_slot_from") else candidates[0]
        if picked is None:
            raise RuntimeError(f"could not resolve slot for {who!r}")
    elif model:
        logical, candidates = panel.resolve_candidates(model)
        if not candidates:
            raise RuntimeError(f"no configured providers host model {model!r}")
        picked = candidates[0]
    else:
        # Use the first entry of the default panel.
        if not panel.default_panel:
            raise RuntimeError("no provider/model configured on this Space")
        who = panel.default_panel[0]
        logical, candidates = panel.resolve_candidates(who)
        if not candidates:
            raise RuntimeError(f"default panel entry {who!r} has no candidates")
        picked = candidates[0]
    if logical is None:
        logical = getattr(picked, "who", None) or who

    # Resolve reasoning body from the catalog when effort is set.
    if effort and effort != "low":
        try:
            from providers import resolve_reasoning_body
            rcat = getattr(panel, "reasoning_catalog", None) or {}
            extra_body = resolve_reasoning_body(
                rcat, logical=logical, who=picked.who,
                family=getattr(picked, "model_family", None)) or None
        except Exception:
            extra_body = None

    # Wrap emit so each event carries the jobId.
    def _emit(ev: dict) -> None:
        ev = dict(ev)
        ev.setdefault("jobId", jid)
        # Map research_templates event types to monitor event types.
        et = ev.get("type") or ev.get("event") or ""
        if et == "status":
            stage = ev.get("stage") or ev.get("detail") or "running"
            update_job(jid, stage=stage, progress=min(0.9, _progress_for_stage(stage)))
            emit({"event": "job_progress", "jobId": jid, "stage": stage,
                  "detail": ev.get("detail", "")})
            return
        if et == "sources":
            items = ev.get("items", [])
            emit({"event": "job_sources", "jobId": jid, "sources": items})
            return
        if et == "thinking":
            emit({"event": "job_thinking", "jobId": jid,
                  "text": ev.get("text", "")})
            return
        if et == "delta":
            emit({"event": "job_delta", "jobId": jid,
                  "text": ev.get("text", "")})
            return
        if et == "error":
            emit({"event": "job_error", "jobId": jid,
                  "error": ev.get("error", "")})
            return
        # default: pass through
        emit({"event": f"job_{et}", "jobId": jid, **{k: v for k, v in ev.items() if k != "type"}})

    # Dispatch by kind.
    async with httpx.AsyncClient() as client:
        if kind == "research":
            if template not in VALID_TEMPLATES:
                raise ValueError(f"research kind requires template in {VALID_TEMPLATES}")
            from research_templates import run_template
            res = await run_template(template=template, question=prompt,
                                      client=client, picked=picked,
                                      extra_body=extra_body, emit=_emit, effort=effort)
        elif kind == "deep_research":
            from research_templates import run_deep_research
            res = await run_deep_research(mode=mode or "default",
                                           question=prompt, client=client,
                                           picked=picked, extra_body=extra_body,
                                           max_tokens=16384, timeout_s=1500.0,
                                           emit=_emit, effort=effort)
        elif kind == "panel":
            res = await _run_panel_job(job=job, panel=panel, client=client,
                                        emit=_emit, logical=logical, picked=picked,
                                        effort=effort)
        else:
            # "chat" — single call_slot.
            res = await _run_chat_job(prompt=prompt, client=client, picked=picked,
                                       extra_body=extra_body, emit=_emit, effort=effort)

    # Compute cost + persist.
    usage = res.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    reasoning_chars = len(usage.get("reasoning_content") or "")
    cost = _compute_cost(picked, in_tok, out_tok)

    # Generate follow-up suggestions.
    suggestions = await _generate_suggestions(panel=panel, picked=picked,
                                                client=client, prompt=prompt,
                                                output=res.get("output", ""),
                                                extra_body=extra_body)

    update_job(jid, status="complete" if res.get("ok") else "error",
               result=res, error=res.get("error"), cost_usd=cost,
               tokens_in=in_tok, tokens_out=out_tok,
               tokens_reasoning=reasoning_chars, progress=1.0,
               stage="complete", suggestions=suggestions,
               completed_at=_iso_now())
    emit({"event": "job_complete", "jobId": jid,
          "result": res, "suggestions": suggestions,
          "cost_usd": cost, "tokens": {"in": in_tok, "out": out_tok,
                                       "reasoning_chars": reasoning_chars}})
    return get_job(jid) or job


async def _run_chat_job(*, prompt: str, client: httpx.AsyncClient, picked,
                        extra_body: dict | None, emit, effort: str) -> dict:
    """Single LLM call. Streams the response as one delta (non-token streaming)."""
    from research_templates import _llm  # reuse the safe wrapper
    emit({"type": "status", "stage": "calling_model", "detail": "Calling model..."})
    sys_p = "You are a helpful assistant. Be concise and direct."
    content, usage, err = await _llm(client, picked, system=sys_p, user=prompt,
        max_tokens=4096, timeout_s=600.0, extra_body=extra_body)
    if err:
        return {"ok": False, "error": f"chat failed: {err}", "usage": usage}
    emit({"type": "delta", "text": content})
    return {"ok": True, "output": content, "usage": usage}


async def _run_panel_job(*, job: dict, panel, client: httpx.AsyncClient,
                         emit, logical: str, picked, effort: str) -> dict:
    """Fan out to the judge panel and merge."""
    from jobs import run_panel_slots, finalize_judges
    emit({"type": "status", "stage": "panel_fanout",
          "detail": "Fanning out to judge panel..."})
    who_list = list(panel.default_panel)
    if not who_list:
        return {"ok": False, "error": "no judges configured on this Space"}
    judges = await run_panel_slots(
        panel, who_list,
        system_prompt=("You are a judge panel. Each judge responds independently. "
                       "Provide a thorough answer to the user's prompt."),
        role_label="generator", effort=effort, profile="default",
        metrics=panel.metrics,
        user_msg=job["prompt"], max_tokens=8192, timeout_s=900.0,
        reasoning=False, research=False)
    finished, merged = finalize_judges(judges, "panel", "concat")
    emit({"type": "delta", "text": merged})
    in_tok = sum(int((j.get("usage") or {}).get("prompt_tokens") or 0) for j in finished)
    out_tok = sum(int((j.get("usage") or {}).get("completion_tokens") or 0) for j in finished)
    return {"ok": True, "output": merged, "judges": finished,
            "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok}}


async def _generate_suggestions(*, panel, picked, client: httpx.AsyncClient,
                                 prompt: str, output: str,
                                 extra_body: dict | None) -> list[dict]:
    """Generate 3-6 follow-up suggestions, sorted by creativity + importance."""
    from research_templates import _llm
    sys_p = ("You are a follow-up question generator. Based on the user's "
             "prompt and the assistant's response, generate 4-6 follow-up "
             "questions the user might want to ask next. For each, also rate "
             "creativity (0-10) and importance (0-10). Output a JSON array of "
             "objects: {'question': '...', 'creativity': N, 'importance': N}. "
             "No commentary.")
    user_s = (f"## User prompt\n{prompt[:1500]}\n\n"
              f"## Assistant response\n{output[:3000]}\n\n"
              f"## Follow-up suggestions (JSON array)")
    try:
        content, _usage, err = await _llm(client, picked, system=sys_p, user=user_s,
            max_tokens=512, timeout_s=60.0, extra_body=extra_body)
        if err:
            return []
        from research_templates import _parse_json_objects
        items = _parse_json_objects(content, ["question"])
        out = []
        for o in items:
            try:
                c = float(o.get("creativity", 5))
                i = float(o.get("importance", 5))
            except (TypeError, ValueError):
                c, i = 5.0, 5.0
            out.append({"question": str(o.get("question", ""))[:200],
                        "creativity": c, "importance": i})
        # Sort by combined creativity + importance, descending.
        out.sort(key=lambda s: s["creativity"] + s["importance"], reverse=True)
        return out[:6]
    except Exception:
        return []


def _compute_cost(picked, in_tok: int, out_tok: int) -> float:
    """Best-effort cost estimate from providers_catalog pricing (per 1M tokens)."""
    try:
        from providers import load_provider_catalog
        prov_name = picked.provider.name
        for entry in load_provider_catalog():
            if entry.get("name") == prov_name:
                pricing = (entry.get("pricing") or {})
                in_per_m = float(pricing.get("input_per_m", 0) or 0)
                out_per_m = float(pricing.get("output_per_m", 0) or 0)
                return round((in_tok * in_per_m + out_tok * out_per_m) / 1_000_000, 6)
    except Exception:
        pass
    return 0.0


def _progress_for_stage(stage: str) -> float:
    """Map a stage name to a rough progress fraction."""
    s = (stage or "").lower()
    table = {
        "starting": 0.05, "decompose": 0.10, "initial_search": 0.15,
        "searching": 0.30, "hop": 0.40, "reading": 0.50, "followup": 0.55,
        "followups": 0.55, "followup_search": 0.60, "verifying": 0.65,
        "identify_perspectives": 0.20, "extract_claims": 0.20,
        "react_loop": 0.50, "extended_thinking": 0.60,
        "synthesizing": 0.80, "calling_model": 0.50, "panel_fanout": 0.50,
        "complete": 1.0, "error": 1.0,
    }
    return table.get(s, 0.30)


# Re-export the pick role used by the scheduler.
from content.roles import Roles as _Roles  # noqa: E402
_PICK_ROLE = _Roles("critiquer")


# ---------------------------------------------------------------------------
# HTTP dispatch — called from critique_service.Handler
# ---------------------------------------------------------------------------

def handle_request(method: str, path: str, body: dict, handler) -> bool:
    """Dispatch /api/chat/queue routes. Returns True if handled."""
    from urllib.parse import parse_qs, urlsplit
    route = urlsplit(path).path.rstrip("/")
    if route != "/api/chat/queue":
        return False
    if not handler._auth_ok():
        handler._send_json(401, {"error": "missing or invalid bearer token"})
        return True
    user_id = _user_id_from_handler(handler)

    if method == "POST":
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            handler._send_json(400, {"error": "'prompt' (non-empty string) is required"})
            return True
        if len(prompt) > 100_000:
            handler._send_json(413, {"error": "'prompt' too large (max 100k chars)"})
            return True
        kind = str(body.get("kind", "chat")).strip().lower()
        if kind not in VALID_KINDS:
            handler._send_json(400, {"error": f"'kind' must be one of {VALID_KINDS}"})
            return True
        effort = body.get("effort")
        if effort and effort not in VALID_EFFORTS:
            handler._send_json(400, {"error": f"'effort' must be one of {VALID_EFFORTS}"})
            return True
        template = body.get("template")
        if template and template not in VALID_TEMPLATES:
            handler._send_json(400, {"error": f"'template' must be one of {VALID_TEMPLATES}"})
            return True
        mode = body.get("mode")
        if mode and mode not in VALID_MODES:
            handler._send_json(400, {"error": f"'mode' must be one of {VALID_MODES}"})
            return True
        try:
            job = enqueue_job(
                prompt=prompt, space_id=body.get("spaceId") or body.get("space_id"),
                user_id=user_id,
                conversation_id=body.get("conversationId") or body.get("conversation_id"),
                kind=kind, provider=body.get("provider"),
                model=body.get("model"), effort=effort, mode=mode, template=template)
        except ValueError as e:
            handler._send_json(400, {"error": str(e)})
            return True
        handler._send_json(202, {"jobId": job["id"], "job": job})
        return True

    if method == "GET":
        qs = parse_qs(urlsplit(path).query)
        space_id = (qs.get("spaceId", [""])[0] or qs.get("space_id", [""])[0] or "").strip()
        try:
            limit = int(qs.get("limit", ["50"])[0])
        except ValueError:
            limit = 50
        jobs = list_jobs(space_id=space_id or None, user_id=user_id if not space_id else None,
                         limit=limit)
        handler._send_json(200, {"jobs": jobs, "count": len(jobs)})
        return True

    if method == "PATCH":
        job_id = str(body.get("jobId", "")).strip()
        if not job_id:
            handler._send_json(400, {"error": "'jobId' is required"})
            return True
        job = cancel_job(job_id)
        if job is None:
            handler._send_json(404, {"error": "job not found"})
            return True
        handler._send_json(200, {"cancelled": job_id, "job": job})
        return True

    handler._send_json(405, {"error": f"method {method} not allowed"})
    return True


def _user_id_from_handler(handler) -> str | None:
    try:
        return handler._require_user_from_jwt()
    except Exception:
        return None
