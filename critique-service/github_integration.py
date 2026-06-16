"""GitHub integration for the loom critique service.

Handles GitHub OAuth token exchange, encrypted storage, repo/branch/content
proxying, workspace CRUD, git commit/push/PR operations, and the public
workspace registry.  All GitHub API calls go through stdlib urllib (no httpx
at module level) to stay compatible with the stdlib-only server.

Env vars required:
    GITHUB_CLIENT_ID       — GitHub OAuth App client ID
    GITHUB_CLIENT_SECRET   — GitHub OAuth App client secret
    ENCRYPTION_KEY         — Fernet key for token encryption (auto-generated if absent)

The module is stateless — all durable state lives in ``db.py``.  Token
encryption/decryption is handled by ``crypto.py``.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

import crypto
import db
import jwt_auth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

# Scopes needed for repo access + push + PR creation
GITHUB_SCopes = "read:user user:email repo"


def _github_configured() -> bool:
    return bool(
        os.environ.get("GITHUB_CLIENT_ID", "").strip()
        and os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    )


# ---------------------------------------------------------------------------
# GitHub OAuth flow
# ---------------------------------------------------------------------------

def make_github_authorize_url(state: str) -> str:
    """Build the GitHub OAuth authorize URL with state token."""
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
        "scope": GITHUB_SCopes,
        "state": state,
    })
    return f"{GITHUB_AUTHORIZE_URL}?{params}"


def exchange_github_code(code: str) -> str:
    """Exchange OAuth code for access token.  Returns the raw token string.

    Raises RuntimeError on failure.
    """
    from urllib.parse import urlencode
    body = urlencode({
        "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
        "client_secret": os.environ.get("GITHUB_CLIENT_SECRET", ""),
        "code": code,
    }).encode()
    req = urllib.request.Request(GITHUB_TOKEN_URL, data=body, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"GitHub token exchange failed (HTTP {exc.code}): {detail}") from exc
    token = data.get("access_token", "").strip()
    if not token:
        raise RuntimeError(f"GitHub token exchange returned no access_token: {data}")
    return token


def get_github_user(token: str) -> dict:
    """Fetch authenticated user info from GitHub API. Returns dict with
    'id', 'login', 'avatar_url', etc."""
    return _github_api("/user", token=token)


def upsert_user_from_github(github_token: str, user_id: str | None = None) -> dict:
    """Fetch GitHub user info, upsert into DB, return the user row.

    If ``user_id`` is None, a deterministic ID is derived from the GitHub user id
    (via ``jwt_auth.derive_user_id``) so the row survives DB rebuilds.
    """
    info = get_github_user(github_token)
    encrypted = crypto.encrypt_token(github_token)
    uid = user_id or jwt_auth.derive_user_id(info["id"])
    return db.upsert_user(
        user_id=uid,
        github_id=info["id"],
        github_username=info["login"],
        github_token_encrypted=encrypted,
    )


# ---------------------------------------------------------------------------
# GitHub API proxy (uses stored token from DB)
# ---------------------------------------------------------------------------

def _github_api(path: str, *, token: str = "", method: str = "GET",
                body: dict | None = None) -> dict | list:
    """Raw GitHub API call via stdlib urllib."""
    url = f"{GITHUB_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"GitHub API {path} failed (HTTP {exc.code}): {detail}") from exc


def _token_for_user(user_id: str) -> str:
    """Retrieve and decrypt a user's stored GitHub token.  Raises if missing."""
    user = db.get_user(user_id)
    if not user or not user.get("github_token_encrypted"):
        raise RuntimeError("GitHub not connected — please link your GitHub account")
    return crypto.decrypt_token(user["github_token_encrypted"])


def list_user_repos(user_id: str, *, page: int = 1, per_page: int = 30,
                    sort: str = "updated") -> list[dict]:
    """List the authenticated user's repositories."""
    token = _token_for_user(user_id)
    path = f"/user/repos?page={page}&per_page={per_page}&sort={sort}&type=all"
    result = _github_api(path, token=token)
    # return slim fields the frontend needs
    return [
        {
            "full_name": r["full_name"],
            "name": r["name"],
            "owner": r["owner"]["login"],
            "private": r["private"],
            "default_branch": r["default_branch"],
            "description": r.get("description") or "",
            "updated_at": r["updated_at"],
            "clone_url": r["clone_url"],
        }
        for r in (result if isinstance(result, list) else [])
    ]


def list_repo_branches(user_id: str, owner: str, repo: str) -> list[dict]:
    """List branches for a repo."""
    token = _token_for_user(user_id)
    result = _github_api(f"/repos/{owner}/{repo}/branches?per_page=100", token=token)
    return [
        {"name": b["name"], "sha": b["commit"]["sha"]}
        for b in (result if isinstance(result, list) else [])
    ]


def get_file_content(user_id: str, owner: str, repo: str,
                     path: str, ref: str = "main") -> dict:
    """Fetch a single file's content from a repo."""
    token = _token_for_user(user_id)
    from urllib.parse import quote
    result = _github_api(
        f"/repos/{owner}/{repo}/contents/{quote(path, safe='')}?ref={ref}",
        token=token)
    return result  # {name, path, content (base64), encoding, sha, ...}


def get_repo_tree(user_id: str, owner: str, repo: str,
                  ref: str = "main", recursive: bool = True) -> list[dict]:
    """Fetch the full file tree of a repo (for context packing)."""
    token = _token_for_user(user_id)
    suffix = "?recursive=1" if recursive else ""
    result = _github_api(f"/repos/{owner}/{repo}/git/trees/{ref}{suffix}", token=token)
    return result.get("tree", []) if isinstance(result, dict) else []


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

def create_workspace(user_id: str, *, title: str, description: str = "",
                     source_repo: str | None = None,
                     source_branch: str | None = None,
                     source_branches: list[str] | None = None,
                     visibility: str = "private",
                     auto_sync: bool = False) -> dict:
    """Create a new workspace.  If source_repo is given, clone it into the
    sandbox directory.  source_branches can specify multiple branches to clone;
    when source_branch is None and source_branches is None, all branches are cloned."""
    sandbox_base = Path(os.environ.get("WORKSPACE_BASE", "/workspace"))
    ws_id = db._gen_id()
    sandbox_path = str(sandbox_base / ws_id)
    Path(sandbox_path).mkdir(parents=True, exist_ok=True)
    ws = db.create_workspace(
        user_id, title=title, description=description,
        source_repo=source_repo, source_branch=source_branch,
        source_branches=source_branches,
        visibility=visibility, auto_sync=auto_sync,
        sandbox_path=sandbox_path)
    if source_repo:
        _clone_repo(user_id, source_repo, source_branch,
                    dest=sandbox_path, branches=source_branches)
    return ws


def list_workspaces(user_id: str) -> list[dict]:
    return db.list_user_workspaces(user_id)


def get_workspace(workspace_id: str) -> dict | None:
    return db.get_workspace(workspace_id)


def update_workspace(workspace_id: str, **fields) -> dict | None:
    return db.update_workspace(workspace_id, **fields)


def delete_workspace(workspace_id: str) -> bool:
    ws = db.get_workspace(workspace_id)
    if ws and ws.get("sandbox_path"):
        import shutil
        p = Path(ws["sandbox_path"])
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    return db.delete_workspace(workspace_id)


# ---------------------------------------------------------------------------
# Git operations (commit, push, PR)
# ---------------------------------------------------------------------------

def _run_git(sandbox: str, *args: str) -> str:
    """Run a git command in the workspace sandbox.  Returns stdout."""
    import subprocess
    cmd = ["git", "-C", sandbox] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {_sanitize_git_error(result.stderr.strip())}")
    return result.stdout.strip()


def _sanitize_git_error(err: str) -> str:
    """Strip URLs and auth tokens from git error messages."""
    import re
    # redact anything that looks like https://...@... or x-access-token:...
    err = re.sub(r'https://[^@]*@', 'https://***@', err)
    err = re.sub(r'x-access-token:[^\s]+', 'x-access-token:***', err)
    return err


def _clone_repo(user_id: str, repo_url: str, branch: str | None = None,
                dest: str | None = None, branches: list[str] | None = None) -> None:
    """Clone a GitHub repo into ``dest``.

    Three modes:
    * ``branch=None`` and ``branches=None`` — full repo clone (all branches).
    * ``branch='main'`` and ``branches=None`` — single branch (current behaviour).
    * ``branches=['main','dev']`` — clone listed branches (first is default).
    """
    import subprocess
    if dest is None:
        raise RuntimeError("dest is required for _clone_repo")
    token = _token_for_user(user_id)
    auth = f"http.extraHeader=Authorization: Bearer {token}"

    # Mode A: clone all branches
    if branch is None and branches is None:
        cmd = [
            "git", "clone", "--no-single-branch", "--depth", "1",
            "-c", auth, repo_url, dest,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("git clone timed out")
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {_sanitize_git_error(result.stderr.strip())}")
        return

    # Mode C: clone specific branches
    if branches:
        first = branches[0]
        cmd = [
            "git", "clone", "--depth", "1", "-b", first, "--single-branch",
            "-c", auth, repo_url, dest,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("git clone timed out")
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {_sanitize_git_error(result.stderr.strip())}")

        for extra in branches[1:]:
            fetch = [
                "git", "-C", dest, "fetch", "origin",
                f"{extra}:refs/remotes/origin/{extra}", "--depth", "1",
                "-c", auth,
            ]
            r = subprocess.run(fetch, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(
                    f"git fetch {extra} failed: {_sanitize_git_error(r.stderr.strip())}")
            # Create a local branch tracking the fetched remote
            co = subprocess.run(
                ["git", "-C", dest, "checkout", "-b", extra, f"origin/{extra}"],
                capture_output=True, text=True, timeout=30)
            if co.returncode != 0:
                raise RuntimeError(
                    f"git checkout {extra} failed: {_sanitize_git_error(co.stderr.strip())}")
        # Checkout first branch as default
        subprocess.run(
            ["git", "-C", dest, "checkout", first],
            capture_output=True, timeout=30)
        return

    # Mode B: single branch (default to "main")
    effective = branch or "main"
    cmd = [
        "git", "clone", "--depth", "1", "-b", effective,
        "-c", auth, repo_url, dest,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("git clone timed out")
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {_sanitize_git_error(result.stderr.strip())}")


def init_repo(sandbox: str) -> None:
    """Init a fresh git repo in the sandbox (no remote)."""
    import subprocess
    subprocess.run(["git", "init"], cwd=sandbox, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "agent@loom.local"],
                   cwd=sandbox, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Loom Agent"],
                   cwd=sandbox, capture_output=True, timeout=10)


def checkout_branch(sandbox: str, branch: str) -> None:
    """Checkout an existing branch or create it from the current HEAD."""
    import re
    if not branch or '..' in branch or branch.startswith('-') or ' ' in branch:
        raise RuntimeError(f"invalid branch name: {branch!r}")
    if not re.match(r'^[a-zA-Z0-9_./\-]+$', branch):
        raise RuntimeError(f"invalid branch name: {branch!r}")
    try:
        _run_git(sandbox, "checkout", branch)
    except RuntimeError:
        # branch doesn't exist locally — create it from HEAD
        _run_git(sandbox, "checkout", "-b", branch)


def commit_changes(sandbox: str, message: str) -> str:
    """Stage all changes and commit.  Returns the new commit SHA."""
    _run_git(sandbox, "add", "-A")
    # check if there's anything to commit
    status = _run_git(sandbox, "status", "--porcelain")
    if not status.strip():
        raise RuntimeError("Nothing to commit — no changes detected")
    _run_git(sandbox, "commit", "-m", message)
    return _run_git(sandbox, "rev-parse", "HEAD")


def push_to_remote(user_id: str, workspace_id: str, branch: str,
                   *, force: bool = False) -> str:
    """Push the workspace branch to the source repo's remote.  Returns commit SHA."""
    ws = db.get_workspace(workspace_id)
    if not ws:
        raise RuntimeError("Workspace not found")
    if not ws.get("source_repo"):
        raise RuntimeError("Workspace has no source repo configured")
    token = _token_for_user(user_id)
    sandbox = ws["sandbox_path"]
    # set remote URL (no token in URL — use http.extraHeader for auth)
    repo_url = ws["source_repo"]
    try:
        _run_git(sandbox, "remote", "set-url", "origin", repo_url)
    except RuntimeError:
        _run_git(sandbox, "remote", "add", "origin", repo_url)
    # push with auth header
    args = [
        "push", "-u", "origin", branch,
        "-c", f"http.extraHeader=Authorization: Bearer {token}",
    ]
    if force:
        args.append("--force")
    try:
        _run_git(sandbox, *args)
    except RuntimeError as exc:
        raise RuntimeError(f"git push failed: {_sanitize_git_error(str(exc))}")
    sha = _run_git(sandbox, "rev-parse", "HEAD")
    # log the push
    db.log_push(
        user_id, workspace_id,
        target_repo=ws["source_repo"],
        target_branch=branch,
        commit_sha=sha,
        push_type="direct",
        approved_by_user=True,
    )
    return sha


def create_pull_request(user_id: str, workspace_id: str, *,
                        title: str, body: str = "",
                        head_branch: str = "main",
                        base_branch: str = "main") -> dict:
    """Push branch + create a PR on the source repo via GitHub API.
    Returns PR dict with 'number', 'html_url', etc."""
    ws = db.get_workspace(workspace_id)
    if not ws:
        raise RuntimeError("Workspace not found")
    if not ws.get("source_repo"):
        raise RuntimeError("Workspace has no source repo configured")
    token = _token_for_user(user_id)
    sandbox = ws["sandbox_path"]
    # push first
    push_to_remote(user_id, workspace_id, head_branch)
    # extract owner/repo from source_repo URL
    source = ws["source_repo"]
    # https://github.com/owner/repo.git or /owner/repo
    parts = source.rstrip("/").replace(".git", "").split("/")
    owner_repo = "/".join(parts[-2:])
    # create PR via API
    pr_body = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    result = _github_api(f"/repos/{owner_repo}/pulls", token=token, body=pr_body)
    # log
    db.log_push(
        user_id, workspace_id,
        target_repo=source,
        target_branch=base_branch,
        commit_sha=_run_git(sandbox, "rev-parse", "HEAD"),
        commit_message=title,
        push_type="pr",
        pr_number=result.get("number"),
        pr_url=result.get("html_url"),
        approved_by_user=True,
    )
    return {
        "number": result.get("number"),
        "html_url": result.get("html_url"),
        "state": result.get("state"),
        "title": result.get("title"),
    }


# ---------------------------------------------------------------------------
# Public workspace registry
# ---------------------------------------------------------------------------

def publish_workspace(user_id: str, workspace_id: str) -> dict:
    """Register a workspace as public in the registry."""
    ws = db.get_workspace(workspace_id)
    if not ws:
        raise RuntimeError("Workspace not found")
    user = db.get_user(user_id)
    gh_user = user.get("github_username", "") if user else ""
    return db.register_public_workspace(
        workspace_id,
        owner_username=gh_user,
        title=ws.get("title", ""),
        description=ws.get("description", ""),
        source_repo=ws.get("source_repo"),
    )


def unpublish_workspace(workspace_id: str) -> None:
    db.unregister_public_workspace(workspace_id)


def list_registry(*, page: int = 1, per_page: int = 20,
                  sort: str = "recent", search: str = "") -> dict:
    return db.list_public_workspaces(page=page, per_page=per_page,
                                     sort=sort, search=search)


# ---------------------------------------------------------------------------
# Push approval queue (for the frontend approval flow)
# ---------------------------------------------------------------------------

# In-memory approval queue: request_id -> {user_id, workspace_id, action, ...}
_approval_queue: dict[str, dict] = {}
_approval_lock = __import__("threading").Lock()


def enqueue_push_request(user_id: str, workspace_id: str, action: str,
                         **extra) -> str:
    """Create a push approval request.  Returns request_id."""
    rid = secrets.token_urlsafe(16)
    with _approval_lock:
        _approval_queue[rid] = {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "action": action,
            "created_at": time.time(),
            **extra,
        }
    return rid


# Push requests expire after 1 hour.
_PUSH_REQUEST_TTL_S = 3600


def resolve_push_request(request_id: str, approved: bool, user_id: str = "") -> dict | None:
    """Approve or reject a queued push request.  Executes the action if approved.
    If user_id is provided, verifies ownership before resolving."""
    with _approval_lock:
        req = _approval_queue.get(request_id)
        if req is None:
            return None
        if user_id and req["user_id"] != user_id:
            return None  # ownership mismatch — don't reveal existence
        # check TTL
        if time.time() - req["created_at"] > _PUSH_REQUEST_TTL_S:
            del _approval_queue[request_id]
            return None  # expired
        # only pop if we're going to act on it
        del _approval_queue[request_id]
    if not approved:
        return {"status": "rejected", "action": req["action"]}
    # execute the approved action
    action = req["action"]
    user_id = req["user_id"]
    workspace_id = req["workspace_id"]
    try:
        if action == "git_push":
            branch = req.get("branch", "main")
            sha = push_to_remote(user_id, workspace_id, branch)
            return {"status": "pushed", "commit_sha": sha, "branch": branch}
        elif action == "git_force_push":
            branch = req.get("branch", "main")
            sha = push_to_remote(user_id, workspace_id, branch, force=True)
            return {"status": "force_pushed", "commit_sha": sha, "branch": branch}
        elif action == "gh_pr_create":
            result = create_pull_request(
                user_id, workspace_id,
                title=req.get("title", "Agent changes"),
                body=req.get("body", ""),
                head_branch=req.get("head_branch", "main"),
                base_branch=req.get("base_branch", "main"))
            return {"status": "pr_created", **result}
        else:
            return {"status": "error", "message": f"Unknown action: {action}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
