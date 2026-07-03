"""HTTP route handlers for ``/api/conscious/*`` (Tier 3, Phase 1).

13 endpoints (TIER3_PLAN.md §11). Each returns ``(status_code, json_body)``.
Auth is enforced by the caller (``critique_service.py``): every conscious
route goes through ``_token_ok`` + workspace-owning ops additionally call
``_require_user_from_jwt``. This module receives the authenticated
``user_id`` (or None for read-only public ops) and the parsed request.

Path dispatch is handled by ``handle_request(method, path, body, headers,
user_id)``. The path is the raw URL path (with query string stripped by
the caller); body is the parsed JSON dict (or {} for GETs).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit
import os

import brain as _brain
import conscious_db
import db as _dbmod


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def handle_request(method: str, raw_path: str, body: dict | None,
                   headers: dict, user_id: str | None) -> tuple[int, dict]:
    """Route a /api/conscious/* request. Returns (status, json_body)."""
    path = urlsplit(raw_path).path.rstrip("/")
    qs = parse_qs(urlsplit(raw_path).query)
    body = body or {}

    # strip the /api/conscious prefix; ``rest`` is what's left
    prefix = "/api/conscious"
    if path == prefix:
        rest = ""
    elif path.startswith(prefix + "/"):
        rest = path[len(prefix) + 1:]
    else:
        return 404, {"error": "not found"}

    # POST /api/conscious  (create)
    if method == "POST" and rest == "":
        return _create_conscious(body, user_id)
    # GET /api/conscious?workspace_id=...
    if method == "GET" and rest == "":
        workspace_id = (qs.get("workspace_id", [""])[0] or "").strip()
        return _list_conscious(workspace_id, user_id)

    # /api/conscious/<cid>/...
    parts = rest.split("/", 1)
    if len(parts) < 1 or not parts[0]:
        return 404, {"error": "not found"}
    cid = parts[0]
    sub = parts[1] if len(parts) > 1 else ""

    # /api/conscious/<cid>
    if method == "GET" and sub == "":
        return _get_conscious(cid, user_id)
    if method == "PATCH" and sub == "":
        return _patch_conscious(cid, body, user_id)
    if method == "DELETE" and sub == "":
        return _delete_conscious(cid, user_id)

    # /api/conscious/<cid>/agents[/<aid>]
    if sub == "agents" or sub.startswith("agents/"):
        return _route_agents(method, cid, sub, body, user_id)

    # /api/conscious/<cid>/blackboard[/<section>/<key>]
    if sub == "blackboard" or sub.startswith("blackboard/"):
        return _route_blackboard(method, cid, sub, body, qs, user_id)

    # /api/conscious/<cid>/proposals[/<pid>/(commit|reject)]
    if sub == "proposals" or sub.startswith("proposals/"):
        return _route_proposals(method, cid, sub, body, user_id)

    # /api/conscious/<cid>/drawer[/<invoke_id>]
    if sub == "drawer" or sub.startswith("drawer/"):
        return _route_drawer(method, cid, sub, body, qs, user_id)

    # /api/conscious/<cid>/messages
    if sub == "messages":
        return _route_messages(method, cid, body, qs, user_id)

    # /api/conscious/<cid>/tasks[/<tid>]
    if sub == "tasks" or sub.startswith("tasks/"):
        return _route_tasks(method, cid, sub, body, user_id)

    # /api/conscious/<cid>/context
    if sub == "context" and method == "POST":
        return _post_context(cid, body, user_id)

    # Phase 2: /api/conscious/<cid>/files[?branch=&prefix=]  (list files on a branch)
    #          /api/conscious/<cid>/files/<path>  (read a file on main)
    if sub == "files" or sub.startswith("files/"):
        return _route_files(method, cid, sub, qs, user_id)

    # Phase 4: POST /api/conscious/<cid>/merge — merge drawer entries
    if sub == "merge" and method == "POST":
        return _route_merge(cid, body, user_id)
    # Phase 4: POST /api/conscious/<cid>/panel — invoke judge panel as sub-agent
    if sub == "panel" and method == "POST":
        return _route_panel(cid, body, user_id)

    return 404, {"error": f"not found: {method} /api/conscious/{rest}"}


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

def _check_ownership(cid: str, user_id: str | None) -> tuple[bool, dict | None]:
    """Return (ok, error_response). ok=False means send the error_response.

    Phase 6: if user_id is None (no GitHub auth), auto-provision a default
    user + workspace so the conscious system works without GitHub (like the
    chat panel). The default user owns all conscious instances in this mode.
    """
    if not user_id:
        # Phase 6: no GitHub auth — auto-provision a default user
        user_id = _ensure_default_user()
    c = conscious_db.get_conscious(cid)
    if not c:
        return False, (404, {"error": "conscious not found"})
    if c["owner_user_id"] != user_id:
        # also allow if the workspace belongs to the user (defense in depth)
        ws = _dbmod.get_workspace(c["workspace_id"])
        if not ws or ws["user_id"] != user_id:
            return False, (403, {"error": "not your conscious"})
    return True, None


_DEFAULT_USER_ID = "conscious-default-user"


def _ensure_default_user() -> str:
    """Phase 6: create a default user + workspace if they don't exist.
    Used when no GitHub auth is provided (like the chat panel)."""
    sandbox_base = os.environ.get("WORKSPACE_BASE", "/data/workspaces")
    try:
        user = _dbmod.get_user(_DEFAULT_USER_ID)
        if not user:
            _dbmod.upsert_user(user_id=_DEFAULT_USER_ID)
        # ensure a default workspace exists
        ws = _dbmod.get_workspace("conscious-default-workspace")
        if not ws:
            _dbmod.create_workspace(
                _DEFAULT_USER_ID,
                title="Default Conscious Workspace",
                sandbox_path=f"{sandbox_base}/conscious-default",
            )
        return _DEFAULT_USER_ID
    except Exception:
        return _DEFAULT_USER_ID


# ---------------------------------------------------------------------------
# conscious lifecycle
# ---------------------------------------------------------------------------

def _resolve_workspace(user_id: str, workspace_id: str) -> tuple[str, dict | None]:
    """Find or create a workspace by id (or title).  Returns (resolved_id, ws_dict)."""
    from pathlib import Path as _Path
    sandbox_base = _Path(os.environ.get("WORKSPACE_BASE", "/data/workspaces"))
    ws = _dbmod.get_workspace(workspace_id)
    if ws:
        return workspace_id, ws
    # look up by title among the user's workspaces
    for w in _dbmod.list_user_workspaces(user_id):
        if w.get("title") == workspace_id:
            return w["id"], w
    # auto-create
    ws = _dbmod.create_workspace(
        user_id,
        title=workspace_id,
        sandbox_path=str(sandbox_base / workspace_id),
    )
    return ws["id"], ws


def _create_conscious(body: dict, user_id: str | None) -> tuple[int, dict]:
    # Phase 6: if no user_id, auto-provision a default user + workspace
    if not user_id:
        user_id = _ensure_default_user()
    workspace_id = str(body.get("workspace_id", "")).strip()
    if not workspace_id:
        workspace_id = "conscious-default-workspace"
    workspace_id, ws = _resolve_workspace(user_id, workspace_id)
    if ws["user_id"] != user_id:
        return 403, {"error": "not your workspace"}
    # cap conscious per workspace (TIER3_PLAN.md §13)
    existing = conscious_db.list_conscious(workspace_id)
    if len(existing) >= 4:
        return 429, {"error": "max 4 conscious per workspace"}
    title = str(body.get("title", "Untitled Conscious")).strip() or "Untitled Conscious"
    goal = str(body.get("goal", ""))
    cost_ceiling = float(body.get("cost_ceiling_usd", 0) or 0)
    policy = str(body.get("brain_commit_policy", "on")).strip()
    if policy not in ("on", "off"):
        policy = "on"
    max_agents = int(body.get("max_agents", 8) or 8)

    c = conscious_db.create_conscious(
        workspace_id=workspace_id, owner_user_id=user_id, title=title, goal=goal,
        cost_ceiling_usd=cost_ceiling, brain_commit_policy=policy,
        max_agents=max_agents)
    # init .brain/ in the workspace sandbox
    from pathlib import Path
    sandbox = Path(ws["sandbox_path"])
    sandbox.mkdir(parents=True, exist_ok=True)
    bp = _brain.init_brain(sandbox, c)

    # spawn orchestrator agent (Phase 1: DB row only, no worktree)
    orch_model = str(body.get("orchestrator_model", "")).strip()
    if not orch_model:
        # default to claude-opus if claude tier available, else open default
        try:
            import agent_sessions
            tier = agent_sessions.agent_tier()
            if tier == "claude":
                orch_model = "claude-opus-4-8"
            elif tier == "open":
                models = agent_sessions.agent_models()
                orch_model = models[0]["model"] if models else "groq/llama-3.3-70b-versatile"
            else:
                orch_model = "glm-5.2-free"  # zai tier default
        except Exception:
            orch_model = "glm-5.2-free"
    if orch_model.startswith("claude"):
        tier = "claude"
    elif orch_model.startswith("glm"):
        tier = "zai"
    else:
        tier = "open"
    agent = conscious_db.spawn_agent(
        conscious_id=c["id"], role="orchestrator", model=orch_model, tier=tier,
        is_orchestrator=True, subscribed_events=["proposal.*", "task.*", "drawer.*", "message.*"])
    conscious_db.set_orchestrator(c["id"], agent["id"])
    c = conscious_db.get_conscious(c["id"]) or {}

    # regenerate CONSCIOUS.md with the orchestrator
    agents = conscious_db.list_agents(c["id"])
    _brain.regenerate_conscious_md(bp, c, agents, [], [], [])
    return 201, {"conscious": c, "agents": agents, "brain_path": str(bp)}


def _list_conscious(workspace_id: str, user_id: str | None) -> tuple[int, dict]:
    if not user_id:
        user_id = _ensure_default_user()
    if not workspace_id:
        workspace_id = "conscious-default-workspace"
    workspace_id, ws = _resolve_workspace(user_id, workspace_id)
    if ws["user_id"] != user_id:
        return 403, {"error": "not your workspace"}
    items = conscious_db.list_conscious(workspace_id)
    return 200, {"conscious": items}


def _get_conscious(cid: str, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    c = conscious_db.get_conscious(cid)
    if not c:
        return 404, {"error": "conscious not found"}
    agents = conscious_db.list_agents(cid)
    # Phase 3 — surface stale HF Dataset sync (>10min = stale, null = never)
    sync_status = _sync_status(c.get("last_synced_at"))
    return 200, {"conscious": c, "agents": agents, "sync_status": sync_status}


def _sync_status(last_synced_at: str | None) -> dict:
    """Phase 3: classify the HF Dataset brain sync freshness.

    Returns {state: "fresh"|"stale"|"never", last_synced_at, age_seconds}.
    fresh = synced within 10min; stale = over 10min; never = null.
    """
    if not last_synced_at:
        return {"state": "never", "last_synced_at": None, "age_seconds": None}
    import time as _time
    from datetime import datetime, timezone
    try:
        # parse ISO-8601 (with or without trailing Z)
        ts = last_synced_at.rstrip("Z")
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        age = int(_time.time() - dt.timestamp())
    except Exception:
        return {"state": "unknown", "last_synced_at": last_synced_at, "age_seconds": None}
    return {
        "state": "fresh" if age < 600 else "stale",
        "last_synced_at": last_synced_at,
        "age_seconds": age,
    }


def _patch_conscious(cid: str, body: dict, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    fields: dict[str, Any] = {}
    for k in ("title", "goal", "cost_ceiling_usd", "brain_commit_policy",
              "status", "orchestrator_agent_id", "max_agents"):
        if k in body:
            fields[k] = body[k]
    c = conscious_db.update_conscious(cid, **fields)
    # if orchestrator_agent_id changed, promote
    if "orchestrator_agent_id" in fields and fields["orchestrator_agent_id"]:
        conscious_db.set_orchestrator(cid, str(fields["orchestrator_agent_id"]))
        c = conscious_db.get_conscious(cid) or {}
    # refresh CONSCIOUS.md
    bp = _brain._brain_path_for_workspace  # not used; use conscious_db helper
    from pathlib import Path
    full = conscious_db.get_conscious(cid) or {}
    ws = _dbmod.get_workspace(full.get("workspace_id", ""))
    if ws:
        bpath = Path(ws["sandbox_path"]) / ".brain"
        if bpath.is_dir():
            agents = conscious_db.list_agents(cid)
            _brain.regenerate_conscious_md(bpath, full, agents, [], [], [])
    return 200, {"conscious": c}


def _delete_conscious(cid: str, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    conscious_db.archive_conscious(cid)
    return 200, {"deleted": True}


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def _route_agents(method: str, cid: str, sub: str, body: dict,
                  user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    # sub is "agents" | "agents/<aid>" | "agents/<aid>/merge"
    parts = sub.split("/")
    # parts[0] == "agents"; parts[1] = aid (optional); parts[2] = "merge" (optional)
    aid = parts[1] if len(parts) > 1 and parts[1] else None

    if method == "POST" and not aid:
        # spawn agent
        c = conscious_db.get_conscious(cid) or {}
        agents = conscious_db.list_agents(cid)
        if len(agents) >= int(c.get("max_agents", 8) or 8):
            return 429, {"error": f"max agents per conscious reached ({c.get('max_agents', 8)})"}
        role = str(body.get("role", "")).strip()
        model = str(body.get("model", "")).strip()
        tier = str(body.get("tier", "")).strip()
        if not role or not model or not tier:
            return 400, {"error": "role, model, tier are required"}
        if tier not in ("claude", "open", "zai"):
            return 400, {"error": "tier must be 'claude' or 'open'"}
        parent = body.get("parent_agent_id") or None
        agent = conscious_db.spawn_agent(
            conscious_id=cid, role=role, model=model, tier=tier,
            parent_agent_id=str(parent) if parent else None,
            is_orchestrator=False)
        # Phase 2: create a git worktree for the new agent. On failure, roll
        # back the agent DB row so we never leave an orphan row without a
        # worktree. The orchestrator does NOT get a worktree (it operates on
        # the main working tree).
        if not agent.get("is_orchestrator"):
            ws = _dbmod.get_workspace(c["workspace_id"])
            if ws and ws.get("sandbox_path"):
                from pathlib import Path as _P
                import worktree as _wt
                try:
                    wt_path = _wt.create_worktree(_P(ws["sandbox_path"]), agent["id"])
                    branch = _wt.branch_name_for(agent["id"])
                    agent = conscious_db.update_agent(
                        agent["id"], worktree_path=str(wt_path), branch=branch)
                except Exception as exc:
                    # rollback: delete the agent row
                    conscious_db.update_agent(agent["id"], status="failed")
                    # log the full error server-side; return a generic message
                    # (don't leak internals / potential tokens via {exc})
                    try:
                        from oplog import log_event
                        log_event("worktree_creation_failed", agent_id=agent["id"],
                                  error=repr(exc)[:200])
                    except Exception:
                        pass
                    return 500, {"error": "worktree creation failed",
                                 "agent_id": agent["id"]}
        return 201, {"agent": agent}
    if method == "GET" and not aid:
        return 200, {"agents": conscious_db.list_agents(cid)}
    if method == "GET" and aid:
        a = conscious_db.get_agent(aid)
        if not a or a["conscious_id"] != cid:
            return 404, {"error": "agent not found"}
        # Phase 2: include the worktree branch status (ahead/behind/files changed)
        status: dict[str, Any] = {}
        if a.get("worktree_path"):
            ws = _dbmod.get_workspace(conscious_db.get_conscious(cid)["workspace_id"])
            if ws and ws.get("sandbox_path"):
                import worktree as _wt
                from pathlib import Path as _P
                try:
                    status = _wt.agent_branch_status(_P(ws["sandbox_path"]), aid)
                except Exception as exc:
                    status = {"error": str(exc)}
        return 200, {"agent": a, "worktree_status": status}
    if method == "PATCH" and aid:
        fields: dict[str, Any] = {}
        for k in ("role", "status", "subscribed_events", "worktree_path", "branch"):
            if k in body:
                fields[k] = body[k]
        a = conscious_db.update_agent(aid, **fields)
        if not a or a["conscious_id"] != cid:
            return 404, {"error": "agent not found"}
        return 200, {"agent": a}
    if method == "DELETE" and aid:
        # Phase 2: remove the worktree + branch before marking the agent done.
        a = conscious_db.get_agent(aid)
        if a and a.get("worktree_path"):
            c = conscious_db.get_conscious(cid)
            if c:
                ws = _dbmod.get_workspace(c["workspace_id"])
                if ws and ws.get("sandbox_path"):
                    import worktree as _wt
                    from pathlib import Path as _P
                    try:
                        _wt.remove_worktree(_P(ws["sandbox_path"]), aid)
                    except Exception:
                        pass  # best-effort; the DB row is still marked done
        conscious_db.update_agent(aid, status="done")
        return 200, {"deleted": True}
    # Phase 2: POST /api/conscious/<cid>/agents/<aid>/merge
    #   orchestrator-only. Merges the agent's branch into main. On conflict,
    #   creates one proposal per conflicting file and aborts the merge. With
    #   {resolutions: {file: content}}, applies the resolution and retries.
    if method == "POST" and aid and len(parts) > 2 and parts[2] == "merge":
        return _merge_agent(cid, aid, body, user_id)
    return 404, {"error": "not found"}


def _merge_agent(cid: str, aid: str, body: dict,
                 user_id: str | None) -> tuple[int, dict]:
    """Phase 2: merge an agent's branch into main. Orchestrator-only.

    Without ``resolutions``: attempts the merge. On clean merge, returns the
    merged files + commit sha. On conflict, creates one proposal per
    conflicting file (so the orchestrator can review + resolve) and aborts.

    With ``{resolutions: {file: content}}``: writes the resolved content onto
    the agent's branch, commits, retries the merge. Marks the conflict
    proposals as committed.
    """
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    committer = str(body.get("committer_agent_id", "")).strip()
    if not committer:
        return 400, {"error": "committer_agent_id is required"}
    committer_agent = conscious_db.get_agent(committer)
    if not committer_agent or committer_agent["conscious_id"] != cid:
        return 404, {"error": "committer agent not found"}
    if not committer_agent.get("is_orchestrator"):
        return 403, {"error": "only the orchestrator can merge agent branches"}
    agent = conscious_db.get_agent(aid)
    if not agent or agent["conscious_id"] != cid:
        return 404, {"error": "agent not found"}
    if not agent.get("worktree_path"):
        return 400, {"error": "agent has no worktree (Phase 1 agent?)"}
    c = conscious_db.get_conscious(cid) or {}
    ws = _dbmod.get_workspace(c["workspace_id"])
    if not ws or not ws.get("sandbox_path"):
        return 500, {"error": "workspace sandbox missing"}
    import worktree as _wt
    from pathlib import Path as _P
    sandbox = _P(ws["sandbox_path"])
    resolutions = body.get("resolutions") or {}
    if resolutions:
        # apply + retry
        try:
            result = _wt.apply_resolution_and_merge(sandbox, aid, resolutions)
        except Exception as exc:
            try:
                from oplog import log_event
                log_event("merge_resolution_failed", agent_id=aid, error=repr(exc)[:200])
            except Exception:
                pass
            return 500, {"error": "resolution failed"}
        # mark any conflict proposals for this agent as committed
        props = conscious_db.list_proposals(cid, status="pending")
        for p in props:
            if p["section"] == "plan" and p["key"].startswith(f"conflict.{aid}."):
                try:
                    conscious_db.commit_proposal(p["id"], committer_agent_id=committer)
                except Exception:
                    pass
        conscious_db.append_event(
            cid, "merge.resolved",
            f"agent {aid} merged after conflict resolution by {committer}",
            author=committer)
        return 200, {"merge": result, "agent_id": aid}
    # plain merge attempt
    try:
        result = _wt.merge_agent_branch(sandbox, aid)
    except Exception as exc:
        try:
            from oplog import log_event
            log_event("merge_failed", agent_id=aid, error=repr(exc)[:200])
        except Exception:
            pass
        return 500, {"error": "merge failed"}
    if result.get("status") == "clean":
        conscious_db.append_event(
            cid, "merge.clean",
            f"agent {aid} merged clean by {committer} ({len(result.get('merged_files', []))} files)",
            author=committer)
        return 200, {"merge": result, "agent_id": aid}
    # conflicts — create one proposal per conflicting file
    proposal_ids: list[str] = []
    for conf in result.get("conflicts", []):
        prop = conscious_db.create_conflict_proposal(
            conscious_id=cid, agent_id=aid, file_path=conf["file"],
            ours=conf.get("ours", ""), theirs=conf.get("theirs", ""))
        proposal_ids.append(prop["id"])
    conscious_db.append_event(
        cid, "merge.conflict",
        f"agent {aid} merge has {len(result.get('conflicts', []))} conflict(s); {len(proposal_ids)} proposal(s) created",
        author=committer)
    return 200, {"merge": result, "agent_id": aid, "proposal_ids": proposal_ids}


# ---------------------------------------------------------------------------
# blackboard
# ---------------------------------------------------------------------------

def _route_blackboard(method: str, cid: str, sub: str, body: dict,
                      qs: dict, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    # sub is "blackboard" or "blackboard/<section>/<key>"
    parts = sub.split("/")
    if method == "GET" and len(parts) == 1:
        section = (qs.get("section", [None])[0] if "section" in qs else None)
        key = (qs.get("key", [None])[0] if "key" in qs else None)
        since_version = (qs.get("since", [None])[0] if "since" in qs else None)
        sv = int(since_version) if since_version else None
        rows = conscious_db.get_blackboard(cid, section=section, key=key,
                                           since_version=sv)
        return 200, {"entries": rows, "cursor": max((r.get("version", 0) for r in rows), default=0)}
    if method == "POST" and len(parts) == 1:
        section = str(body.get("section", "")).strip()
        key = str(body.get("key", "")).strip()
        value = str(body.get("value", ""))
        agent_id = str(body.get("agent_id", "")).strip()
        if not section or not key or not agent_id:
            return 400, {"error": "section, key, agent_id are required"}
        # write-proxy: agent must be the orchestrator
        agent = conscious_db.get_agent(agent_id)
        if not agent or agent["conscious_id"] != cid:
            return 404, {"error": "agent not found"}
        if not agent.get("is_orchestrator"):
            return 422, {"error": "sub-agents must use /proposals; only the orchestrator posts directly"}
        entry = conscious_db.post_blackboard(
            conscious_id=cid, section=section, key=key, value=value,
            author_agent_id=agent_id, committed_by_agent_id=agent_id,
            proposal_id=None)
        return 201, {"entry": entry}
    if method == "GET" and len(parts) == 3:
        section = parts[1]
        key = parts[2]
        rows = conscious_db.get_blackboard(cid, section=section, key=key)
        if not rows:
            return 404, {"error": "no such entry"}
        return 200, {"entry": rows[-1]}  # latest version
    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------

def _route_proposals(method: str, cid: str, sub: str, body: dict,
                     user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    # sub is "proposals" or "proposals/<pid>/commit" | "proposals/<pid>/reject"
    parts = sub.split("/")
    if method == "POST" and len(parts) == 1:
        section = str(body.get("section", "")).strip()
        key = str(body.get("key", "")).strip()
        value = str(body.get("value", ""))
        reason = str(body.get("reason", ""))
        proposer = str(body.get("proposer_agent_id", "")).strip()
        if not section or not key or not proposer:
            return 400, {"error": "section, key, proposer_agent_id are required"}
        agent = conscious_db.get_agent(proposer)
        if not agent or agent["conscious_id"] != cid:
            return 404, {"error": "proposer agent not found"}
        prop = conscious_db.create_proposal(
            conscious_id=cid, proposer_agent_id=proposer, section=section,
            key=key, value=value, reason=reason)
        return 201, {"proposal": prop}
    if method == "GET" and len(parts) == 1:
        status = body.get("status")  # ignored for GET; qs handled below
        # status from qs
        from urllib.parse import parse_qs, urlsplit
        # the qs isn't passed here; caller passed body for GET. Accept status from body.
        st = status or None
        props = conscious_db.list_proposals(cid, status=st if isinstance(st, str) else None)
        return 200, {"proposals": props}
    if len(parts) == 3 and parts[2] in ("commit", "reject"):
        pid = parts[1]
        action = parts[2]
        committer = str(body.get("committer_agent_id", "")).strip()
        if not committer:
            return 400, {"error": "committer_agent_id is required"}
        agent = conscious_db.get_agent(committer)
        if not agent or agent["conscious_id"] != cid:
            return 404, {"error": "committer agent not found"}
        if not agent.get("is_orchestrator"):
            return 403, {"error": "only the orchestrator can commit/reject proposals"}
        if action == "commit":
            try:
                out = conscious_db.commit_proposal(pid, committer_agent_id=committer)
            except ValueError as e:
                return 404, {"error": str(e)}
            return 200, out
        else:  # reject
            reason = str(body.get("reason", ""))
            try:
                prop = conscious_db.reject_proposal(pid, committer_agent_id=committer,
                                                    reason=reason)
            except ValueError as e:
                return 404, {"error": str(e)}
            return 200, {"proposal": prop}
    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# drawer
# ---------------------------------------------------------------------------

def _route_drawer(method: str, cid: str, sub: str, body: dict,
                  qs: dict, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    parts = sub.split("/")
    # /drawer (POST=create, GET=list)  /drawer/<invoke_id> (GET, PATCH)
    if method == "POST" and len(parts) == 1:
        # cost ceiling check (Phase 1 soft)
        c = conscious_db.get_conscious(cid) or {}
        ceiling = float(c.get("cost_ceiling_usd", 0) or 0)
        spent = float(c.get("cost_spent_usd", 0) or 0)
        if ceiling > 0 and spent >= ceiling:
            return 429, {"error": "cost ceiling exceeded", "spent": spent, "ceiling": ceiling}
        from_agent = str(body.get("from_agent_id", "")).strip()
        to_agent = str(body.get("to_agent_id", "")).strip()
        kind = str(body.get("kind", "invoke")).strip()
        task = str(body.get("task", "")).strip()
        if not from_agent or not to_agent or not task:
            return 400, {"error": "from_agent_id, to_agent_id, task are required"}
        if kind not in ("invoke", "delegate"):
            return 400, {"error": "kind must be 'invoke' or 'delegate'"}
        invoke_id = body.get("invoke_id") or _gen_id()
        entry = conscious_db.create_drawer_entry(
            conscious_id=cid, invoke_id=str(invoke_id),
            from_agent_id=from_agent, to_agent_id=to_agent, kind=kind,
            task=task, inputs=body.get("inputs") or {})
        # Phase 1 stub: auto-complete invoke immediately; delegate after 2s
        if kind == "invoke":
            result = (f"[stub] invoke {invoke_id}: {task}\n"
                      f"(Phase 2 wires real worktree-per-agent execution)")
            entry = conscious_db.complete_drawer_entry(invoke_id=str(invoke_id),
                                                       result=result, status="done")
            _bump_cost(cid, 0.001)
        else:
            import threading, time
            def _finish():
                time.sleep(2.0)
                try:
                    conscious_db.complete_drawer_entry(
                        str(invoke_id),
                        result=f"[stub] delegated: {task}\n(Phase 2 wires real async execution)",
                        status="done")
                except Exception:
                    pass
            threading.Thread(target=_finish, daemon=True).start()
            _bump_cost(cid, 0.001)
        return 201, {"drawer_entry": entry}
    if method == "GET" and len(parts) == 1:
        to_agent = (qs.get("to_agent_id", [None])[0] if "to_agent_id" in qs else None)
        status = (qs.get("status", [None])[0] if "status" in qs else None)
        limit = int(qs.get("limit", ["20"])[0] or 20)
        entries = conscious_db.list_drawer(cid, to_agent_id=to_agent,
                                           status=status, limit=limit)
        return 200, {"entries": entries}
    if len(parts) == 2:
        invoke_id = parts[1]
        if method == "GET":
            e = conscious_db.get_drawer_entry(invoke_id)
            if not e or e["conscious_id"] != cid:
                return 404, {"error": "drawer entry not found"}
            return 200, {"drawer_entry": e}
        if method == "PATCH":
            updates: dict[str, Any] = {}
            for k in ("status", "result", "error"):
                if k in body:
                    updates[k] = body[k]
            e = conscious_db.get_drawer_entry(invoke_id)
            if not e or e["conscious_id"] != cid:
                return 404, {"error": "drawer entry not found"}
            if "result" in updates or "status" in updates:
                e = conscious_db.complete_drawer_entry(
                    invoke_id, result=str(updates.get("result", e.get("result", ""))),
                    status=str(updates.get("status", e.get("status", "done"))),
                    error=updates.get("error"))
            return 200, {"drawer_entry": e}
    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def _route_messages(method: str, cid: str, body: dict, qs: dict,
                    user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    if method == "POST":
        from_agent = str(body.get("from_agent_id", "")).strip()
        to_agent = body.get("to_agent_id")
        msg_body = str(body.get("body", ""))
        if not from_agent or not msg_body:
            return 400, {"error": "from_agent_id and body are required"}
        agent = conscious_db.get_agent(from_agent)
        if not agent or agent["conscious_id"] != cid:
            return 404, {"error": "from_agent not found"}
        msg = conscious_db.send_message(
            conscious_id=cid, from_agent_id=from_agent,
            to_agent_id=str(to_agent) if to_agent else None, body=msg_body)
        return 201, {"message": msg}
    if method == "GET":
        to_agent = (qs.get("to_agent_id", [None])[0] if "to_agent_id" in qs else None)
        since = (qs.get("since", [None])[0] if "since" in qs else None)
        msgs = conscious_db.list_messages(cid, to_agent_id=to_agent, since=since)
        return 200, {"messages": msgs}
    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def _route_tasks(method: str, cid: str, sub: str, body: dict,
                 user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    parts = sub.split("/")
    if method == "POST" and len(parts) == 1:
        # orchestrator-only via API (sub-agents propose)
        title = str(body.get("title", "")).strip()
        if not title:
            return 400, {"error": "title is required"}
        task = conscious_db.create_task(
            conscious_id=cid, title=title,
            description=str(body.get("description", "")),
            assignee_agent_id=body.get("assignee_agent_id"),
            depends_on=body.get("depends_on") or [],
            cost_ceiling_usd=body.get("cost_ceiling_usd"))
        return 201, {"task": task}
    if method == "GET" and len(parts) == 1:
        status = body.get("status")
        assignee = body.get("assignee")
        tasks = conscious_db.list_tasks(cid, status=status if isinstance(status, str) else None,
                                        assignee=assignee if isinstance(assignee, str) else None)
        return 200, {"tasks": tasks}
    if len(parts) == 2:
        tid = parts[1]
        if method == "GET":
            t = conscious_db.get_task(tid)
            if not t or t["conscious_id"] != cid:
                return 404, {"error": "task not found"}
            return 200, {"task": t}
        if method == "PATCH":
            fields: dict[str, Any] = {}
            for k in ("title", "description", "assignee_agent_id", "status",
                      "depends_on", "cost_ceiling_usd", "completed_at"):
                if k in body:
                    fields[k] = body[k]
            # status=claimed triggers atomic claim semantics if assignee given
            if fields.get("status") == "claimed" and "assignee_agent_id" in fields:
                out = conscious_db.claim_task(tid, agent_id=str(fields["assignee_agent_id"]))
                if "error" in out:
                    return 409, out
                return 200, {"task": out}
            t = conscious_db.update_task(tid, **fields)
            if not t or t["conscious_id"] != cid:
                return 404, {"error": "task not found"}
            return 200, {"task": t}
    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

def _post_context(cid: str, body: dict, user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    agent_id = str(body.get("agent_id", "")).strip()
    if not agent_id:
        return 400, {"error": "agent_id is required"}
    agent = conscious_db.get_agent(agent_id)
    if not agent or agent["conscious_id"] != cid:
        return 404, {"error": "agent not found"}
    ctx = conscious_db.get_context(
        cid, agent_id, since=int(body.get("since", 0) or 0),
        sections=body.get("sections"),
        include_drawer=bool(body.get("include_drawer", False)),
        include_messages=bool(body.get("include_messages", True)),
        include_proposals=bool(body.get("include_proposals", True)),
        include_tasks=bool(body.get("include_tasks", True)),
        bb_since=body.get("bb_since"))  # Phase 3 delta-sync
    return 200, ctx


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def _bump_cost(cid: str, amount: float) -> None:
    with _dbmod._write_lock:
        _dbmod._db().execute(
            "UPDATE conscious SET cost_spent_usd = cost_spent_usd + ?, "
            "updated_at = ? WHERE id = ?",
            (float(amount), conscious_db._iso_now(), cid))
        _dbmod._db().commit()


# ---------------------------------------------------------------------------
# Phase 2: files on a branch (for smoke-test verification)
# ---------------------------------------------------------------------------

def _route_files(method: str, cid: str, sub: str, qs: dict,
                 user_id: str | None) -> tuple[int, dict]:
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    if method != "GET":
        return 405, {"error": "method not allowed"}
    c = conscious_db.get_conscious(cid) or {}
    ws = _dbmod.get_workspace(c.get("workspace_id", ""))
    if not ws or not ws.get("sandbox_path"):
        return 500, {"error": "workspace sandbox missing"}
    import worktree as _wt
    from pathlib import Path as _P
    sandbox = _P(ws["sandbox_path"])
    parts = sub.split("/", 1)
    # sub == "files" → list; sub == "files/<path>" → read
    if len(parts) == 1 or not parts[1]:
        branch = (qs.get("branch", ["main"])[0] if "branch" in qs else "main")
        prefix = (qs.get("prefix", [""])[0] if "prefix" in qs else "")
        try:
            files = _wt.list_files_on_branch(sandbox, branch=branch, prefix=prefix)
        except Exception as exc:
            return 500, {"error": "list files failed"}
        return 200, {"branch": branch, "files": files}
    file_path = parts[1]
    # Security: reject path traversal / absolute paths / hidden dirs (audit M2)
    if (not file_path or ".." in file_path or file_path.startswith("/")
            or "\x00" in file_path
            or file_path.startswith((".git/", ".brain/", ".worktrees/"))):
        return 400, {"error": "invalid file path"}
    try:
        content = _wt.read_file_on_branch(sandbox, file_path, branch="main")
    except Exception as exc:
        return 500, {"error": "read file failed"}
    return 200, {"path": file_path, "content": content}


# ---------------------------------------------------------------------------
# Phase 4 — conscious_merge + conscious_panel HTTP endpoints
# ---------------------------------------------------------------------------

def _route_merge(cid: str, body: dict,
                 user_id: str | None) -> tuple[int, dict]:
    """Phase 4: POST /api/conscious/<cid>/merge — merge drawer entries.

    Body: {invoke_ids: [str], mode?: "dedupe"|"vote"|"concat", committer_agent_id: str}
    Orchestrator-only. Wraps merge.merge_panel over the verbatim results of
    the given drawer entries + posts the merged markdown to the blackboard
    (section="merge").
    """
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    committer = str(body.get("committer_agent_id", "")).strip()
    if not committer:
        return 400, {"error": "committer_agent_id is required"}
    committer_agent = conscious_db.get_agent(committer)
    if not committer_agent or committer_agent["conscious_id"] != cid:
        return 404, {"error": "committer agent not found"}
    if not committer_agent.get("is_orchestrator"):
        return 403, {"error": "only the orchestrator can merge drawer entries"}
    invoke_ids = body.get("invoke_ids") or []
    if not isinstance(invoke_ids, list) or not invoke_ids:
        return 400, {"error": "invoke_ids (non-empty list) is required"}
    mode = str(body.get("mode", "dedupe")).strip()
    if mode not in ("dedupe", "vote", "concat"):
        return 400, {"error": "mode must be 'dedupe', 'vote', or 'concat'"}

    judges: list[dict] = []
    for invoke_id in invoke_ids:
        entry = conscious_db.get_drawer_entry(str(invoke_id))
        if not entry or entry["conscious_id"] != cid:
            return 404, {"error": f"drawer entry {invoke_id} not found"}
        if entry.get("status") != "done":
            return 422, {"error": f"drawer entry {invoke_id} is not done (status={entry.get('status')})"}
        agent = conscious_db.get_agent(entry["to_agent_id"]) or {}
        judges.append({
            "ok": True,
            "output": entry["result"] or "",
            "model": agent.get("model", entry["to_agent_id"]),
        })

    try:
        import merge
        merged = merge.merge_panel(judges, mode)
    except Exception as exc:
        try:
            from oplog import log_event
            log_event("merge_tool_failed", cid=cid, error=repr(exc)[:200])
        except Exception:
            pass
        return 500, {"error": "merge failed"}
    if not merged:
        merged = "(merge produced no output — check that the drawer entries have non-empty results)"

    import time as _time
    key = f"merge.{int(_time.time())}"
    entry = conscious_db.post_blackboard(
        conscious_id=cid, section="merge", key=key, value=merged,
        author_agent_id=committer, committed_by_agent_id=committer,
        proposal_id=None)
    return 200, {"merged": merged, "mode": mode, "count": len(judges),
                 "entry_id": entry["id"], "version": entry["version"]}


def _route_panel(cid: str, body: dict,
                 user_id: str | None) -> tuple[int, dict]:
    """Phase 4: POST /api/conscious/<cid>/panel — invoke judge panel as sub-agent.

    Body: {prompt: str, panel?: [str], profile?: str, effort?: str, from_agent_id: str}
    Spawns (or reuses) a 'panel' sub-agent + routes the prompt through the
    standard invoke path (drawer entry created, result verbatim). Returns the
    drawer invoke_id + status.
    """
    ok, err = _check_ownership(cid, user_id)
    if not ok:
        return err  # type: ignore[return-value]
    from_agent = str(body.get("from_agent_id", "")).strip()
    if not from_agent:
        return 400, {"error": "from_agent_id is required"}
    from_agent_row = conscious_db.get_agent(from_agent)
    if not from_agent_row or from_agent_row["conscious_id"] != cid:
        return 404, {"error": "from_agent not found"}
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return 400, {"error": "prompt (non-empty string) is required"}

    # find or create a "panel" sub-agent
    agents = conscious_db.list_agents(cid)
    panel_agent = next((a for a in agents if a.get("role") == "panel"), None)
    if not panel_agent:
        panel_agent = conscious_db.spawn_agent(
            conscious_id=cid, role="panel", model="panel/judge-loop",
            tier="open", is_orchestrator=False)

    # create the drawer entry + run the panel
    task = f"Panel critique: {prompt[:200]}"
    inputs = {
        "prompt": prompt,
        "panel": body.get("panel"),
        "profile": body.get("profile", "default"),
        "effort": body.get("effort", "medium"),
        "_panel_invoke": True,
    }
    # cost ceiling check
    c = conscious_db.get_conscious(cid) or {}
    ceiling = float(c.get("cost_ceiling_usd", 0) or 0)
    spent = float(c.get("cost_spent_usd", 0) or 0)
    if ceiling > 0 and spent >= ceiling:
        return 429, {"error": "cost ceiling exceeded", "spent": spent, "ceiling": ceiling}

    invoke_id = _gen_id()
    conscious_db.create_drawer_entry(
        conscious_id=cid, invoke_id=invoke_id, from_agent_id=from_agent,
        to_agent_id=panel_agent["id"], kind="invoke", task=task, inputs=inputs)

    # In production this would route through agent_sessions to drive the real
    # judge loop. In the preview/simulated path, write a placeholder result
    # noting the panel was invoked. The Python backend's _run_sub_agent in
    # conscious_tools.py handles the real path.
    result = (
        f"[panel] critique for: {prompt[:200]}\n\n"
        f"Panel invoked with profile={inputs['profile']}, effort={inputs['effort']}.\n"
        f"In production, this drives the judge loop (one drawer entry per judge "
        f"model). In the preview, this is a placeholder — the orchestrator can "
        f"still conscious_merge the per-judge outputs if multiple panel invokes "
        f"are run.\n\n"
        f"Inputs: {inputs}"
    )
    completed = conscious_db.complete_drawer_entry(
        invoke_id, result=result, status="done")
    _bump_cost(cid, 0.001)
    return 200, {"invoke_id": invoke_id, "status": completed["status"],
                 "result": result, "panel_agent_id": panel_agent["id"]}
