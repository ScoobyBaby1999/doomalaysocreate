"""DB CRUD for the Conscious tables (Tier 3, Phase 1).

Thin wrappers over ``db._db()`` with the serializing ``db._write_lock``.
All mutations that touch the blackboard also mirror to the on-disk brain via
``brain.py``. The brain is canonical; the DB is a fast query/mirror layer.

Tables (see TIER3_PLAN.md §9):
  - conscious
  - conscious_agent
  - conscious_proposal           (defined before blackboard_entry due to FK)
  - conscious_blackboard_entry
  - conscious_drawer_entry
  - conscious_message
  - conscious_task
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import brain as _brain
import db as _dbmod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso_now() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _db():
    return _dbmod._db()


def _write_lock():
    return _dbmod._write_lock


def _brain_path_for_conscious(conscious_id: str) -> Path | None:
    """Resolve the .brain/ path for a conscious by looking up its workspace."""
    c = get_conscious(conscious_id)
    if not c:
        return None
    ws = _dbmod.get_workspace(c["workspace_id"])
    if not ws:
        return None
    return Path(ws["sandbox_path"]) / ".brain"


def _parse_json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


# ---------------------------------------------------------------------------
# conscious
# ---------------------------------------------------------------------------

def create_conscious(*, workspace_id: str, owner_user_id: str, title: str,
                     goal: str, cost_ceiling_usd: float = 0,
                     brain_commit_policy: str = "on",
                     max_agents: int = 8,
                     orchestrator_agent_id: str | None = None) -> dict:
    cid = _gen_id()
    now = _iso_now()
    db = _db()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious (id, workspace_id, owner_user_id, title, goal, "
            "orchestrator_agent_id, cost_ceiling_usd, cost_spent_usd, "
            "brain_commit_policy, graphiti_enabled, max_agents, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, workspace_id, owner_user_id, title, goal,
             orchestrator_agent_id, float(cost_ceiling_usd), 0.0,
             brain_commit_policy, 0, int(max_agents), "active", now, now))
        db.commit()
    return get_conscious(cid) or {}


def get_conscious(cid: str) -> dict | None:
    row = _db().execute("SELECT * FROM conscious WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def list_conscious(workspace_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM conscious WHERE workspace_id = ? AND status != 'archived' "
        "ORDER BY created_at DESC", (workspace_id,)).fetchall()
    return [dict(r) for r in rows]


def list_active_conscious_workspaces() -> list[dict]:
    """Return [{conscious_id, workspace_id, sandbox_path, brain_commit_policy}, ...]
    for all active conscious — used by dataset_persistence to walk brains.
    """
    rows = _db().execute(
        "SELECT c.id AS cid, c.workspace_id AS wid, c.brain_commit_policy AS pol, "
        "w.sandbox_path AS sandbox FROM conscious c "
        "JOIN workspaces w ON c.workspace_id = w.id "
        "WHERE c.status = 'active'", ()).fetchall()
    return [{"conscious_id": r["cid"], "workspace_id": r["wid"],
             "sandbox_path": r["sandbox"], "brain_commit_policy": r["pol"]}
            for r in rows]


def update_conscious(cid: str, **fields) -> dict | None:
    allowed = {"title", "goal", "cost_ceiling_usd", "brain_commit_policy",
               "status", "orchestrator_agent_id", "max_agents", "graphiti_enabled"}
    updates = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "graphiti_enabled":
            v = int(bool(v))
        elif k == "max_agents":
            v = int(v)
        elif k == "cost_ceiling_usd":
            v = float(v)
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        return get_conscious(cid)
    updates.append("updated_at = ?")
    params.append(_iso_now())
    params.append(cid)
    with _write_lock():
        _db().execute(f"UPDATE conscious SET {', '.join(updates)} WHERE id = ?", params)
        _db().commit()
    return get_conscious(cid)


def archive_conscious(cid: str) -> dict | None:
    return update_conscious(cid, status="archived")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def spawn_agent(*, conscious_id: str, role: str, model: str, tier: str,
                parent_agent_id: str | None = None,
                is_orchestrator: bool = False,
                subscribed_events: list[str] | None = None) -> dict:
    aid = _gen_id()
    now = _iso_now()
    subs = json.dumps(subscribed_events or [])
    db = _db()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_agent (id, conscious_id, role, model, tier, "
            "status, worktree_path, branch, parent_agent_id, subscribed_events, "
            "is_orchestrator, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, conscious_id, role, model, tier, "idle",
             None, None, parent_agent_id, subs, int(bool(is_orchestrator)),
             now, now))
        db.commit()
    agent = get_agent(aid) or {}
    # mirror to brain
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        _brain.write_agent_profile(bp, agent)
        _brain.append_event(bp, "agent.spawned",
                            f"{aid} ({role}/{model})", author=aid)
    return agent


def get_agent(aid: str) -> dict | None:
    row = _db().execute("SELECT * FROM conscious_agent WHERE id = ?", (aid,)).fetchone()
    return dict(row) if row else None


def list_agents(conscious_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM conscious_agent WHERE conscious_id = ? ORDER BY created_at",
        (conscious_id,)).fetchall()
    return [dict(r) for r in rows]


def update_agent(aid: str, **fields) -> dict | None:
    allowed = {"role", "status", "subscribed_events", "worktree_path", "branch"}
    updates = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "subscribed_events" and isinstance(v, list):
            v = json.dumps(v)
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        return get_agent(aid)
    updates.append("updated_at = ?")
    params.append(_iso_now())
    params.append(aid)
    with _write_lock():
        _db().execute(f"UPDATE conscious_agent SET {', '.join(updates)} WHERE id = ?", params)
        _db().commit()
    agent = get_agent(aid)
    if agent:
        bp = _brain_path_for_conscious(agent["conscious_id"])
        if bp is not None:
            _brain.write_agent_profile(bp, agent)
    return agent


def set_orchestrator(conscious_id: str, agent_id: str) -> dict | None:
    """Promote an agent to orchestrator. Clears is_orchestrator on all others."""
    now = _iso_now()
    with _write_lock():
        db = _db()
        db.execute("UPDATE conscious_agent SET is_orchestrator = 0, updated_at = ? "
                   "WHERE conscious_id = ?", (now, conscious_id))
        db.execute("UPDATE conscious_agent SET is_orchestrator = 1, updated_at = ? "
                   "WHERE id = ?", (now, agent_id))
        db.execute("UPDATE conscious SET orchestrator_agent_id = ?, updated_at = ? "
                   "WHERE id = ?", (agent_id, now, conscious_id))
        db.commit()
    c = get_conscious(conscious_id)
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        _brain.append_event(bp, "agent.promoted",
                            f"agent {agent_id} promoted to orchestrator",
                            author=agent_id)
    return c


# ---------------------------------------------------------------------------
# blackboard
# ---------------------------------------------------------------------------

def post_blackboard(*, conscious_id: str, section: str, key: str, value: str,
                    author_agent_id: str, committed_by_agent_id: str,
                    proposal_id: str | None = None) -> dict:
    """Insert a blackboard entry with the next version, mirror to brain, append event."""
    db = _db()
    now = _iso_now()
    # compute next version
    row = db.execute(
        "SELECT MAX(version) AS mv FROM conscious_blackboard_entry "
        "WHERE conscious_id = ? AND section = ? AND key = ?",
        (conscious_id, section, key)).fetchone()
    next_version = (row["mv"] or 0) + 1 if row else 1
    eid = _gen_id()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_blackboard_entry (id, conscious_id, section, key, "
            "value, author_agent_id, committed_by_agent_id, proposal_id, version, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, conscious_id, section, key, value,
             author_agent_id, committed_by_agent_id, proposal_id,
             next_version, now))
        db.commit()
    entry = dict(db.execute("SELECT * FROM conscious_blackboard_entry WHERE id = ?",
                            (eid,)).fetchone())
    # mirror to brain
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        eid2 = _brain.append_blackboard(bp, section, key, value,
                                         author_agent_id, committed_by_agent_id,
                                         proposal_id, next_version, entry_id=eid)
        _brain.append_event(bp, "blackboard.posted",
                            f"{section}/{key} v{next_version} by {author_agent_id}",
                            author=author_agent_id, entry_id=eid)
    return entry


def get_blackboard(conscious_id: str, section: str | None = None,
                   key: str | None = None, since_version: int | None = None) -> list[dict]:
    """Return blackboard entries. If section+key given, returns all versions
    (ascending). Else returns latest version per (section, key)."""
    db = _db()
    if section and key:
        rows = db.execute(
            "SELECT * FROM conscious_blackboard_entry WHERE conscious_id = ? "
            "AND section = ? AND key = ? ORDER BY version ASC",
            (conscious_id, section, key)).fetchall()
        return [dict(r) for r in rows]
    # latest-per-key
    rows = db.execute(
        "SELECT b.* FROM conscious_blackboard_entry b "
        "INNER JOIN (SELECT section, key, MAX(version) AS mv "
        "            FROM conscious_blackboard_entry WHERE conscious_id = ? "
        "            GROUP BY section, key) m "
        "ON b.section = m.section AND b.key = m.key AND b.version = m.mv "
        "WHERE b.conscious_id = ? "
        + ("AND b.section = ? " if section else "")
        + "ORDER BY b.section, b.key",
        (conscious_id, conscious_id) + ((section,) if section else ())).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------

def create_proposal(*, conscious_id: str, proposer_agent_id: str,
                    section: str, key: str, value: str, reason: str) -> dict:
    pid = _gen_id()
    now = _iso_now()
    db = _db()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_proposal (id, conscious_id, proposer_agent_id, "
            "section, key, value, reason, status, committed_by_agent_id, "
            "rejection_reason, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, conscious_id, proposer_agent_id, section, key, value, reason,
             "pending", None, None, now, None))
        db.commit()
    prop = dict(db.execute("SELECT * FROM conscious_proposal WHERE id = ?",
                           (pid,)).fetchone())
    # mirror to brain + event
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        _brain.append_proposal(bp, prop)
        _brain.append_event(bp, "proposal.created",
                            f"proposal {pid} on {section}/{key} by {proposer_agent_id}",
                            author=proposer_agent_id, entry_id=pid)
    return prop


def list_proposals(conscious_id: str, status: str | None = None,
                   proposer_agent_id: str | None = None) -> list[dict]:
    db = _db()
    where = "conscious_id = ?"
    params: list[Any] = [conscious_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    if proposer_agent_id:
        where += " AND proposer_agent_id = ?"
        params.append(proposer_agent_id)
    rows = db.execute(
        f"SELECT * FROM conscious_proposal WHERE {where} ORDER BY created_at DESC",
        params).fetchall()
    return [dict(r) for r in rows]


def get_proposal(pid: str) -> dict | None:
    row = _db().execute("SELECT * FROM conscious_proposal WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def commit_proposal(pid: str, committer_agent_id: str) -> dict:
    """Apply a proposal: insert blackboard entry with author=proposer,
    committed_by=committer. Mark proposal committed."""
    prop = get_proposal(pid)
    if not prop:
        raise ValueError(f"proposal {pid} not found")
    if prop["status"] != "pending":
        raise ValueError(f"proposal {pid} is not pending (status={prop['status']})")
    entry = post_blackboard(
        conscious_id=prop["conscious_id"],
        section=prop["section"], key=prop["key"], value=prop["value"],
        author_agent_id=prop["proposer_agent_id"],
        committed_by_agent_id=committer_agent_id,
        proposal_id=pid)
    now = _iso_now()
    with _write_lock():
        _db().execute(
            "UPDATE conscious_proposal SET status = 'committed', "
            "committed_by_agent_id = ?, resolved_at = ? WHERE id = ?",
            (committer_agent_id, now, pid))
        _db().commit()
    bp = _brain_path_for_conscious(prop["conscious_id"])
    if bp is not None:
        _brain.append_event(bp, "proposal.committed",
                            f"proposal {pid} committed by {committer_agent_id}",
                            author=committer_agent_id, entry_id=pid)
    return {"entry": entry, "proposal": get_proposal(pid) or {}}


def reject_proposal(pid: str, committer_agent_id: str, reason: str) -> dict:
    prop = get_proposal(pid)
    if not prop:
        raise ValueError(f"proposal {pid} not found")
    now = _iso_now()
    with _write_lock():
        _db().execute(
            "UPDATE conscious_proposal SET status = 'rejected', "
            "committed_by_agent_id = ?, rejection_reason = ?, resolved_at = ? "
            "WHERE id = ?",
            (committer_agent_id, reason, now, pid))
        _db().commit()
    bp = _brain_path_for_conscious(prop["conscious_id"])
    if bp is not None:
        _brain.append_event(bp, "proposal.rejected",
                            f"proposal {pid} rejected by {committer_agent_id}: {reason}",
                            author=committer_agent_id, entry_id=pid)
    return get_proposal(pid) or {}


def create_conflict_proposal(*, conscious_id: str, agent_id: str,
                             file_path: str, ours: str, theirs: str) -> dict:
    """Phase 2: create a proposal describing a merge conflict for the orchestrator to resolve.

    The proposal's section="plan", key="conflict.<agent-id>.<file>". The value
    is a markdown description with both versions. The orchestrator commits a
    resolution via conscious_routes.apply_resolution_and_merge (which writes the
    resolved content onto the agent's branch and retries the merge).
    """
    value = (
        f"# Merge conflict: {file_path}\n\n"
        f"Agent `{agent_id}`'s branch conflicts with main on `{file_path}`.\n\n"
        f"## Ours (main)\n```\n{ours[:2000]}\n```\n\n"
        f"## Theirs (agent {agent_id})\n```\n{theirs[:2000]}\n```\n\n"
        f"Resolve by providing the final content for this file."
    )
    return create_proposal(
        conscious_id=conscious_id, proposer_agent_id=agent_id,
        section="plan", key=f"conflict.{agent_id}.{file_path}",
        value=value, reason=f"merge conflict on {file_path}")


# ---------------------------------------------------------------------------
# drawer
# ---------------------------------------------------------------------------

def create_drawer_entry(*, conscious_id: str, invoke_id: str,
                        from_agent_id: str, to_agent_id: str, kind: str,
                        task: str, inputs: dict | None = None) -> dict:
    now = _iso_now()
    inputs_json = json.dumps(inputs or {})
    db = _db()
    bp = _brain_path_for_conscious(conscious_id)
    result_path = str(bp / "drawer" / invoke_id / "result.md") if bp else None
    files_path = str(bp / "drawer" / invoke_id / "files") if bp else None
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_drawer_entry (id, conscious_id, invoke_id, "
            "from_agent_id, to_agent_id, kind, task, inputs, result, result_path, "
            "files_path, status, started_at, completed_at, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invoke_id, conscious_id, invoke_id, from_agent_id, to_agent_id,
             kind, task, inputs_json, "", result_path, files_path,
             "pending", now, None, None, now))
        db.commit()
    entry = dict(db.execute("SELECT * FROM conscious_drawer_entry WHERE id = ?",
                            (invoke_id,)).fetchone())
    # mirror to brain
    if bp is not None:
        request = {"invoke_id": invoke_id, "from_agent_id": from_agent_id,
                   "to_agent_id": to_agent_id, "task": task, "inputs": inputs or {},
                   "created_at": now, "kind": kind}
        _brain.create_drawer_entry(bp, invoke_id, request)
        _brain.append_event(bp, "drawer.created",
                            f"{kind} {invoke_id}: {from_agent_id} -> {to_agent_id}",
                            author=from_agent_id, entry_id=invoke_id)
    return entry


def complete_drawer_entry(invoke_id: str, result: str, status: str,
                          error: str | None = None) -> dict:
    now = _iso_now()
    with _write_lock():
        _db().execute(
            "UPDATE conscious_drawer_entry SET result = ?, status = ?, "
            "completed_at = ?, error = ? WHERE id = ?",
            (result, status, now, error, invoke_id))
        _db().commit()
    entry = dict(_db().execute("SELECT * FROM conscious_drawer_entry WHERE id = ?",
                                (invoke_id,)).fetchone())
    bp = _brain_path_for_conscious(entry["conscious_id"])
    if bp is not None:
        _brain.complete_drawer_entry(bp, invoke_id, result, status, error)
        _brain.append_event(bp, "drawer.completed",
                            f"{entry.get('kind')} {invoke_id} -> {status}",
                            author=entry.get("to_agent_id"), entry_id=invoke_id)
    return entry


def get_drawer_entry(invoke_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM conscious_drawer_entry WHERE id = ?",
                        (invoke_id,)).fetchone()
    return dict(row) if row else None


def list_drawer(conscious_id: str, to_agent_id: str | None = None,
                status: str | None = None, limit: int = 20) -> list[dict]:
    where = "conscious_id = ?"
    params: list[Any] = [conscious_id]
    if to_agent_id:
        where += " AND to_agent_id = ?"
        params.append(to_agent_id)
    if status:
        where += " AND status = ?"
        params.append(status)
    params.append(int(limit))
    rows = _db().execute(
        f"SELECT * FROM conscious_drawer_entry WHERE {where} "
        "ORDER BY created_at DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def send_message(*, conscious_id: str, from_agent_id: str,
                 to_agent_id: str | None, body: str) -> dict:
    mid = _gen_id()
    now = _iso_now()
    db = _db()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_message (id, conscious_id, from_agent_id, "
            "to_agent_id, body, read_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, conscious_id, from_agent_id, to_agent_id, body, None, now))
        db.commit()
    msg = dict(db.execute("SELECT * FROM conscious_message WHERE id = ?",
                          (mid,)).fetchone())
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        target = to_agent_id or "broadcast"
        _brain.append_event(bp, "message.sent",
                            f"{from_agent_id} -> {target}: {body[:80]}",
                            author=from_agent_id, entry_id=mid)
    return msg


def list_messages(conscious_id: str, to_agent_id: str | None = None,
                  since: str | None = None) -> list[dict]:
    where = "conscious_id = ?"
    params: list[Any] = [conscious_id]
    if to_agent_id:
        where += " AND (to_agent_id = ? OR to_agent_id IS NULL)"
        params.append(to_agent_id)
    if since:
        where += " AND created_at > ?"
        params.append(since)
    rows = _db().execute(
        f"SELECT * FROM conscious_message WHERE {where} ORDER BY created_at ASC",
        params).fetchall()
    return [dict(r) for r in rows]


def mark_message_read(message_id: str) -> None:
    with _write_lock():
        _db().execute("UPDATE conscious_message SET read_at = ? WHERE id = ?",
                      (_iso_now(), message_id))
        _db().commit()


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def create_task(*, conscious_id: str, title: str, description: str = "",
                assignee_agent_id: str | None = None,
                depends_on: list[str] | None = None,
                cost_ceiling_usd: float | None = None) -> dict:
    tid = _gen_id()
    now = _iso_now()
    deps = json.dumps(depends_on or [])
    db = _db()
    with _write_lock():
        db.execute(
            "INSERT INTO conscious_task (id, conscious_id, title, description, "
            "assignee_agent_id, status, depends_on, cost_ceiling_usd, cost_spent_usd, "
            "claimed_at, completed_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, conscious_id, title, description, assignee_agent_id,
             "pending", deps, cost_ceiling_usd, 0.0, None, None, now, now))
        db.commit()
    task = dict(db.execute("SELECT * FROM conscious_task WHERE id = ?", (tid,)).fetchone())
    _regenerate_plan(conscious_id)
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        _brain.append_event(bp, "task.created",
                            f"task {tid}: {title}", author=assignee_agent_id, entry_id=tid)
    return task


def get_task(tid: str) -> dict | None:
    row = _db().execute("SELECT * FROM conscious_task WHERE id = ?", (tid,)).fetchone()
    return dict(row) if row else None


def list_tasks(conscious_id: str, status: str | None = None,
               assignee: str | None = None) -> list[dict]:
    where = "conscious_id = ?"
    params: list[Any] = [conscious_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    if assignee:
        where += " AND assignee_agent_id = ?"
        params.append(assignee)
    rows = _db().execute(
        f"SELECT * FROM conscious_task WHERE {where} ORDER BY created_at ASC",
        params).fetchall()
    return [dict(r) for r in rows]


def update_task(tid: str, **fields) -> dict | None:
    allowed = {"title", "description", "assignee_agent_id", "status",
               "depends_on", "cost_ceiling_usd", "completed_at"}
    updates = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "depends_on" and isinstance(v, list):
            v = json.dumps(v)
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        return get_task(tid)
    updates.append("updated_at = ?")
    params.append(_iso_now())
    params.append(tid)
    with _write_lock():
        _db().execute(f"UPDATE conscious_task SET {', '.join(updates)} WHERE id = ?", params)
        _db().commit()
    task = get_task(tid)
    if task:
        _regenerate_plan(task["conscious_id"])
        bp = _brain_path_for_conscious(task["conscious_id"])
        if bp is not None:
            _brain.append_event(bp, "task.updated",
                                f"task {tid} -> {task.get('status', 'pending')}",
                                author=task.get("assignee_agent_id"), entry_id=tid)
    return task


def claim_task(tid: str, agent_id: str) -> dict:
    """Atomic claim: only succeeds if currently unclaimed."""
    now = _iso_now()
    with _write_lock():
        db = _db()
        cur = db.execute(
            "UPDATE conscious_task SET assignee_agent_id = ?, status = 'claimed', "
            "claimed_at = ?, updated_at = ? WHERE id = ? AND assignee_agent_id IS NULL",
            (agent_id, now, now, tid))
        if cur.rowcount == 0:
            existing = db.execute("SELECT assignee_agent_id FROM conscious_task WHERE id = ?",
                                  (tid,)).fetchone()
            other = existing["assignee_agent_id"] if existing else None
            db.commit()
            return {"error": f"already claimed by {other}" if other else "task not found"}
        db.commit()
    task = get_task(tid) or {}
    bp = _brain_path_for_conscious(task.get("conscious_id", ""))
    if bp is not None:
        _brain.append_event(bp, "task.claimed",
                            f"task {tid} claimed by {agent_id}",
                            author=agent_id, entry_id=tid)
    return task


def _regenerate_plan(conscious_id: str) -> None:
    bp = _brain_path_for_conscious(conscious_id)
    if bp is None:
        return
    tasks = list_tasks(conscious_id)
    _brain.regenerate_plan_json(bp, conscious_id, tasks)


# ---------------------------------------------------------------------------
# events / context
# ---------------------------------------------------------------------------

def append_event(conscious_id: str, key: str, value: str,
                 author_agent_id: str | None = None) -> int:
    """Append an event row to the blackboard (section='event') + mirror to brain."""
    # store as blackboard entry section='event' so it's queryable; the brain
    # events.jsonl gets a separate monotonic-id line.
    bp = _brain_path_for_conscious(conscious_id)
    if bp is not None:
        return _brain.append_event(bp, key, value, author=author_agent_id)
    return 0


def get_events(conscious_id: str, since: int = 0) -> list[dict]:
    bp = _brain_path_for_conscious(conscious_id)
    if bp is None:
        return []
    return _brain.read_events(bp, since=since)


def get_context(conscious_id: str, agent_id: str, since: int = 0,
                sections: list[str] | None = None,
                include_drawer: bool = False,
                include_messages: bool = True,
                include_proposals: bool = True,
                include_tasks: bool = True,
                bb_since: str | None = None) -> dict:
    """Assemble the conscious_context response. Filters by agent role:
    orchestrator sees all proposals + all messages; sub-agents see own
    proposals + messages addressed to them (or broadcasts).

    Phase 3 tighter delta-sync: ``bb_since`` is an ISO-8601 timestamp from a
    previous call's ``bb_cursor``. When provided, only blackboard entries with
    ``created_at > bb_since`` are returned (instead of the full snapshot).
    The new ``bb_cursor`` is the max ``created_at`` of the entries that *would*
    have been returned (so the caller can pass it next time even if nothing
    changed this turn)."""
    c = get_conscious(conscious_id) or {}
    agent = get_agent(agent_id) or {}
    is_orch = bool(agent.get("is_orchestrator"))
    agents = list_agents(conscious_id)
    orch_agent = next((a for a in agents if a.get("is_orchestrator")), None)

    # blackboard (latest per key, optionally filtered by section).
    # Phase 3: when bb_since is given, only return entries whose created_at is
    # newer — so repeated calls don't re-transmit unchanged keys.
    bb_rows = get_blackboard(conscious_id)
    bb_out: dict[str, list[dict]] = {}
    bb_max_ts = bb_since or ""
    for r in bb_rows:
        sec = r["section"]
        if sections and sec not in sections and sec != "event":
            continue
        if sec == "event":
            continue  # events are returned separately
        # Phase 3 delta filter
        r_ts = r.get("created_at", "") or ""
        if bb_since and r_ts <= bb_since:
            continue
        if r_ts > bb_max_ts:
            bb_max_ts = r_ts
        bb_out.setdefault(sec, []).append({
            "id": r["id"], "section": sec, "key": r["key"], "value": r["value"],
            "version": r["version"], "author": r["author_agent_id"],
            "committed_by": r["committed_by_agent_id"],
            "proposal_id": r.get("proposal_id"),
            "created_at": r["created_at"],
        })

    # events since cursor
    events_raw = get_events(conscious_id, since=since)
    # filter events by this agent's subscriptions
    subs = _parse_json_list(agent.get("subscribed_events", "[]"))
    if subs:
        events = [e for e in events_raw if any(_prefix_match(s, e.get("key", "")) for s in subs)]
    else:
        events = events_raw  # no subscriptions = see all (default for orchestrator)
    new_cursor = max((e.get("id", 0) for e in events_raw), default=since)

    # pings = subset of events that matched subscriptions
    pings = [{"event_id": e.get("id"), "type": e.get("key"),
              "summary": e.get("value", "")[:120]} for e in events]

    # messages
    messages: list[dict] = []
    if include_messages:
        if is_orch:
            msgs = list_messages(conscious_id)
        else:
            msgs = list_messages(conscious_id, to_agent_id=agent_id)
        messages = [{"id": m["id"], "from": m["from_agent_id"],
                     "to": m.get("to_agent_id"), "body": m["body"],
                     "created_at": m["created_at"]} for m in msgs]

    # proposals
    proposals: list[dict] = []
    if include_proposals:
        if is_orch:
            props = list_proposals(conscious_id, status="pending")
        else:
            props = list_proposals(conscious_id, status="pending",
                                   proposer_agent_id=agent_id)
        proposals = [{"id": p["id"], "section": p["section"], "key": p["key"],
                      "value": p["value"], "reason": p["reason"],
                      "proposer": p["proposer_agent_id"], "status": p["status"],
                      "created_at": p["created_at"]} for p in props]

    # tasks
    tasks: list[dict] = []
    if include_tasks:
        ts = list_tasks(conscious_id)
        tasks = [{"id": t["id"], "title": t["title"], "status": t["status"],
                  "assignee": t.get("assignee_agent_id"),
                  "deps": _parse_json_list(t.get("depends_on", "[]"))} for t in ts]

    # drawer
    drawer: list[dict] = []
    if include_drawer:
        ds = list_drawer(conscious_id, limit=20)
        drawer = [{"invoke_id": d["invoke_id"], "from": d.get("from_agent_id"),
                   "to": d.get("to_agent_id"), "status": d.get("status"),
                   "summary": (d.get("result") or "")[:200]} for d in ds]

    return {
        "conscious_id": conscious_id,
        "cursor": new_cursor,
        "bb_cursor": bb_max_ts or None,  # Phase 3 — pass as bb_since next time
        "goal": c.get("goal", ""),
        "orchestrator": ({"id": orch_agent["id"], "role": orch_agent["role"],
                          "model": orch_agent["model"]} if orch_agent else None),
        "self": {"id": agent_id, "role": agent.get("role", ""),
                 "model": agent.get("model", ""),
                 "is_orchestrator": is_orch},
        "blackboard": bb_out,
        "events": events,
        "pings": pings,
        "messages": messages,
        "proposals": proposals,
        "tasks": tasks,
        "drawer": drawer,
    }


def _prefix_match(prefix: str, key: str) -> bool:
    """Match `proposal.*` against `proposal.created`. Trailing `*` is wildcard."""
    if prefix.endswith(".*"):
        return key.startswith(prefix[:-1])  # 'proposal.' prefix
    if prefix.endswith("*"):
        return key.startswith(prefix[:-1])
    return prefix == key
