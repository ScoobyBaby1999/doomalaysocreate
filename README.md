---
title: Doomalaysocreate
emoji: "\U0001F525"
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# doomalaysocreate — multi-tenant AI panel gateway

A self-contained Next.js 16 fullstack app: a **multi-model judge panel** (fan an input
out to a diverse panel of frontier LLMs across providers, merge their uncorrelated
opinions) plus a **streaming chatbot**, **templates**, **per-space metrics**, and a
**bring-your-own-keys** provider vault.

## Duplicate this Space to get your own

Click **"Duplicate this Space"** in the top-right. You'll get a completely isolated
deployment with its own database, its own users, and its own configuration. Every user
in your Space brings their **own provider API keys** (encrypted at rest).

### Required secrets for your duplicate

Set these in **Settings → Repository secrets**:

| Secret | Purpose |
|---|---|
| `APP_SECRET` | Encryption key for provider API keys (AES-256-GCM). Auto-generated if unset, but set your own for persistence across restarts. |
| `DATABASE_URL` | SQLite path. Defaults to `file:/data/custom.db` (HF persistent storage). |

The built-in **Z.ai** provider works with zero configuration — no key needed. Users add
their own keys for NVIDIA, Groq, OpenRouter, Cloudflare, GitHub Models, Google, and more
in the **API Keys** tab.

## Features

- **Judge Panel** — fan one input out to N frontier models, merge consensus-tagged bullets.
  One judge failing never fails the request. Async jobs with live polling.
- **Streaming Chat** — single-model chat with real-time token streaming. Uses the built-in
  Z.ai provider or any user-provided key.
- **Duplicate Spaces** — fork any workspace into a fresh environment in one click.
- **Bring Your Own Keys** — every user stores their own provider API keys, encrypted at rest.
- **Templates** — multi-stage pipelines: repo audit, design doc, red team, panel debate.
- **Per-Space Metrics** — calls, success rate, throttle rate, latency, tokens per provider/model.

## Tech stack

Next.js 16 (App Router) · TypeScript 5 · Tailwind CSS 4 · shadcn/ui · Prisma (SQLite) ·
Zustand · z-ai-web-dev-sdk (built-in AI provider)

## Local development

```bash
bun install
bun run db:push    # initialize SQLite schema
bun run dev        # http://localhost:3000
```

## Architecture

This is a reimplementation of the original `doomalaysocreate` (Python judge-panel gateway
on the `c` branch + Vite agentic-coder client on the `socreate` branch) as a single
Next.js fullstack app. The multi-tenant model:

- **HF Space deployment** = top-level tenant. Duplicating the Space gives you a fresh
  isolated environment.
- **In-app Spaces** = workspaces/teams within a deployment. Each has its own members,
  conversations, panel jobs, and metrics.
- **Users** — each user authenticates with email/password and stores their own encrypted
  provider API keys.
