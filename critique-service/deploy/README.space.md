---
title: loom
emoji: 🧵
colorFrom: gray
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# loom — multi-model judge panel, in your pocket

One free Space = the whole product: open the Space URL on any phone or browser and
the **web app** loads; the same URL also serves the **panel API** (`/health`,
`/api/panel`, `/api/critique`, `/api/run`). Everything runs on free-tier provider
keys that **you** bring — nothing is shared with anyone else.

## Get your own (no PC needed)

1. Tap **⋮ → Duplicate this Space** (free CPU hardware is plenty).
2. In **Settings → Variables and secrets** of *your* copy, add:
   - `CRITIQUE_ROTATION_SECRET` — your auth root secret
     (generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`,
     or any long random string). Never share it; it never travels on the wire.
   - At least one provider key from the CORE pool — see `.env.example` in the
     repo (`NVIDIA_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`,
     `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, …). All have free tiers.
3. Open your Space URL. In the app's **Settings** tab, leave the Gateway URL
   empty (same origin) and paste the same rotation secret. Done — add it to your
   home screen for an app-like experience.

Your copy is fully isolated: your container, your secrets, your metrics. Two
users' Spaces never communicate or know of each other.

## Notes

- Free Spaces **sleep when idle**; the first request after a nap cold-starts
  (~30s). Subsequent calls are fast.
- API docs, security model, and privacy modes: see the source repo's
  `critique-service/` README, SECURITY.md and PRIVACY.md.
- Static token (`CRITIQUE_TOKEN`) is still supported instead of the rotation
  secret, but the rotating scheme is safer for anything you use from chats/phones.
