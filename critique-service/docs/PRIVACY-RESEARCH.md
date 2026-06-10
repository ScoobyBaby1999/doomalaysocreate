# Privacy + free-tier utility research (P1 groundwork)

Panel research job `dddd33442894ba3e` (kimi-k2.6, nemotron-ultra, glm-5.1 done; gpt-oss
429, gemma 504), reasoning on, profile `privacy_research`, plus operator WebFetch
verification. Drives Roadmap P1.

## Architecture decision (locked by user): privacy BY ISOLATION

**The final product gives every user their own sub-space with their own provider keys —
nothing shared.** This is the strongest possible answer to multi-tenancy: there is no
shared API token and no cross-tenant data, so we do NOT build per-user access-control
inside one backend. Each instance is single-tenant by construction.

What this changes for P1:
- **Drop** in-backend per-user job-ownership/ACL (isolation = separate deployments).
- **Keep + emphasize** `PRIVACY_MODE` provider routing, and `PRIVACY.md` that tells each
  user how to opt out of training/logging in THEIR OWN provider accounts.
- Still worth doing: optional encryption-at-rest, a stale-data TTL sweep, and a
  "don't persist my content" switch (privacy jobs trade deploy-resume for zero retention).

## Per-provider data posture
Tags: ✅ no-training by default · ⚠️ conditional/uncertain · 🔴 trains/logs by default.
"verify" = confirm against the live policy before public release.

| Provider | Default posture | Opt-out / notes |
|---|---|---|
| **NVIDIA NIM** (build.nvidia.com) | ✅ does not train on submissions; ~30-day abuse/security logs | no flag needed (verify) |
| **Cloudflare Workers AI** | ✅ no training on inputs | no flag needed (verify) |
| **GitHub Models** | ✅ no training (Azure-backed inference) | verify retention terms |
| **Mistral La Plateforme** | ⚠️ not used to train under standard terms; no documented free-tier zero-retention flag | verify at legal.mistral.ai/terms |
| **OpenRouter** | 🔴 for `:free` | **TRAP (confirmed by panel ×3):** `:free` routes REQUIRE consent to prompt logging/training as a condition of use; account-level Privacy is incompatible with / disabled for them. Paid routes: no training, shielded by the Privacy setting. → **mark every `:free` slot `trains_on_data:true`.** verify at openrouter.ai privacy docs |
| **HuggingFace Inference Providers** | ⚠️ depends on routed backend | **CONFIRMED via HF docs: HF Inference is a PROXY** to third-party backends (Cerebras, Together, SambaNova, fal, Replicate, Novita, Groq…). HF itself doesn't train, but posture = the backend serving the request. → pin the backend via the `:provider` suffix and only allow no-training backends; never treat HF as blanket-safe. |

**Safest-by-default for `PRIVACY_MODE`:** Cloudflare, NVIDIA, GitHub, Mistral. Exclude
OpenRouter `:free` and any unpinned HF route from the privacy-safe router.

## Mid-generation resume — reality
No provider resumes KV-cache / generation state via a handle. To "continue" a partial
output you re-send the full prior context + the partial assistant text + a "continue"
turn — which **reprocesses all tokens** (not free, and thinking-models don't resume
traces cleanly). Works on all OpenAI-compat hosts (NVIDIA, CF, GitHub, OpenRouter,
Mistral); HF via raw-prompt append.
- **Action:** persist the partial output/reasoning instead of dropping it (today it's
  lost on interrupt → full restart), and offer opt-in textual continuation where useful.

## Maximize free-tier utility (within ToS)
- **Model→task routing:** smallest viable model for extraction/classification; reserve
  large reasoners for reasoning/code. (Don't pay frontier tokens for trivial work.)
- **Prompt cache** (exact + semantic) in front of the router — biggest single saver.
- **Failover by generosity/latency:** Cloudflare (highest free daily) → NVIDIA → GitHub
  → Mistral as primaries; OpenRouter `:free` + HF as *guarded* fallbacks.
- **Context-window economy:** rolling window + distilled summary instead of full history.
- **Task-appropriate token defaults** (NOT hard caps — caps removed): size the default
  to the task's typical length so we don't burn budget on padding or truncate-and-retry.

## Top 3 highest-leverage actions (P1)
1. Add `trains_on_data` / `privacy_optout_url` / `stability_tier` per slot; `PRIVACY_MODE`
   (default ON) routes only to ✅ slots and `/health.frontier_ok` counts only those.
2. Ship a gateway prompt cache (exact first, semantic later) — utility multiplier.
3. Persist partial generations + offer opt-in continuation; never silently restart.
