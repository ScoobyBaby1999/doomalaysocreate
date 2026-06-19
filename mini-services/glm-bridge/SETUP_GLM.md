# GLM 5.2 Setup Guide

## Quick Start: Free GLM 5.2 (zero setup, 30 seconds)

**Puter.js** — free GLM 5.2 in the browser, no API key, no account on Z.ai.

1. Visit your loom Space
2. Open the **Agent** tab
3. Select **"GLM 5.2 (free, via Puter)"** in the model picker
4. Send a message — a Puter sign-in popup appears
5. Create a free Puter account (Google/GitHub login, no phone)
6. Done — you have free GLM 5.2. Every future call is seamless.

**How it works:** Puter's "User-Pays" model. Each user authenticates with
their free Puter account and gets AI credits. The developer (you) pays $0.
If a user exceeds their free quota, they pay Puter directly (not you).

**Why this is the default:** It's the closest to "exactly like this chat" —
zero friction, zero cost, real GLM 5.2.

---

## Backend Setup (for Conscious agents)

The conscious system (multi-agent) runs on the backend. To use GLM there,
set ONE of these as a Space Secret (Settings → Repository secrets):

### Option A: Puter API Token (FREE, recommended)
1. Go to **puter.com/dashboard**
2. Sign in → click **"Copy"** to get your auth token
3. Add as Space Secret: `PUTER_API_TOKEN = <your-token>`
4. Conscious agents now use free GLM 5.2 through your Puter account

### Option B: NVIDIA NIM (FREE GLM-5.1, no phone)
1. Go to **build.nvidia.com** → sign in with GitHub
2. Generate an API key (1000 free credits, no credit card)
3. Add as Space Secret: `NVIDIA_API_KEY = <your-key>`
4. Note: NVIDIA serves GLM-5.1 (not 5.2)

### Option C: Z.ai API (real GLM-5.2, "Limited-time Free")
1. Go to **z.ai** → register → create an API key
2. Add as Space Secret: `ZAI_API_KEY = <your-key>`
3. Z.ai currently offers "Limited-time Free" cached input for GLM-5.2

### Option D: OpenRouter (GLM-5.2, $1 free credit)
1. Go to **openrouter.ai** → sign in with GitHub → get $1 free credit
2. Add as Space Secret: `OPENROUTER_API_KEY = <your-key>`
3. Credits never expire

### Option E: SiliconFlow (free tier, GitHub login)
1. Go to **siliconflow.com** → sign in with GitHub
2. Add as Space Secret: `SILICONFLOW_API_KEY = <your-key>`

---

## Provider Priority (backend bridge)

When multiple keys are set, the bridge uses this priority:

1. **PUTER_API_TOKEN** → free GLM-5.2 + 5.1 (user-pays, no cost to you)
2. **ZAI_API_KEY** → real GLM-5.2 + 5.1 (limited-time free cached input)
3. **NVIDIA_API_KEY** → real GLM-5.1 (1000 free credits)
4. **OPENROUTER_API_KEY** → GLM-5.2 + 5.1 (paid, $1 free credit)
5. **SILICONFLOW_API_KEY** → GLM models (free tier)
6. **File config** → sandbox fallback (session JWT, sandbox-only)

## Model Routing

- `glm-5.2` → Puter / Z.ai / OpenRouter (NVIDIA only has 5.1)
- `glm-5.1` → Puter / NVIDIA / Z.ai / OpenRouter
- `glm-4.x` → auto-upgraded to 5.2 (never silently serve 4.x)

## Verify Which Provider Is Active

```bash
# On your HF Space (via the gateway):
curl /health?XTransformPort=3030
# → {"provider":"puter","available_providers":[{"name":"puter","models":["glm-5.2","glm-5.1"]}]}
```

The `provider` field tells you which path is active. The `served_model` field
in chat responses tells you if you're getting real 5.2 (or being downgraded).

---

## FAQ

**Q: Is Puter really free?**
A: Yes for developers. Each user gets their own AI credits. If they exceed
the free quota, they pay Puter directly (typically pennies). You pay $0.

**Q: Do users need a Z.ai account?**
A: No. Puter handles the Z.ai API call on the user's behalf. Users only need
a free Puter account.

**Q: What about the sandbox's free GLM?**
A: The sandbox uses a Z.ai-internal session JWT that only works in Z.ai's
hosted sandbox. It can't be replicated for production. Puter.js is the
production equivalent: free GLM 5.2 for every user.

**Q: Can I use multiple providers?**
A: Yes. Set multiple keys — the bridge uses the highest-priority one that
serves the requested model. This gives automatic failover.

**Q: BigModel.cn?**
A: Offers free GLM-5.1 but requires a Chinese phone number for signup.
Listed for completeness; not recommended for international users.
