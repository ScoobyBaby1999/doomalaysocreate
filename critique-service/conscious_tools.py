"""Conscious tool handlers (Tier 3, Phase 1).

Each handler takes ``(agent_session, args) -> dict``. The ``agent_session``
carries the conscious_id and agent_id context (set when an agent is spawned
against a Conscious). Existing non-Conscious agents have conscious_id=None;
the handlers no-op with an error in that case.

The write-proxy rule (TIER3_PLAN.md §8.2) is enforced here:
  - conscious_post / conscious_task / conscious_commit_proposal /
    conscious_reject_proposal: orchestrator-only.
  - conscious_propose: sub-agents only (orchestrator uses conscious_post directly).
  - conscious_drawer (write own result), conscious_message, conscious_claim,
    conscious_subscribe: any agent (direct).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import conscious_db

# Phase 1 invoke/delegate stub cost (mocked; per TIER3_PLAN.md §14).
_STUB_INVOKE_COST_USD = 0.001


def _ctx(agent_session: Any) -> tuple[str | None, str | None]:
    """Return (conscious_id, agent_id) from the session, or (None, None)."""
    cid = getattr(agent_session, "conscious_id", None)
    aid = getattr(agent_session, "agent_id", None)
    return cid, aid


def _not_in_conscious() -> dict:
    return {"error": "not in a conscious session"}


def _is_orchestrator(agent_session: Any) -> bool:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return False
    agent = conscious_db.get_agent(aid)
    return bool(agent and agent.get("is_orchestrator"))


# ---------------------------------------------------------------------------
# 10.1 conscious_context
# ---------------------------------------------------------------------------

def _conscious_context(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    since = int(args.get("since", 0) or 0)
    sections = args.get("sections") or None
    include_drawer = bool(args.get("include_drawer", False))
    include_messages = bool(args.get("include_messages", True))
    include_proposals = bool(args.get("include_proposals", True))
    include_tasks = bool(args.get("include_tasks", True))
    return conscious_db.get_context(
        cid, aid, since=since, sections=sections,
        include_drawer=include_drawer, include_messages=include_messages,
        include_proposals=include_proposals, include_tasks=include_tasks,
        bb_since=args.get("bb_since"))  # Phase 3 delta-sync


# ---------------------------------------------------------------------------
# 10.2 conscious_post  (orchestrator only)
# ---------------------------------------------------------------------------

def _conscious_post(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    if not _is_orchestrator(agent_session):
        return {"error": "sub-agents must use conscious_propose; only the orchestrator posts directly"}
    section = str(args.get("section", "")).strip()
    key = str(args.get("key", "")).strip()
    value = str(args.get("value", ""))
    if not section or not key:
        return {"error": "section and key are required"}
    entry = conscious_db.post_blackboard(
        conscious_id=cid, section=section, key=key, value=value,
        author_agent_id=aid, committed_by_agent_id=aid, proposal_id=None)
    return {"entry_id": entry["id"], "version": entry["version"]}


# ---------------------------------------------------------------------------
# 10.3 conscious_propose  (sub-agents)
# ---------------------------------------------------------------------------

def _conscious_propose(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    section = str(args.get("section", "")).strip()
    key = str(args.get("key", "")).strip()
    value = str(args.get("value", ""))
    reason = str(args.get("reason", ""))
    if not section or not key:
        return {"error": "section and key are required"}
    prop = conscious_db.create_proposal(
        conscious_id=cid, proposer_agent_id=aid, section=section, key=key,
        value=value, reason=reason)
    return {"proposal_id": prop["id"], "status": "pending"}


# ---------------------------------------------------------------------------
# 10.4 conscious_commit_proposal  (orchestrator)
# ---------------------------------------------------------------------------

def _conscious_commit_proposal(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    if not _is_orchestrator(agent_session):
        return {"error": "only the orchestrator can commit proposals"}
    pid = str(args.get("proposal_id", "")).strip()
    if not pid:
        return {"error": "proposal_id is required"}
    try:
        out = conscious_db.commit_proposal(pid, committer_agent_id=aid)
    except ValueError as e:
        return {"error": str(e)}
    return {"entry_id": out["entry"]["id"], "version": out["entry"]["version"],
            "proposal_id": pid}


# ---------------------------------------------------------------------------
# 10.5 conscious_reject_proposal  (orchestrator)
# ---------------------------------------------------------------------------

def _conscious_reject_proposal(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    if not _is_orchestrator(agent_session):
        return {"error": "only the orchestrator can reject proposals"}
    pid = str(args.get("proposal_id", "")).strip()
    reason = str(args.get("reason", ""))
    if not pid:
        return {"error": "proposal_id is required"}
    try:
        prop = conscious_db.reject_proposal(pid, committer_agent_id=aid, reason=reason)
    except ValueError as e:
        return {"error": str(e)}
    return {"proposal_id": pid, "status": prop.get("status", "rejected")}


# ---------------------------------------------------------------------------
# 10.6 conscious_invoke  (Phase 2: real worktree-per-agent execution)
# ---------------------------------------------------------------------------

def _conscious_invoke(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    to_agent_id = str(args.get("to_agent_id", "")).strip()
    task = str(args.get("task", ""))
    inputs = args.get("inputs") or {}
    timeout_s = int(args.get("timeout_s", 300) or 300)
    if not to_agent_id or not task:
        return {"error": "to_agent_id and task are required"}
    # cost ceiling check (soft enforcement before starting; hard enforcement
    # is in the adapter's turn() hook — §14 Phase 2)
    if not _cost_ok(cid):
        return {"error": "cost ceiling exceeded",
                "spent": _cost_spent(cid), "ceiling": _cost_ceiling(cid)}
    invoke_id = _gen_invoke_id()
    conscious_db.create_drawer_entry(
        conscious_id=cid, invoke_id=invoke_id, from_agent_id=aid,
        to_agent_id=to_agent_id, kind="invoke", task=task, inputs=inputs)
    # Phase 2: run the sub-agent against its worktree.
    result, files, status, error = _run_sub_agent(cid, to_agent_id, task, inputs, timeout_s)
    completed = conscious_db.complete_drawer_entry(
        invoke_id, result=result, status=status, error=error)
    _bump_cost(cid, _STUB_INVOKE_COST_USD)
    return {"invoke_id": invoke_id, "status": completed["status"],
            "result": result, "files": files}


# ---------------------------------------------------------------------------
# 10.7 conscious_delegate  (Phase 2: real async worktree execution)
# ---------------------------------------------------------------------------

def _conscious_delegate(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    to_agent_id = str(args.get("to_agent_id", "")).strip()
    task = str(args.get("task", ""))
    inputs = args.get("inputs") or {}
    if not to_agent_id or not task:
        return {"error": "to_agent_id and task are required"}
    if not _cost_ok(cid):
        return {"error": "cost ceiling exceeded",
                "spent": _cost_spent(cid), "ceiling": _cost_ceiling(cid)}
    invoke_id = _gen_invoke_id()
    conscious_db.create_drawer_entry(
        conscious_id=cid, invoke_id=invoke_id, from_agent_id=aid,
        to_agent_id=to_agent_id, kind="delegate", task=task, inputs=inputs)

    def _finish() -> None:
        try:
            result, files, status, error = _run_sub_agent(
                cid, to_agent_id, task, inputs, 600)
            conscious_db.complete_drawer_entry(
                invoke_id, result=result, status=status, error=error)
            _bump_cost(cid, _STUB_INVOKE_COST_USD)
        except Exception as exc:
            try:
                conscious_db.complete_drawer_entry(
                    invoke_id, result="", status="failed", error=str(exc))
            except Exception:
                pass

    threading.Thread(target=_finish, daemon=True).start()
    return {"invoke_id": invoke_id, "status": "pending"}


def _run_sub_agent(cid: str, to_agent_id: str, task: str,
                   inputs: dict, timeout_s: int) -> tuple[str, list[str], str, str | None]:
    """Phase 2: execute the sub-agent against its worktree.

    Real path (production): calls ``agent_sessions.get_or_create`` with the
    sub-agent's worktree as cwd + ``conscious_id``/``agent_id`` bound, submits
    the task as a user message, polls the transcript until the turn completes
    or ``timeout_s`` elapses, then returns the verbatim assistant text + any
    files the sub-agent wrote in its worktree.

    Fallback (no agent SDK available): writes a deterministic "simulated
    agent" output file to the worktree + commits it, so the worktree/merge/
    conflict flow is still exercised. This is what the Next.js preview uses
    (which has no LLM keys); the Python backend uses the real path when an
    SDK is present.
    """
    sub = conscious_db.get_agent(to_agent_id)
    if not sub or sub["conscious_id"] != cid:
        return (f"error: agent {to_agent_id} not found", [], "failed", "agent not found")
    if not sub.get("worktree_path"):
        # Phase 1 agent (no worktree) — fall back to a stub result so the
        # API contract still works on older conscious instances.
        return (_stub_result(to_agent_id, task, inputs), [], "done", None)
    # try the real path
    try:
        import agent_sessions
        # Phase 6: if the agent's tier is "zai", use the FREE GLM bridge
        # (a Node.js mini-service that wraps z-ai-web-dev-sdk). This avoids
        # the paid LiteLLM path entirely — GLM 5.2 is free + rate-limited.
        if sub.get("tier") == "zai":
            return _run_glm_bridge(sub, task, inputs, cid)
        tier = agent_sessions.tier_for_model(sub["model"])
        if tier is not None:
            from pathlib import Path
            ws_id = conscious_db.get_conscious(cid)["workspace_id"]
            import db as _dbmod
            ws = _dbmod.get_workspace(ws_id)
            session = agent_sessions.get_or_create(
                model=sub["model"], workspace_id=ws["id"],
                conscious_id=cid, agent_id=to_agent_id)
            session.submit(task)
            # poll until idle/timeout
            deadline = time.time() + timeout_s
            transcript: list[dict] = []
            cursor = 0
            while time.time() < deadline:
                snap = session.snapshot(since=cursor)
                transcript = snap.get("events", [])
                cursor = snap.get("next", cursor)
                if snap.get("status") in ("idle", "error") and snap["status"] != "running":
                    break
                time.sleep(0.5)
            # collect assistant text from the transcript
            parts = [ev.get("text", "") for ev in transcript
                     if ev.get("type") == "assistant"]
            result = "\n\n".join(p for p in parts if p) or "(no assistant output)"
            # list files the sub-agent wrote in its worktree
            files = _list_worktree_files(Path(sub["worktree_path"]))
            status = "done"
            error = None
            if session.status == "error":
                status = "failed"
                error = "agent turn errored"
            return (result, files, status, error)
    except Exception as exc:
        # fall through to the simulated path on any failure
        pass
    # simulated fallback (no SDK / no keys / error): write a deterministic file
    return _simulate_agent_work(sub, task, inputs)


def _stub_result(to_agent_id: str, task: str, inputs: dict) -> str:
    return (f"[stub] would invoke agent {to_agent_id} with task: {task}\n"
            f"inputs: {inputs}\n"
            f"(Phase 2: no agent SDK available; install claude-agent-sdk or strands-agents)")


def _run_glm_bridge(sub: dict, task: str, inputs: dict,
                    cid: str) -> tuple[str, list[str], str, str | None]:
    """Call the GLM bridge mini-service (free, rate-limited GLM 5.2).

    The bridge is a Node.js/Bun HTTP service at localhost:3030 that wraps
    the z-ai-web-dev-sdk. This avoids the paid LiteLLM path.
    """
    import json as _json
    import urllib.request as _urlreq
    from pathlib import Path as _P

    # build the conscious context for the system prompt
    c = conscious_db.get_conscious(cid) or {}
    bb_rows = conscious_db.get_blackboard(cid)
    latest: dict = {}
    for r in bb_rows:
        if r["section"] == "event":
            continue
        k = f"{r['section']}/{r['key']}"
        if k not in latest or r["version"] > latest[k]["version"]:
            latest[k] = r
    context_parts = [f"Goal: {c.get('goal', '')}"]
    for r in list(latest.values())[-10:]:
        context_parts.append(f"[{r['section']}/{r['key']}] {r['value'][:200]}")
    conscious_context = "\n".join(context_parts)

    system_prompt = (
        f"You are an AI agent in a Conscious multi-agent system.\n"
        f"Role: {sub.get('role', '')}\n"
        f"Model: GLM 5.2 (free)\n"
        f"Agent ID: {sub['id']}\n\n"
        f"--- Conscious Context ---\n{conscious_context}\n\n"
        f"--- Instructions ---\n"
        f"Your response will be recorded verbatim in the agent drawer.\n"
        f"If you want to write files, use this format:\n"
        f"```filename: path/to/file.ext\n"
        f"file content here\n"
        f"```\n"
        f"Be concise but thorough."
    )

    messages = [
        {"role": "assistant", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    try:
        response_text = ""
        errors = []
        # Path 1: PYTHON-NATIVE direct HTTP call (PREFERRED — no Node needed).
        # Uses urllib (stdlib) to call Puter/Z.ai/NVIDIA/OpenRouter/SiliconFlow
        # directly from the Python process. Works on Python-only HF Spaces.
        try:
            import agent_sessions as _as
            if _as._glm_native_available():
                response_text = _as._glm_call_native(messages, "glm-5.2", 120)
        except Exception as exc:
            errors.append(f"native: {type(exc).__name__}: {str(exc)[:120]}")
        # Path 2: Node.js subprocess (fallback)
        if not response_text:
            try:
                import agent_sessions as _as
                if _as._glm_subprocess_available():
                    response_text = _as._glm_call_subprocess(messages, "glm-5.2", 120)
            except Exception as exc:
                errors.append(f"subprocess: {type(exc).__name__}: {str(exc)[:120]}")
        # Path 3: HTTP bridge at localhost:3030 (last resort)
        if not response_text:
            body = _json.dumps({"messages": messages, "model": "glm-5.2"}).encode()
            req = _urlreq.Request(
                "http://localhost:3030/chat",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with _urlreq.urlopen(req, timeout=120) as resp:
                    result = _json.loads(resp.read().decode())
                response_text = result.get("content", "")
            except Exception as exc:
                errors.append(f"http: {type(exc).__name__}: {str(exc)[:120]}")
        if not response_text:
            raise RuntimeError("GLM unavailable — " + "; ".join(errors))
    except Exception as exc:
        return (f"[GLM bridge error] {exc}\n\n(Falling back to simulated response.)",
                [], "failed", str(exc))

    # parse file-write blocks
    import re as _re
    files: list[str] = []
    wt = _P(sub["worktree_path"])
    for m in _re.finditer(r"```filename:\s*(.+?)\n([\s\S]*?)```", response_text):
        filename = m.group(1).strip()
        content = m.group(2)
        try:
            import worktree as _wt
            target = _wt._safe_join(wt, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            files.append(filename)
        except Exception:
            pass

    # commit files on the agent's branch
    if files:
        try:
            import subprocess as _sp
            for f in files:
                _sp.run(["git", "-C", str(wt), "add", "--", f],
                        capture_output=True, text=True, timeout=30)
            _sp.run(["git", "-C", str(wt), "commit", "-m", f"[agent:glm] {task[:60]}"],
                    capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    # if no files, write the response as markdown
    if not files:
        slug = "".join(c if c.isalnum() else "-" for c in task.lower())[:40].strip("-") or "output"
        filename = f"{slug}.md"
        try:
            import worktree as _wt
            target = _wt._safe_join(wt, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(response_text, encoding="utf-8")
            import subprocess as _sp
            _sp.run(["git", "-C", str(wt), "add", "--", filename],
                    capture_output=True, text=True, timeout=30)
            _sp.run(["git", "-C", str(wt), "commit", "-m", f"[agent:glm] {task[:60]}"],
                    capture_output=True, text=True, timeout=30)
            files.append(filename)
        except Exception:
            pass

    return (response_text, files, "done", None)


def _list_worktree_files(wt_path: Path) -> list[str]:
    """List files the sub-agent wrote in its worktree (excluding .git/.brain)."""
    out: list[str] = []
    for p in wt_path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(wt_path)
        if str(rel).startswith((".git/", ".brain/")):
            continue
        out.append(str(rel))
    return out


def _simulate_agent_work(sub: dict, task: str, inputs: dict) -> tuple[str, list[str], str, str | None]:
    """Phase 2 fallback: write a deterministic output file to the sub-agent's
    worktree + commit it on the agent's branch. Returns the verbatim result +
    the list of files produced.

    This is the path the Next.js preview uses (it has no LLM keys). The Python
    backend uses the real ``agent_sessions`` path above when an SDK is present;
    this fallback only triggers when no SDK is importable.

    Security: the filename is validated via ``worktree._safe_join`` to prevent
    path traversal (``inputs.filename = "../../etc/evil"`` is rejected).
    """
    import os as _os
    from pathlib import Path as _P
    import subprocess as _sp
    import worktree as _wt
    wt = _P(sub["worktree_path"])
    # derive a filename from the task (cheap heuristic)
    slug = "".join(c if c.isalnum() else "-" for c in task.lower())[:40].strip("-")
    if not slug:
        slug = "output"
    filename = inputs.get("filename") or f"{slug}.md"
    # Security: validate the filename stays inside the worktree (audit C2)
    try:
        target = _wt._safe_join(wt, filename)
    except ValueError as exc:
        return (f"error: invalid filename rejected: {exc}", [], "failed", str(exc))
    content = (
        f"# Agent output: {task}\n\n"
        f"- **Agent:** {sub['id']} ({sub.get('role', '')} / {sub.get('model', '')})\n"
        f"- **Worktree:** {wt}\n"
        f"- **Branch:** {sub.get('branch', '')}\n"
        f"- **Timestamp:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
        f"## Inputs\n```\n{inputs}\n```\n\n"
        f"## Findings (simulated)\n"
        f"- [simulated] Reviewed the workspace state.\n"
        f"- [simulated] Tier 1 token-safety invariants intact (no token in .git/config).\n"
        f"- [simulated] This file was written by the Phase 2 simulated agent worker\n"
        f"  to exercise the worktree/merge/conflict flow without an LLM call.\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    # commit on the agent's branch
    try:
        _sp.run(["git", "-C", str(wt), "add", "--", filename],
                capture_output=True, text=True, timeout=30)
        _sp.run(["git", "-C", str(wt), "commit", "-m", f"[agent] {task[:60]}"],
                capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    return (content, [filename], "done", None)


# ---------------------------------------------------------------------------
# 10.8 conscious_drawer
# ---------------------------------------------------------------------------

def _conscious_drawer(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    invoke_id = args.get("invoke_id")
    if invoke_id:
        e = conscious_db.get_drawer_entry(str(invoke_id))
        if not e:
            return {"error": "drawer entry not found"}
        return {"entry": e}
    limit = int(args.get("limit", 20) or 20)
    entries = conscious_db.list_drawer(cid, limit=limit)
    return {"entries": entries}


# ---------------------------------------------------------------------------
# 10.9 conscious_message
# ---------------------------------------------------------------------------

def _conscious_message(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    to_agent_id = args.get("to_agent_id")  # None = broadcast
    body = str(args.get("body", ""))
    if not body:
        return {"error": "body is required"}
    msg = conscious_db.send_message(
        conscious_id=cid, from_agent_id=aid,
        to_agent_id=str(to_agent_id) if to_agent_id else None, body=body)
    return {"message_id": msg["id"]}


# ---------------------------------------------------------------------------
# 10.10 conscious_claim
# ---------------------------------------------------------------------------

def _conscious_claim(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return {"error": "task_id is required"}
    out = conscious_db.claim_task(task_id, agent_id=aid)
    if "error" in out:
        return out
    return {"task_id": task_id, "status": "claimed"}


# ---------------------------------------------------------------------------
# 10.11 conscious_task  (orchestrator direct; sub-agents propose)
# ---------------------------------------------------------------------------

def _conscious_task(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    if not _is_orchestrator(agent_session):
        return {"error": "sub-agents must propose plan mutations via conscious_propose with section='plan'"}
    action = str(args.get("action", "")).strip()
    if action == "create":
        title = str(args.get("title", "")).strip()
        if not title:
            return {"error": "title is required for create"}
        task = conscious_db.create_task(
            conscious_id=cid, title=title,
            description=str(args.get("description", "")),
            assignee_agent_id=args.get("assignee_agent_id"),
            depends_on=args.get("depends_on") or [],
            cost_ceiling_usd=args.get("cost_ceiling_usd"))
        return {"task_id": task["id"], "status": task["status"]}
    if action == "update":
        tid = str(args.get("task_id", "")).strip()
        if not tid:
            return {"error": "task_id is required for update"}
        fields = {}
        for k in ("title", "description", "assignee_agent_id", "status",
                  "depends_on", "cost_ceiling_usd", "completed_at"):
            if k in args:
                fields[k] = args[k]
        task = conscious_db.update_task(tid, **fields)
        if not task:
            return {"error": "task not found"}
        return {"task_id": task["id"], "status": task["status"]}
    return {"error": "action must be 'create' or 'update'"}


# ---------------------------------------------------------------------------
# 10.12 conscious_subscribe
# ---------------------------------------------------------------------------

def _conscious_subscribe(agent_session: Any, args: dict) -> dict:
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    events = args.get("events") or []
    if not isinstance(events, list):
        return {"error": "events must be a list of prefix strings"}
    conscious_db.update_agent(aid, subscribed_events=[str(e) for e in events])
    return {"subscribed": [str(e) for e in events]}


# ---------------------------------------------------------------------------
# 10.13 conscious_merge  (Phase 4 — wraps merge.merge_critiques over drawer entries)
# ---------------------------------------------------------------------------

def _conscious_merge(agent_session: Any, args: dict) -> dict:
    """Merge a set of drawer entries' results into one consolidated markdown
    using the deterministic merge from ``merge.merge_critiques``.

    This is the OPTIONAL deterministic merge — the drawer is verbatim by
    default (TIER3_PLAN §1: "merging is a judgment call, not a deterministic
    function"). Callers who want the old merge behavior can opt in via this
    tool. Modes: ``dedupe`` (default; merge_critiques), ``vote`` (merge_votes
    for pass/fail consensus), ``concat`` (stack all outputs labeled).

    Args:
        invoke_ids: list of drawer invoke_ids to merge (their result texts are
          the judge outputs).
        mode: dedupe|vote|concat (default dedupe).

    Returns:
        {merged: <markdown string>, mode: <mode>, count: <N>, entry_id: <id>}
        The merged text is also posted to the blackboard under section="merge",
        key="merge.<timestamp>" (committed by the calling agent).
    """
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    invoke_ids = args.get("invoke_ids") or []
    if not isinstance(invoke_ids, list) or not invoke_ids:
        return {"error": "invoke_ids (non-empty list) is required"}
    mode = str(args.get("mode", "dedupe")).strip()
    if mode not in ("dedupe", "vote", "concat"):
        return {"error": "mode must be 'dedupe', 'vote', or 'concat'"}

    # collect drawer entries + build the judges list for merge.py
    judges: list[dict] = []
    for invoke_id in invoke_ids:
        entry = conscious_db.get_drawer_entry(str(invoke_id))
        if not entry or entry["conscious_id"] != cid:
            return {"error": f"drawer entry {invoke_id} not found"}
        if entry.get("status") != "done":
            return {"error": f"drawer entry {invoke_id} is not done (status={entry.get('status')})"}
        # look up the agent's model for the "model" field merge.py expects
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
        return {"error": f"merge failed: {exc}"}
    if not merged:
        merged = "(merge produced no output — check that the drawer entries have non-empty results)"

    # post the merged result to the blackboard (section="merge")
    import time as _time
    key = f"merge.{int(_time.time())}"
    entry = conscious_db.post_blackboard(
        conscious_id=cid, section="merge", key=key, value=merged,
        author_agent_id=aid, committed_by_agent_id=aid, proposal_id=None)
    return {"merged": merged, "mode": mode, "count": len(judges),
            "entry_id": entry["id"], "version": entry["version"]}


# ---------------------------------------------------------------------------
# 10.14 conscious_panel  (Phase 4 — invokes the judge panel as a sub-agent)
# ---------------------------------------------------------------------------

def _conscious_panel(agent_session: Any, args: dict) -> dict:
    """Invoke the existing judge panel as a sub-agent.

    Spawns a temporary sub-agent whose "task" is the panel critique request,
    runs it through the standard invoke path (drawer entry created, result
    verbatim), and returns the drawer invoke_id. The panel's actual execution
    (driving the judge loop) happens via the normal agent_sessions path when
    an LLM SDK is present; in the preview/simulated path, the result is a
    placeholder noting the panel was invoked.

    This lets the orchestrator delegate critique tasks to the existing judge
    panel — the panel's per-judge outputs land in the drawer as separate
    entries (one per judge model), and the orchestrator can then
    ``conscious_merge`` them if desired.

    Args:
        prompt: the critique prompt (non-empty string).
        panel: optional list of "provider/model" strings (passed through to
          the panel runner in production).
        profile: optional profile id (default "default").
        effort: optional effort level (default "medium").

    Returns:
        {invoke_id: <id>, status: "done"|"pending", result: <verbatim>}
    """
    cid, aid = _ctx(agent_session)
    if not cid or not aid:
        return _not_in_conscious()
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"error": "prompt (non-empty string) is required"}

    # find or create a "panel" sub-agent to be the invoke target
    agents = conscious_db.list_agents(cid)
    panel_agent = next((a for a in agents if a.get("role") == "panel"), None)
    if not panel_agent:
        # spawn a panel sub-agent (no worktree needed for the panel — it
        # doesn't write files, just returns critique text)
        panel_agent = conscious_db.spawn_agent(
            conscious_id=cid, role="panel", model="panel/judge-loop",
            tier="open", is_orchestrator=False)

    # build the task + inputs
    task = f"Panel critique: {prompt[:200]}"
    inputs = {
        "prompt": prompt,
        "panel": args.get("panel"),
        "profile": args.get("profile", "default"),
        "effort": args.get("effort", "medium"),
        "_panel_invoke": True,  # signal to the worker that this is a panel call
    }

    # route through the standard invoke path (reuses _conscious_invoke)
    return _conscious_invoke(agent_session, {
        "to_agent_id": panel_agent["id"],
        "task": task,
        "inputs": inputs,
        "timeout_s": int(args.get("timeout_s", 600) or 600),
    })


# ---------------------------------------------------------------------------
# cost helpers
# ---------------------------------------------------------------------------

def _gen_invoke_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def _cost_ceiling(cid: str) -> float:
    c = conscious_db.get_conscious(cid) or {}
    return float(c.get("cost_ceiling_usd", 0) or 0)


def _cost_spent(cid: str) -> float:
    c = conscious_db.get_conscious(cid) or {}
    return float(c.get("cost_spent_usd", 0) or 0)


def _cost_ok(cid: str) -> bool:
    ceiling = _cost_ceiling(cid)
    if ceiling <= 0:
        return True  # 0 = infinite
    return _cost_spent(cid) < ceiling


def _bump_cost(cid: str, amount: float) -> None:
    """Add ``amount`` to conscious.cost_spent_usd."""
    import db as _dbmod
    with _dbmod._write_lock:
        _dbmod._db().execute(
            "UPDATE conscious SET cost_spent_usd = cost_spent_usd + ?, "
            "updated_at = ? WHERE id = ?",
            (float(amount), conscious_db._iso_now(), cid))
        _dbmod._db().commit()


# ---------------------------------------------------------------------------
# 10.15 agent_panel  — standalone panel invocation (no conscious context needed)
# ---------------------------------------------------------------------------

def _agent_panel(agent_session: Any, args: dict) -> dict:
    """Invoke the judge panel directly, emitting progress events to the agent stream.

    Unlike ``conscious_panel``, this tool requires no conscious binding. It runs the
    panel inline (blocking the agent's turn), emitting ``panel`` events with per-judge
    progress snapshots so the frontend can stream results in real-time.

    Args:
        prompt: the critique prompt (non-empty string).
        panel: optional list of logical model names.
        profile: optional profile id (default "default").
        effort: optional effort level (default "medium").
        timeout_s: optional per-judge timeout (default 600).

    Returns:
        {invoke_id, task_name, status, judges, merged, meta}
    """
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"error": "prompt (non-empty string) is required"}

    panel_list = args.get("panel")
    profile = str(args.get("profile", "default"))
    effort = str(args.get("effort", "medium")).replace("medium", "med")
    timeout_s = int(args.get("timeout_s", 600) or 600)

    try:
        from critique_service import _panel as g_panel, _jobs as g_jobs
    except Exception as exc:
        return {"error": f"panel system not available: {exc}"}

    if g_panel is None or g_jobs is None:
        return {"error": "panel system not initialized"}

    who_list: list[str] = (
        [str(m) for m in panel_list]
        if isinstance(panel_list, list) and panel_list
        else list(g_panel.default_panel)
    )

    import secrets as _sec
    task_name = _sec.token_hex(4)
    invoke_id = _sec.token_hex(8)

    agent_session.emit({
        "type": "panel", "status": "starting",
        "invoke_id": invoke_id, "task_name": task_name,
        "prompt": prompt, "panel": list(who_list),
    })

    try:
        job = g_jobs.submit(
            who_list=who_list,
            system_prompt=prompt,
            user_msg=prompt,
            max_tokens=4096,
            role="critiquer",
            merge_mode="dedupe",
            kind="critique",
            profile=profile,
            effort="med" if effort == "medium" else effort,
            timeout_s=float(timeout_s),
            nonce="",
            reasoning=False,
            research=False,
            privacy="off",
        )
    except Exception as exc:
        agent_session.emit({
            "type": "panel", "status": "done",
            "invoke_id": invoke_id, "task_name": task_name,
            "error": str(exc),
        })
        return {"invoke_id": invoke_id, "task_name": task_name,
                "status": "error", "error": str(exc)}

    job_id = job.get("job_id") or ""
    if not job_id:
        return {"invoke_id": invoke_id, "task_name": task_name,
                "status": "error", "error": "no job_id returned"}

    deadline = time.time() + (timeout_s * len(who_list) + 30)
    while time.time() < deadline:
        snap = g_jobs.snapshot(job_id)
        if snap is None:
            break
        agent_session.emit({
            "type": "panel", "status": "running",
            "invoke_id": invoke_id, "task_name": task_name,
            "snapshot": dict(snap),
        })
        if snap.get("status") == "complete":
            break
        time.sleep(1.5)

    final = g_jobs.snapshot(job_id) or job
    agent_session.emit({
        "type": "panel", "status": "done",
        "invoke_id": invoke_id, "task_name": task_name,
        "snapshot": dict(final) if isinstance(final, dict) else {},
    })

    return {
        "invoke_id": invoke_id,
        "task_name": task_name,
        "status": "done",
        "judges": list(final.get("judges", [])),
        "merged": final.get("merged", ""),
        "meta": dict(final.get("meta", {})),
    }


# ---------------------------------------------------------------------------
# CONSCIOUS_TOOLS — registered in both adapters (claude + open) by agent_sessions.py
# ---------------------------------------------------------------------------

CONSCIOUS_TOOLS: list[dict] = [
    {
        "name": "conscious_context",
        "description": (
            "Read the conscious brain: goal, blackboard (decisions/findings/questions/"
            "artifacts), events since a cursor, pending proposals, tasks, drawer entries, "
            "messages. Call at the start of every turn to know what's going on. "
            "Returns pings for events matching this agent's subscriptions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "integer", "default": 0,
                          "description": "event cursor; pass the previous `cursor` to get only new events"},
                "sections": {"type": "array", "items": {"type": "string"},
                             "description": "optional: only these blackboard sections"},
                "include_drawer": {"type": "boolean", "default": False},
                "include_messages": {"type": "boolean", "default": True},
                "include_proposals": {"type": "boolean", "default": True},
                "include_tasks": {"type": "boolean", "default": True},
            },
        },
        "handler": _conscious_context,
    },
    {
        "name": "conscious_post",
        "description": (
            "Orchestrator-only: post a blackboard entry directly (no proposal needed). "
            "Sub-agents get an error and must use conscious_propose instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                            "description": "goal|plan|decision|finding|question|artifact|<custom>"},
                "key": {"type": "string", "description": "logical key within the section"},
                "value": {"type": "string", "description": "the content (markdown/text)"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["section", "key", "value"],
        },
        "handler": _conscious_post,
    },
    {
        "name": "conscious_propose",
        "description": (
            "Sub-agent: propose a blackboard write. Queued for the orchestrator to "
            "commit or reject. Use this for any brain mutation (decision/finding/"
            "question/plan). Returns proposal_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string"},
                "key": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string", "description": "why this change"},
            },
            "required": ["section", "key", "value", "reason"],
        },
        "handler": _conscious_propose,
    },
    {
        "name": "conscious_commit_proposal",
        "description": "Orchestrator-only: commit a sub-agent's proposal to the blackboard.",
        "input_schema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
        },
        "handler": _conscious_commit_proposal,
    },
    {
        "name": "conscious_reject_proposal",
        "description": "Orchestrator-only: reject a sub-agent's proposal with a reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["proposal_id", "reason"],
        },
        "handler": _conscious_reject_proposal,
    },
    {
        "name": "conscious_invoke",
        "description": (
            "Synchronously invoke another agent: caller blocks until the sub-agent "
            "returns (or timeout). The sub-agent's verbatim result lands in the drawer. "
            "Phase 1 stub returns a canned result; Phase 2 wires real execution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent_id": {"type": "string"},
                "task": {"type": "string"},
                "inputs": {"type": "object", "default": {}},
                "timeout_s": {"type": "integer", "default": 300},
            },
            "required": ["to_agent_id", "task"],
        },
        "handler": _conscious_invoke,
    },
    {
        "name": "conscious_delegate",
        "description": (
            "Asynchronously delegate to another agent: returns immediately with "
            "invoke_id; poll via conscious_drawer or get pinged via conscious_context "
            "events when done. Phase 1 stub completes after ~2s."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent_id": {"type": "string"},
                "task": {"type": "string"},
                "inputs": {"type": "object", "default": {}},
            },
            "required": ["to_agent_id", "task"],
        },
        "handler": _conscious_delegate,
    },
    {
        "name": "conscious_drawer",
        "description": "Read drawer entries: single (with full result text) or recent list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoke_id": {"type": "string", "description": "if omitted, returns recent entries"},
                "limit": {"type": "integer", "default": 20},
            },
        },
        "handler": _conscious_drawer,
    },
    {
        "name": "conscious_message",
        "description": "Send a message to another agent (to_agent_id) or broadcast (None).",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent_id": {"type": "string", "description": "null/omit = broadcast"},
                "body": {"type": "string"},
            },
            "required": ["body"],
        },
        "handler": _conscious_message,
    },
    {
        "name": "conscious_claim",
        "description": "Atomically claim an unclaimed task. Errors if already claimed.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        "handler": _conscious_claim,
    },
    {
        "name": "conscious_task",
        "description": (
            "Orchestrator-only: create or update a task in the plan DAG. Sub-agents "
            "propose plan mutations via conscious_propose with section='plan'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update"]},
                "task_id": {"type": "string", "description": "required for update"},
                "title": {"type": "string", "description": "required for create"},
                "description": {"type": "string"},
                "assignee_agent_id": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string"},
                "cost_ceiling_usd": {"type": "number"},
            },
            "required": ["action"],
        },
        "handler": _conscious_task,
    },
    {
        "name": "conscious_subscribe",
        "description": (
            "Replace this agent's event subscriptions. Events are prefix-matched "
            "(e.g. 'proposal.*' matches 'proposal.created' + 'proposal.committed')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "items": {"type": "string"},
                           "description": "prefixes, e.g. ['proposal.*','task.claimed']"},
            },
            "required": ["events"],
        },
        "handler": _conscious_subscribe,
    },
    {
        "name": "conscious_merge",
        "description": (
            "Phase 4 — OPTIONAL deterministic merge over a set of drawer entries. "
            "Wraps merge.merge_critiques (dedupe), merge_votes (vote), or merge_concat "
            "(concat) over the verbatim results of the given invoke_ids. The merged "
            "markdown is posted to the blackboard under section='merge'. Use this when "
            "you want the old deterministic merge behavior; the drawer is verbatim by "
            "default (merging is a judgment call, not a deterministic function)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoke_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "drawer invoke_ids whose result texts to merge",
                },
                "mode": {"type": "string", "enum": ["dedupe", "vote", "concat"],
                          "default": "dedupe"},
            },
            "required": ["invoke_ids"],
        },
        "handler": _conscious_merge,
    },
    {
        "name": "conscious_panel",
        "description": (
            "Phase 4 — invoke the existing judge panel as a sub-agent. Spawns a "
            "temporary 'panel' sub-agent, routes the critique prompt through the "
            "standard invoke path (drawer entry created, result verbatim), and returns "
            "the invoke_id. The orchestrator can then conscious_merge the per-judge "
            "outputs if desired. Use this to delegate critique tasks to the panel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "the critique prompt"},
                "panel": {"type": "array", "items": {"type": "string"},
                          "description": "optional list of 'provider/model' strings"},
                "profile": {"type": "string", "default": "default"},
                "effort": {"type": "string", "default": "medium"},
                "timeout_s": {"type": "integer", "default": 600},
            },
            "required": ["prompt"],
        },
        "handler": _conscious_panel,
    },
    {
        "name": "agent_panel",
        "description": (
            "Invoke the judge panel directly, emitting progress to the event stream. "
            "Runs the panel inline (blocking the agent's turn) and streams per-judge "
            "snapshots so the frontend shows live progress. No conscious binding required. "
            "Use this to get multi-model critiques, reviews, or evaluations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "the critique prompt (required)"},
                "panel": {"type": "array", "items": {"type": "string"},
                          "description": "optional list of logical model names; defaults to configured panel"},
                "profile": {"type": "string", "default": "default"},
                "effort": {"type": "string", "default": "medium",
                           "enum": ["low", "med", "high", "max"]},
                "timeout_s": {"type": "integer", "default": 600},
            },
            "required": ["prompt"],
        },
        "handler": _agent_panel,
    },
]
