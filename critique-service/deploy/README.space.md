---
title: loom
emoji: 🧵
colorFrom: gray
colorTo: indigo
sdk: docker
app_port: 7860
hf_oauth: true
hf_oauth_scopes:
  - manage-repos
pinned: false
---

# loom — multi-model judge panel, in your pocket

One free Space = the whole product: open the Space URL on any phone or browser,
**sign in with your Hugging Face account**, and the app auto-provisions your own
private Space — your container, your secrets, your URL. Nothing is shared.

## Get your own (takes ~2 minutes, no PC needed)

1. Open this Space on your phone.
2. Tap **"Sign in with Hugging Face"** — HF OAuth creates your own `<you>/loom` Space
   automatically with a fresh rotation secret already set.
3. Add one free provider API key in the guided wizard (NVIDIA, Google, Groq, or
   OpenRouter — all have free tiers with instant signup).
4. Tap **"Open my Space"** → you land at `<you>-loom.hf.space`, fully configured.
   Add it to your home screen for an app-like experience.

Your copy is fully isolated: your container, your secrets, your metrics. Two
users' Spaces never communicate or know of each other.

---

## Manual setup (if you prefer)

1. Tap **⋮ → Duplicate this Space** (free CPU hardware is plenty).
2. In **Settings → Variables and secrets** of *your* copy, add:
   - `CRITIQUE_ROTATION_SECRET` — generate one:
     `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - At least one provider key (`NVIDIA_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, …)
3. Open your Space URL → Settings tab → leave Gateway URL empty, paste rotation secret.

## Notes

- Free Spaces **sleep when idle**; the first request after a nap cold-starts
  (~30s). Subsequent calls are fast.
- API docs, security model, and privacy modes: see the source repo's
  `critique-service/` README, SECURITY.md and PRIVACY.md.
- Static token (`CRITIQUE_TOKEN`) is still supported as a fallback.
