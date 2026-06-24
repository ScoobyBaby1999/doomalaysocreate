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

| Secret | Purpose |
|--------|---------|
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth app (create one at github.com/settings/developers) |
| `HF_CLIENT_ID` / `HF_CLIENT_SECRET` | HF OAuth app (create one at hf.co/settings/apps) |
| Your LLM provider API keys | See providers_catalog.json for all supported providers |

Everything else (rotation secret, workspace storage) auto-configures on first boot.
