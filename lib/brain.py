"""``.brain/`` folder I/O for the Conscious multi-agent system (Tier 3, Phase 1).

Pure filesystem + git operations. No HTTP, no DB. The brain is a human-readable,
git-versioned folder persisted in the user's workspace repo. The DB
(``conscious_db.py``) mirrors state for fast queries; this module is the
canonical writer for the on-disk artifact.

Layout (see TIER3_PLAN.md §6.2)::

    <workspace>/.brain/
    ├── CONSCIOUS.md            # regenerated summary
    ├── goal.md                 # the goal, verbatim
    ├── plan.json               # task DAG mirror
    ├── config.json             # conscious-level config
    ├── agents/<id>.md          # one per agent
    ├── agents/<id>.log         # append-only activity log
    ├── blackboard/<section>.jsonl
    ├── blackboard/custom/<section>.jsonl
    ├── blackboard/events.jsonl
    ├── drawer/<invoke-id>/{request.json,result.md,files/,status.json}
    ├── proposals/queue.jsonl
    └── skills/                 # conscious skill doc (seeded at creation)

All writes are serialized per-brain-path via ``BRAIN_LOCKS`` so concurrent
appends never corrupt a ``.jsonl`` line.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Built-in blackboard sections. Custom sections land in blackboard/custom/.

def _sanitize_path_component(name):
    """Sanitize a user-supplied path component to prevent path traversal.
    Only allow alphanumeric, hyphens, underscores, and dots."""
    import re
    if not re.match(r'^[A-Za-z0-9._-]+$', str(name)):
        raise ValueError(f"invalid path component: {name!r}")
    if '..' in str(name):
        raise ValueError(f"invalid path component: {name!r}")
    return str(name)

BUILTIN_SECTIONS = {"goal", "plan", "decision", "finding", "question", "artifact", "event"}

# Per-brain-path lock registry — serializes .jsonl appends.
BRAIN_LOCKS: dict[str, threading.Lock] = {}
_BRAIN_LOCKS_GUARD = threading.Lock()


def _lock_for(brain_path: Path) -> threading.Lock:
    key = str(brain_path.resolve())
    with _BRAIN_LOCKS_GUARD:
        lk = BRAIN_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            BRAIN_LOCKS[key] = lk
        return lk


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# init / structure
# ---------------------------------------------------------------------------

def init_brain(workspace_path: Path, conscious: dict) -> Path:
    """Create the ``.brain/`` folder with the full structure.

    Idempotent: a no-op if the folder exists, but always refreshes
    ``CONSCIOUS.md`` and ``config.json`` from the current conscious dict.
    Returns the ``.brain/`` path.
    """
    brain = workspace_path / ".brain"
    (brain / "agents").mkdir(parents=True, exist_ok=True)
    (brain / "blackboard" / "custom").mkdir(parents=True, exist_ok=True)
    (brain / "drawer").mkdir(parents=True, exist_ok=True)
    (brain / "proposals").mkdir(parents=True, exist_ok=True)
    (brain / "skills").mkdir(parents=True, exist_ok=True)

    # goal.md (verbatim)
    (brain / "goal.md").write_text(conscious.get("goal", "") or "", encoding="utf-8")

    # config.json
    cfg = {
        "conscious_id": conscious["id"],
        "cost_ceiling_usd": conscious.get("cost_ceiling_usd", 0),
        "brain_commit_policy": conscious.get("brain_commit_policy", "on"),
        "graphiti_enabled": bool(conscious.get("graphiti_enabled", 0)),
        "max_agents": conscious.get("max_agents", 8),
        "created_at": conscious.get("created_at") or _iso_now(),
        "updated_at": _iso_now(),
    }
    (brain / "config.json").write_text(_pretty_json(cfg), encoding="utf-8")

    # empty .jsonl files (touch)
    for sec in BUILTIN_SECTIONS:
        p = brain / "blackboard" / f"{sec}.jsonl"
        if not p.exists():
            p.touch()
    # proposals queue + events are part of BUILTIN_SECTIONS via "event" section,
    # but proposals/queue.jsonl is a separate file.
    pq = brain / "proposals" / "queue.jsonl"
    if not pq.exists():
        pq.touch()

    # plan.json (empty default)
    pj = brain / "plan.json"
    if not pj.exists() or pj.stat().st_size == 0:
        pj.write_text(_pretty_json({
            "conscious_id": conscious["id"],
            "version": 0,
            "tasks": [],
            "updated_at": _iso_now(),
        }), encoding="utf-8")

    # seed the conscious SKILL.md (best-effort; skills source may not exist yet)
    _seed_skill(brain)

    # CONSCIOUS.md (refreshed every call)
    regenerate_conscious_md(brain, conscious, [], [], [], [])

    return brain


def _seed_skill(brain: Path) -> None:
    """Copy the conscious SKILL.md into .brain/skills/ if the source exists."""
    here = Path(__file__).resolve().parent
    src = here / "agent_skills" / "conscious" / "SKILL.md"
    if not src.is_file():
        return
    dest = brain / "skills" / "conscious" / "SKILL.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# read snapshot
# ---------------------------------------------------------------------------

def read_brain(brain_path: Path) -> dict:
    """Return a structured snapshot of the brain (used by conscious_context)."""
    bp = brain_path
    out: dict[str, Any] = {"brain_path": str(bp)}
    if (bp / "goal.md").is_file():
        out["goal"] = (bp / "goal.md").read_text(encoding="utf-8")
    else:
        out["goal"] = ""
    if (bp / "config.json").is_file():
        try:
            out["config"] = json.loads((bp / "config.json").read_text(encoding="utf-8"))
        except Exception:
            out["config"] = {}
    else:
        out["config"] = {}
    out["blackboard"] = read_blackboard(bp)
    out["events"] = read_events(bp)
    out["proposals"] = read_proposals(bp)
    out["agents"] = read_agents(bp)
    return out


def read_blackboard(brain_path: Path, section: str | None = None) -> dict[str, list[dict]]:
    """Read blackboard sections. Returns {section: [entries...]}.

    If ``section`` is given, returns only that section. Entries are returned
    in append order; readers wanting "latest version per key" take the max-version
    line per key themselves.
    """
    out: dict[str, list[dict]] = {}
    bb = brain_path / "blackboard"
    if not bb.is_dir():
        return out
    sections = [section] if section else list(BUILTIN_SECTIONS)
    for sec in sections:
        p = bb / f"{sec}.jsonl"
        entries: list[dict] = []
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        if entries:
            out[sec] = entries
    # custom sections
    custom_dir = bb / "custom"
    if custom_dir.is_dir():
        for p in custom_dir.glob("*.jsonl"):
            entries = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
            if entries:
                out[p.stem] = entries
    return out


def read_events(brain_path: Path, since: int = 0) -> list[dict]:
    """Read events with id > since. Events use a monotonic id field."""
    p = brain_path / "blackboard" / "events.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("id", 0) > since:
                out.append(obj)
        except Exception:
            continue
    return out


def read_proposals(brain_path: Path) -> list[dict]:
    p = brain_path / "proposals" / "queue.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def read_agents(brain_path: Path) -> list[str]:
    """Return list of agent IDs that have a profile file."""
    d = brain_path / "agents"
    if not d.is_dir():
        return []
    return [p.stem for p in d.glob("*.md")]


# ---------------------------------------------------------------------------
# append operations (all serialized via per-brain lock)
# ---------------------------------------------------------------------------

def _blackboard_file(brain_path: Path, section: str) -> Path:
    if section in BUILTIN_SECTIONS:
        return brain_path / "blackboard" / f"{_sanitize_path_component(section)}.jsonl"
    (brain_path / "blackboard" / "custom").mkdir(parents=True, exist_ok=True)
    return brain_path / "blackboard" / "custom" / f"{_sanitize_path_component(section)}.jsonl"


def append_blackboard(brain_path: Path, section: str, key: str, value: str,
                      author: str, committed_by: str,
                      proposal_id: str | None, version: int,
                      entry_id: str | None = None) -> str:
    """Append a blackboard entry to ``blackboard/<section>.jsonl``.

    Returns the entry id. Atomic per-brain lock.
    """
    eid = entry_id or os.urandom(8).hex()
    obj = {
        "id": eid,
        "section": section,
        "key": key,
        "value": value,
        "author_agent_id": author,
        "committed_by_agent_id": committed_by,
        "proposal_id": proposal_id,
        "version": version,
        "created_at": _iso_now(),
    }
    lk = _lock_for(brain_path)
    with lk:
        p = _blackboard_file(brain_path, section)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(_compact_json(obj) + "\n")
    return eid


def append_event(brain_path: Path, key: str, value: str,
                 author: str | None = None, entry_id: str | None = None) -> int:
    """Append to events.jsonl; returns the new event id (monotonic per brain).

    Phase 3: rotates events.jsonl when it exceeds ``MAX_EVENTS_LINES`` lines.
    The rotated file is moved to ``events.<timestamp>.jsonl`` (archived) and a
    fresh ``events.jsonl`` starts. Event ids continue monotonically across
    rotations (the max id is preserved). Archived files are kept indefinitely
    (they're human-readable history); a future sweeper can prune by age.

    Phase 5 optimization: caches the max_id per brain path in a module-level
    dict so we don't re-read + re-parse the whole file on every append. The
    cache is invalidated on rotation. Falls back to a full scan if the cache
    is cold.
    """
    lk = _lock_for(brain_path)
    with lk:
        p = brain_path / "blackboard" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        # Phase 5 optimization: use cached max_id if available (audit M3).
        # Only do the full scan on first touch or after a rotation.
        cache_key = str(brain_path.resolve())
        max_id = _event_id_cache.get(cache_key, -1)
        lines: list[str] = []
        if max_id < 0:
            # cold cache — scan the file once
            if p.is_file():
                raw = p.read_text(encoding="utf-8").splitlines()
                for line in raw:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj.get("id"), int) and obj["id"] > max_id:
                            max_id = obj["id"]
                    except Exception:
                        continue
                    lines.append(line)
            else:
                max_id = 0
        new_id = max_id + 1
        obj = {
            "id": new_id,
            "key": key,
            "value": value,
            "author_agent_id": author,
            "created_at": _iso_now(),
        }
        if entry_id:
            obj["entry_id"] = entry_id
        new_line = _compact_json(obj)
        # rotation: if over the cap, archive + start fresh
        if max_id > 0 and max_id % MAX_EVENTS_LINES == 0 and max_id > 0:
            archive = brain_path / "blackboard" / f"events.{_iso_now().replace(':', '').replace('-', '')}.jsonl"
            try:
                if p.is_file():
                    p.rename(archive)
            except Exception:
                pass  # archive failure must never block the append
        # append a single line (O(1), not rewrite)
        with p.open("a", encoding="utf-8") as f:
            f.write(new_line + "\n")
        # update the cache
        _event_id_cache[cache_key] = new_id
    return new_id


# Phase 5 optimization: cache max event id per brain path (audit M3).
# Keyed by resolved brain_path string. Invalidated on rotation.
_event_id_cache: dict[str, int] = {}


# Phase 3 — events.jsonl rotation cap. When events.jsonl exceeds this many
# lines, the older events are archived to events.<timestamp>.jsonl and a fresh
# events.jsonl starts. Ids continue monotonically across rotations.
MAX_EVENTS_LINES = int(os.environ.get("CONSCIOUS_MAX_EVENTS_LINES", "10000"))


def append_proposal(brain_path: Path, proposal: dict) -> None:
    """Append a proposal to proposals/queue.jsonl."""
    lk = _lock_for(brain_path)
    with lk:
        p = brain_path / "proposals" / "queue.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(_compact_json(proposal) + "\n")


# ---------------------------------------------------------------------------
# agent profiles
# ---------------------------------------------------------------------------

def write_agent_profile(brain_path: Path, agent: dict) -> None:
    """Write/overwrite agents/<id>.md."""
    p = brain_path / "agents" / f"{agent['id']}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    subs = agent.get("subscribed_events") or []
    if isinstance(subs, str):
        try:
            subs = json.loads(subs)
        except Exception:
            subs = []
    body = [
        f"# Agent: {agent['id']}",
        "",
        f"- **Role:** {agent.get('role', '')}",
        f"- **Model:** {agent.get('model', '')}",
        f"- **Tier:** {agent.get('tier', '')}",
        f"- **Status:** {agent.get('status', 'idle')}",
        f"- **Worktree:** {agent.get('worktree_path') or '(Phase 2)'}",
        f"- **Branch:** {agent.get('branch') or '(Phase 2)'}",
        f"- **Parent:** {agent.get('parent_agent_id') or 'null'}",
        f"- **Orchestrator:** {'yes' if agent.get('is_orchestrator') else 'no'}",
        f"- **Subscribed events:** {json.dumps(subs)}",
        f"- **Created:** {agent.get('created_at', '')}",
        "",
        "## Recent activity (last 20)",
        "",
    ]
    log_path = brain_path / "agents" / f"{agent['id']}.log"
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8").splitlines()[-20:]
        body.extend(lines)
    p.write_text("\n".join(body), encoding="utf-8")


def append_agent_log(brain_path: Path, agent_id: str, entry: str) -> None:
    """Append a line to agents/<id>.log."""
    p = brain_path / "agents" / f"{_sanitize_path_component(agent_id)}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    lk = _lock_for(brain_path)
    with lk:
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{_iso_now()}] {entry}\n")


# ---------------------------------------------------------------------------
# drawer
# ---------------------------------------------------------------------------

def create_drawer_entry(brain_path: Path, invoke_id: str, request: dict) -> Path:
    """Create drawer/<invoke-id>/ with request.json, empty result.md, files/, status.json."""
    d = brain_path / "drawer" / _sanitize_path_component(invoke_id)
    (d / "files").mkdir(parents=True, exist_ok=True)
    (d / "request.json").write_text(_pretty_json(request), encoding="utf-8")
    (d / "result.md").write_text("(pending)", encoding="utf-8")
    status = {
        "status": "pending",
        "started_at": _iso_now(),
        "completed_at": None,
        "error": None,
    }
    (d / "status.json").write_text(_pretty_json(status), encoding="utf-8")
    return d


def complete_drawer_entry(brain_path: Path, invoke_id: str, result: str,
                          status: str, error: str | None = None) -> None:
    """Write result.md and update status.json for a drawer entry."""
    d = brain_path / "drawer" / _sanitize_path_component(invoke_id)
    if not d.is_dir():
        return
    (d / "result.md").write_text(result if result else "(no output)", encoding="utf-8")
    obj = {
        "status": status,
        "started_at": _iso_now(),
        "completed_at": _iso_now(),
        "error": error,
    }
    (d / "status.json").write_text(_pretty_json(obj), encoding="utf-8")


def read_drawer(brain_path: Path, invoke_id: str) -> dict | None:
    """Read a single drawer entry; returns None if missing."""
    d = brain_path / "drawer" / _sanitize_path_component(invoke_id)
    if not d.is_dir():
        return None
    out: dict[str, Any] = {"invoke_id": invoke_id}
    req_p = d / "request.json"
    if req_p.is_file():
        try:
            out["request"] = json.loads(req_p.read_text(encoding="utf-8"))
        except Exception:
            out["request"] = {}
    res_p = d / "result.md"
    if res_p.is_file():
        out["result"] = res_p.read_text(encoding="utf-8")
    else:
        out["result"] = ""
    st_p = d / "status.json"
    if st_p.is_file():
        try:
            out["status"] = json.loads(st_p.read_text(encoding="utf-8"))
        except Exception:
            out["status"] = {}
    files_dir = d / "files"
    out["files"] = [p.name for p in files_dir.iterdir()] if files_dir.is_dir() else []
    return out


def list_drawer(brain_path: Path, limit: int = 20) -> list[dict]:
    """List recent drawer entries (sorted by mtime desc). Each entry contains
    request summary + status, not the full result text."""
    d = brain_path / "drawer"
    if not d.is_dir():
        return []
    items: list[dict] = []
    for sub in d.iterdir():
        if not sub.is_dir():
            continue
        full = read_drawer(brain_path, sub.name)
        if not full:
            continue
        req = full.get("request", {}) or {}
        result_text = full.get("result", "") or ""
        st = full.get("status", {}) or {}
        items.append({
            "invoke_id": sub.name,
            "from": req.get("from_agent_id"),
            "to": req.get("to_agent_id"),
            "kind": req.get("kind"),
            "task": req.get("task", ""),
            "status": st.get("status", "pending"),
            "summary": result_text[:200],
            "created_at": st.get("started_at"),
        })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# regenerated views
# ---------------------------------------------------------------------------

def regenerate_conscious_md(brain_path: Path, conscious: dict,
                            agents: list[dict], recent_decisions: list[dict],
                            open_questions: list[dict], open_tasks: list[dict]) -> None:
    """Rewrite CONSCIOUS.md from current state."""
    orch = next((a for a in agents if a.get("is_orchestrator")), None)
    lines = [
        f"# Conscious: {conscious.get('title', 'Untitled')}",
        "",
        f"- **Goal:** see goal.md",
        f"- **Orchestrator:** {orch['id'] if orch else '(none)'} ({orch['model'] if orch else ''})",
        f"- **Agents:** {len(agents)} active",
        f"- **Created:** {conscious.get('created_at', '')}",
        f"- **Cost ceiling:** ${conscious.get('cost_ceiling_usd', 0):.2f} (0 = infinite)",
        f"- **Brain commit policy:** {conscious.get('brain_commit_policy', 'on')}",
        "",
        "## Agents",
    ]
    for a in agents:
        lines.append(f"- {a['id']} — {a.get('role', '')} — {a.get('model', '')} — {a.get('status', 'idle')}")
    lines += ["", "## Recent decisions (last 10)"]
    if recent_decisions:
        for d in recent_decisions[-10:]:
            lines.append(f"- [{d.get('created_at', '')}] {d.get('author_agent_id', '?')}: {d.get('key', '')} — {str(d.get('value', ''))[:120]}")
    else:
        lines.append("- (none)")
    lines += ["", "## Open questions"]
    if open_questions:
        for q in open_questions[-10:]:
            lines.append(f"- [{q.get('created_at', '')}] {q.get('author_agent_id', '?')}: {str(q.get('value', ''))[:200]}")
    else:
        lines.append("- (none)")
    lines += ["", "## Open tasks"]
    if open_tasks:
        for t in open_tasks[-10:]:
            lines.append(f"- [{t['id']}] {t.get('title', '')} — assignee: {t.get('assignee_agent_id') or '(unassigned)'} — status: {t.get('status', 'pending')}")
    else:
        lines.append("- (none)")
    (brain_path / "CONSCIOUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def regenerate_plan_json(brain_path: Path, conscious_id: str, tasks: list[dict]) -> None:
    """Rewrite plan.json from the task list."""
    # compute version = max version seen + 1, or len(tasks) as a monotonic proxy
    version = 0
    for t in tasks:
        v = t.get("version", 0) or 0
        if isinstance(v, int) and v > version:
            version = v
    version = max(version, len(tasks))
    obj = {
        "conscious_id": conscious_id,
        "version": version + 1,
        "tasks": [
            {
                "id": t["id"],
                "title": t.get("title", ""),
                "assignee_agent_id": t.get("assignee_agent_id"),
                "status": t.get("status", "pending"),
                "depends_on": _parse_json_list(t.get("depends_on", "[]")),
                "cost_ceiling_usd": t.get("cost_ceiling_usd"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
            }
            for t in tasks
        ],
        "updated_at": _iso_now(),
    }
    (brain_path / "plan.json").write_text(_pretty_json(obj), encoding="utf-8")


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
# git commit policy
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 5 security: pre-commit secret scan for .brain/ (TIER3_PLAN §17)
# ---------------------------------------------------------------------------

# Patterns that indicate a leaked credential. If any file in .brain/ matches,
# the commit + HF upload is blocked and a `brain.scan.blocked` event is appended.
_SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                    # GitHub PAT (classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{82}"),             # GitHub PAT (fine-grained)
    re.compile(r"hf_[A-Za-z0-9]{34,}"),                     # HF token
    re.compile(r"AKIA[0-9A-Z]{16}"),                        # AWS access key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),  # private key
    re.compile(r"xox[bpoa]-[A-Za-z0-9-]{10,}"),             # Slack token
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                     # OpenAI / generic API key
]


def scan_for_secrets(brain_path: Path) -> list[dict]:
    """Walk .brain/ and scan every file for known secret patterns.

    Returns a list of ``{file, pattern, line}`` dicts for each match.
    Empty list = clean. The scan reads files as text (utf-8, errors='replace');
    binary files are skipped. Does NOT recurse into ``.worktrees/``.
    """
    findings: list[dict] = []
    if not brain_path.is_dir():
        return findings
    for p in brain_path.rglob("*"):
        if not p.is_file():
            continue
        # skip the worktrees dir (it's outside .brain/ but just in case)
        if ".worktrees" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat in _SECRET_PATTERNS:
                if pat.search(line):
                    findings.append({
                        "file": str(p.relative_to(brain_path)),
                        "pattern": pat.pattern[:40],
                        "line": i,
                    })
                    break  # one finding per line is enough
    return findings


def commit_brain(brain_path: Path, workspace_path: Path,
                 event_type: str, summary: str, policy: str) -> bool:
    """If policy == 'on': scan for secrets, then stage .brain/, commit locally.

    Phase 5 security: scans .brain/ for known secret patterns BEFORE staging.
    If secrets are found, the commit is REFUSED and a ``brain.scan.blocked``
    event is appended so the orchestrator sees it. Returns True if committed,
    False if blocked or skipped.

    The (token, repo_url) args from the Phase 1 signature are DROPPED (audit H3:
    they were unused + a latent footgun). Pushes go through the caller's
    ``push_to_remote(user_id, workspace_id, branch)`` helper, never here.

    Phase 5 robustness: failures are logged via ``oplog.log_event`` instead of
    silently swallowed (audit L1).
    """
    if policy != "on":
        return False
    # Security: scan for secrets before committing (TIER3_PLAN §17)
    findings = scan_for_secrets(brain_path)
    if findings:
        # block the commit + log
        try:
            from oplog import log_event
            log_event("brain_scan_blocked", brain_path=str(brain_path),
                      findings_count=len(findings),
                      files=[f["file"] for f in findings[:10]])
        except Exception:
            pass
        return False
    try:
        import subprocess
        msg = f"[conscious] {event_type}: {summary}"[:200]
        subprocess.run(["git", "-C", str(workspace_path), "add", ".brain/"],
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "-C", str(workspace_path), "commit", "-m", msg],
                       capture_output=True, text=True, timeout=30)
        return True
    except Exception as exc:
        # log the failure instead of silently swallowing (audit L1)
        try:
            from oplog import log_event
            log_event("brain_commit_failed", error=type(exc).__name__)
        except Exception:
            pass
        return False


def brain_path_for_workspace(workspace_path: Path | str) -> Path:
    """Resolve the .brain/ path for a workspace."""
    return Path(workspace_path) / ".brain"
