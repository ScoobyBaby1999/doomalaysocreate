"""Git command interception for the agent sandbox.

Prevents the LLM agent from pushing to remotes, modifying git config, or
accessing credentials without explicit user approval.  Blocked commands surface
as approval prompts in the frontend.

The filter is a simple regex scan — not a full shell parser — so it catches
the common patterns an LLM would emit.  Edge cases (encoded payloads, heredocs)
are handled by the broader container sandbox (the agent has no network access
except through the backend).
"""
from __future__ import annotations

import re

# Patterns that require user approval before execution.  Order matters:
# more specific patterns first so they match before broader ones.
APPROVAL_REQUIRED_PATTERNS: list[tuple[str, str]] = [
    (r"git\s+push",               "git_push"),
    (r"git\s+push\s+.*--force",   "git_force_push"),
    (r"git\s+push\s+.*-f\b",      "git_force_push"),
    (r"git\s+remote\s+add",       "git_remote_add"),
    (r"git\s+remote\s+set-url",   "git_remote_set_url"),
    (r"git\s+config\s+credential", "git_credential_config"),
    (r"gh\s+pr\s+create",         "gh_pr_create"),
    (r"gh\s+pr\s+merge",          "gh_pr_merge"),
    (r"gh\s+repo\s+delete",       "gh_repo_delete"),
    (r"rm\s+-rf\s+/",             "rm_root"),
    (r"rm\s+-rf\s+~",             "rm_home"),
]

# Patterns that are always blocked (never approved).
ALWAYS_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    (r"git\s+config\s+.*--global",  "git_global_config"),
    (r"curl\s+.*\|\s*sh",          "pipe_to_shell"),
    (r"wget\s+.*\|\s*sh",          "pipe_to_shell"),
    (r"eval\s*\(",                  "eval_call"),
    (r"exec\s*\(",                  "exec_call"),
]


class GitInterceptResult:
    """Result of a git command interception check."""

    __slots__ = ("allowed", "reason", "action", "command")

    def __init__(self, allowed: bool, reason: str = "", action: str = "",
                 command: str = ""):
        self.allowed = allowed
        self.reason = reason      # human-readable reason if blocked
        self.action = action      # machine-readable action key
        self.command = command    # the original command

    def to_dict(self) -> dict:
        d = {"allowed": self.allowed, "command": self.command}
        if self.reason:
            d["reason"] = self.reason
        if self.action:
            d["action"] = self.action
        return d


def check_command(cmd: str) -> GitInterceptResult:
    """Check if a shell command is allowed, needs approval, or is blocked.

    Returns:
        allowed=True  — command can run immediately
        allowed=False, action="" — command is permanently blocked
        allowed=False, action="git_push" etc — command needs user approval
    """
    cmd_stripped = cmd.strip()

    # always-blocked first
    for pattern, action in ALWAYS_BLOCKED_PATTERNS:
        if re.search(pattern, cmd_stripped, re.IGNORECASE):
            return GitInterceptResult(
                allowed=False,
                reason=f"Command permanently blocked: {action}",
                action=action,
                command=cmd)

    # approval-required
    for pattern, action in APPROVAL_REQUIRED_PATTERNS:
        if re.search(pattern, cmd_stripped, re.IGNORECASE):
            return GitInterceptResult(
                allowed=False,
                reason=f"User approval required: {action}",
                action=action,
                command=cmd)

    return GitInterceptResult(allowed=True, command=cmd)


def wrap_shell_command(cmd: str) -> dict:
    """Check a command and return a result dict suitable for the agent transcript.

    If the command needs approval, returns a tool_result-style dict that the
    agent sees as a "blocked" message, prompting it to inform the user.
    """
    result = check_command(cmd)
    if result.allowed:
        return {"blocked": False}
    return {
        "blocked": True,
        "status": "error",
        "content": [{"text": (
            f"Command requires user approval: {result.action}\n"
            f"Original command: {result.command}\n"
            f"This action has been queued for user review. "
            f"The user must approve this in the UI before it can execute."
        )}],
        "action": result.action,
    }
