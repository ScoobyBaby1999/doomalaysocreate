#!/bin/bash
# Claude Code on the web restores the container from a filesystem SNAPSHOT that can be
# stale (frozen at an early commit), so the working tree silently reverts and any
# uncommitted edits vanish, while pushed commits survive on origin. This SessionStart
# hook runs on every start/resume (cloud only) and re-syncs the tree to the latest
# pushed 'c' so each session begins from current state. Stray local changes are
# stashed first (recover with `git stash list`), never hard-deleted.
set -u
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
git fetch origin --quiet 2>/dev/null || exit 0
git rev-parse --verify origin/c >/dev/null 2>&1 || exit 0
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  git stash push -u -m "auto-stash session-start $(date -u +%FT%TZ)" >/dev/null 2>&1 || true
fi
git checkout -B c origin/c >/dev/null 2>&1 || git reset --hard origin/c >/dev/null 2>&1 || true
exit 0
