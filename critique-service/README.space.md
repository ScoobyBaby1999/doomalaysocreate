---
title: Doomalaysocreate
emoji: "\U0001F525"
colorFrom: gray
colorTo: green
sdk: docker
pinned: false
license: mit
env:
  - name: WORKSPACE_BASE
    value: /data/workspaces
---

# doomalaysocreate

**Duplication notes — set these Space secrets for full functionality:**

| Secret | Purpose | Required? |
|--------|---------|-----------|
| `ZAI_API_KEY` + other provider keys | LLM API access for agents | Yes (at least one) |
| `GITHUB_TOKEN` | Clone repos from GitHub | Optional |

Everything else auto-configures or proxies through the main Space:

| What | How it's handled |
|------|-----------------|
| GitHub OAuth | Proxied through main Space — set your own `GITHUB_CLIENT_ID/SECRET` only if running a standalone main Space |
| HF OAuth | Proxied through main Space — set your own `HF_CLIENT_ID/SECRET` only if running a standalone main Space |
| `CRITIQUE_ROTATION_SECRET` | Auto-generated on first boot, persisted to `/data/` |
| `ENCRYPTION_KEY` | Auto-generated on first boot, persisted to `/data/` |
| `WORKSPACE_BASE` | Defaults to `/data/workspaces` (persistent) — Dockerfile default |
| `MAIN_SPACE_URL` | Defaults to https://scoobybaby1999-doomalaysocreate.hf.space — Dockerfile default |
