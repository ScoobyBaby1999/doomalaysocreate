# Privacy

This service is designed to run as **one instance per user — your own space, your own
provider keys, nothing shared.** There is no shared multi-tenant backend: your prompts
and outputs stay inside the instance you deploy. Privacy here is by *isolation*.

On top of that, the **privacy router** lets you refuse providers that train on or log your
data. Research behind these classifications: [`docs/PRIVACY-RESEARCH.md`](docs/PRIVACY-RESEARCH.md).

## Privacy modes (per request, or space-wide)

Send `"privacy": "strict" | "fallback" | "off"` on any `/api/critique` or `/api/panel` call:

- **strict** *(default)* — never routes to a host that trains on / logs submissions. If a
  model has no privacy-safe host, that judge returns `privacy_blocked` rather than leaking.
- **fallback** — prefers privacy-safe hosts; uses a non-safe one **only** if every safe host
  is down (cooling/blacklisted). Maximises availability while staying safe by default.
- **off** — uses all configured hosts.

Space-wide controls (env / Space secrets):
- `PRIVACY_MODE=1` (or `strict`) — hard-lock the whole instance to strict, ignoring requests.
- `DEFAULT_PRIVACY=strict|fallback|off` — default when a request omits `privacy` (default `strict`).
- `"no_store": true` on a request (or `NO_STORE=1`) — the job runs but its prompt/output are
  **never written to disk or the durable mirror** (trade-off: that job can't resume across a
  redeploy).

See the live posture and the ≥2-frontier guarantee any time: `GET /api/roster` and the
`frontier_ok` field on `GET /health`.

## Per-provider data posture & how to opt out (in YOUR account)

Because you bring your own keys, training/logging is governed by **your** provider accounts.
Posture as we classify it (verify against each provider's current policy before relying on it):

| Provider | Default | What to do in your account |
|---|---|---|
| **NVIDIA NIM** | ✅ no training on submissions | nothing required; abuse logs ~30d |
| **Cloudflare Workers AI** | ✅ no training on inputs | nothing required |
| **GitHub Models** | ✅ no training | nothing required; review GitHub data terms |
| **Mistral La Plateforme** | ⚠️ no training under standard terms | review `legal.mistral.ai/terms`; no documented free-tier zero-retention flag |
| **OpenRouter** | 🔴 `:free` routes **require** logging/training consent | enable account **Privacy** and use **paid** routes; `:free` slots are tagged unsafe and excluded in strict/fallback |
| **HuggingFace Inference** | ⚠️ proxy — posture = the routed backend | pin a no-training backend via the `:provider` suffix; HF itself doesn't train |

`:free` OpenRouter routes and any unpinned HF route are treated as **not** privacy-safe.

## What this backend itself stores

- **Operational metrics** (latency/cost/throttle counts per profile) — no prompt/output text
  unless you explicitly set `METRICS_SAMPLE_OUTPUTS=1`.
- **Job state** (for resume across restarts/redeploys) — includes prompts/outputs, written via
  atomic local files and mirrored to your **private** HF Dataset. Disable per-job with
  `no_store`. Jobs auto-expire (6h TTL).
- **Prompt cache** — your own results, local to your instance, to save repeat calls. Off-switch
  `CACHE_ENABLED=0`; TTL `CACHE_TTL_S`.

## Every off-switch

`PRIVACY_MODE` · `DEFAULT_PRIVACY` · `no_store` / `NO_STORE` · `CACHE_ENABLED` ·
`METRICS_SAMPLE_OUTPUTS` (default off) · `LOOM_LOG=0` (silence telemetry).
