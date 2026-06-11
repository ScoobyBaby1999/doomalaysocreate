# Loom Model Panel: A Privacy-First Gateway for Uncorrelated Frontier-Model Judgment on Free Tiers

*System design document — 2026-06. All claims cross-checked against the code at the
commit that ships this file; constants quoted are the env-overridable defaults.*

## 1. Problem & motivation

A single LLM reviewing its own plans inherits its own blind spots. The highest-leverage
use of *other* frontier models is as **uncorrelated second opinions**: a panel of
models with different training lineages (Kimi, DeepSeek, GLM, Nemotron, Llama,
MiniMax…) judging one artifact produces failure-mode diversity a single model cannot.

Three constraints shape the system:

1. **Cost: zero.** Every model must come from recurring-free tiers (NVIDIA NIM free
   credits, Cloudflare Workers AI, GitHub Models, OpenRouter `:free`). Free tiers are
   throttled, flaky, and quota-capped — so reliability must be *engineered around* the
   providers, not assumed from them.
2. **Privacy: per-user, verifiable.** The deployment model is **one instance per user,
   the user's own provider keys, nothing shared** — privacy by isolation rather than by
   access control. On top, a request-level **privacy router** refuses providers that
   train on or log prompts.
3. **Operations: a single ephemeral container.** The target platform (Hugging Face
   Spaces, free Docker tier) gives one container that is recycled on every deploy.
   Anything that must survive — async jobs, metrics, templates, cache — needs explicit
   durability machinery.

The result is a stdlib-only Python service (plus `httpx` and optionally
`huggingface_hub`) exposing a token-guarded HTTP API: one-shot panels
(`/api/critique`, `/api/panel`), whole-repo review (`files`/`repo_url` forms), and
multi-stage orchestrated pipelines (`/api/run` + templates).

## 2. Architecture overview

```
client ──HTTP──▶ BoundedThreadingHTTPServer (MAX_WORKERS=48, REQUEST_TIMEOUT_S=30)
                   │  bearer auth (constant-time), body cap 8MB, 503/429 backpressure
                   ▼
             request builders (panel / critique / run)
              ├─ repopack: {files|repo_url} → packed blob + per-judge context budgeting
              ├─ privacy router: strict | fallback | off
              └─ prompt cache (exact-match, per-space)
                   │
        ┌──────────┴───────────┐
   sync (asyncio.run)     async JobRunner (one background event loop)
        │                      │  durable: JobStore (atomic file + HF Dataset mirror)
        ▼                      ▼  reload-on-boot; MAX_INFLIGHT_JOBS=200
              route_judge / orchestrator stages
                   │  per logical model:
                   ▼
             SlotScheduler ──▶ provider hosts (NVIDIA / Cloudflare / GitHub / OpenRouter)
              rotation · pacing · cooldown/blacklist · cross-provider failover
                   │
                   ▼  every call → per-profile MetricStore (HF-mirrored)
```

Two execution planes share one substrate. The **synchronous plane** runs a request to
completion on a bounded HTTP worker via `asyncio.run`. The **asynchronous plane**
(`{"async": true}`) hands work to a single long-lived background event loop in
`JobRunner`, returns a `job_id` immediately, and the client polls `GET /api/jobs/<id>`
for partial results. Both planes call the same `route_judge`/scheduler/metrics core, so
routing, privacy, caching, and telemetry behave identically regardless of plane.

The canonical request is for a **logical model** (e.g. `kimi-k2.6`); the scheduler maps
it to an ordered list of physical `(provider, model)` hosts and bounces across them on
failure. Catalogs (`providers_catalog.json`, `models_catalog.json`, `benchmarks.json`,
`reasoning_catalog.json`, `panel.json`) are the single source of truth — adding a model
or provider is a config edit, not a code change.

## 3. Subsystems

### 3.1 Routing & cross-provider failover (`scheduler.py`)
`SlotScheduler` owns rotation and resilience. `pick_slot_from(candidates, role,
exclude_providers)` returns the best eligible host for a logical model, where eligible =
not blacklisted, not in cooldown, not excluded. Ranking favors the least-recently-attempted
provider (rotation), then healthier model families, then freshness — so a flaky provider
drifts to the back instead of being hammered. `route_judge` (`jobs.py`) drives the bounce
loop: on any 429/5xx/empty/timeout it records a penalty (cooldown or blacklist), emits a
metric, and re-picks the next provider hosting the *same logical model*. Retries are
cross-provider by default (`JUDGE_RETRIES=0`): we move hosts rather than re-hit a failing
one. **Pacing** reserves each provider's next allowed call time under a lock
(`wait_for_provider_pacing`), so concurrent callers across both planes stagger to
distinct slots and stay under per-provider RPM.

### 3.2 Privacy router (`jobs.py` `_privacy_tiers` / `_pick_tiered`, `providers.slot_is_privacy_safe`)
Each provider carries a `trains_on_data` posture (`true`/`false`/`"per-model"`); a host is
privacy-safe iff it does not train on or log submissions. OpenRouter `:free` routes require
training consent, so they are `per-model` → unsafe; HF Inference is a proxy whose posture is
the routed backend's. Three request modes: **strict** (default) drops every unsafe host and
returns `privacy_blocked` rather than leak; **fallback** tries safe hosts first and an unsafe
host only as a last resort; **off** uses all. `PRIVACY_MODE` locks an instance to strict;
`no_store` runs a job without persisting its content anywhere.

### 3.3 Prompt cache (`promptcache.py`)
A per-space exact-match cache keyed by `sha256(logical, system_prompt, user_msg, max_tokens,
reasoning, research, privacy)`. **Privacy is part of the key** — a result produced under
`privacy=off` can never be served to a `strict` request. In-memory LRU (`CACHE_MAX=512`) +
atomic disk mirror, TTL `CACHE_TTL_S=24h`. Only successful calls are cached; `no_store`
skips writes. A hit returns prior output with zero provider calls and transmits nothing.

### 3.4 Durable async jobs (`jobs.py` `JobRunner`, `jobstore.py`)
Free containers recycle on deploy, which previously killed long async runs. `JobStore`
writes each job to `data/jobs/<id>.json` via atomic tmp-rename on every state transition and
mirrors to a private HF Dataset, so jobs survive both crashes and redeploys. On boot the
runner re-hydrates: panel jobs reschedule any unfinished judge (the `_exec` block carries the
prompt/budgets needed to resume), and mid-flight orchestration runs are marked `interrupted`.
`MAX_INFLIGHT_JOBS=200` caps simultaneously-running jobs (the async plane bypasses the HTTP
worker cap); `JOB_TTL_S=6h` prunes finished jobs.

### 3.5 Codebase ingestion (`repopack.py`)
Turns `{path: content}` or a fetched `repo_url` tarball (GitHub codeload, stdlib only,
`REPO_MAX_BYTES=50MB`) into one annotated blob: binaries/lockfiles/`node_modules` filtered,
per-file `===== FILE: =====` delimiters, token estimate chars/3 for code. Each judge gets a
pack **fitted to its own context window** (`ctx` per host in `models_catalog.json`); when a
repo exceeds a judge's budget, low-value files (generated/bulk) are dropped first, tests and
docs last, and the result reports `coverage` so partial-context reviews are honest. A
`diff_mode {base_ref, head_ref}` packs only the changed files plus a unified diff.

### 3.6 Orchestrator & template library (`orchestrator/`, `orchestrator/templates/`)
Beyond one-shot panels, `/api/run` executes multi-stage **schematics**: stages run
sequentially, a stage may **fan out** (one shard per item in a JSON list a prior stage
emitted, shards spread across providers via shared in-flight exclusion), and a **judge loop**
retries the final body up to `max_rounds` if rules/criteria fail. The built-in power
templates — `repo_audit`, `design_doc`, `redteam`, `panel_debate` — each map → fan-out →
synthesize → judge, and bake in hard-won lessons (e.g. `repo_audit` quarantines
confident-but-unverifiable concurrency claims into a "False-positive watch" section).
File-output mode reconstructs a multi-file artifact tree from marker blocks.

### 3.7 Three-layer judge (`judge/`)
Output hardening runs in order and merges reports: **Layer 1** declarative rules
(`engine.py`: min/max words, required sections, banned phrases, regex); **Layer 2** code
plugins (`citation_integrity`, `url_health`, `json_parses`); **Layer 3** LLM-as-judge
natural-language criteria. Hard fails trigger a retry round; soft flags annotate.

### 3.8 Per-profile metrics (`metrics.py`)
Every call emits a metric tagged with a `profile`, so cost/throttle/latency are tracked per
identity, not globally. Rolling per-provider call/token windows (pruned on write, 24h) feed a
**budget guard** that cools a provider when a profile hits its published daily ceiling.
`HOT_WINDOW=5000` events in memory; flushed every `FLUSH_EVERY=25` and mirrored to the HF
Dataset. Outputs are sampled only if `METRICS_SAMPLE_OUTPUTS=1` (off by default), and never
for `no_store` jobs. Exposed via `GET /api/stats`, `/api/metrics?profile=`, `/api/roster`.

## 4. Guarantees

- **≥2 privacy-safe frontier models, always.** `panel.roster()` joins the benchmark catalog
  with live host health and the privacy classification; `/health.frontier_ok` is true iff at
  least two *frontier* logical models have a privacy-safe host routable right now. It reports
  false honestly rather than silently routing to an unsafe or non-frontier host.
- **Privacy by isolation + verifiable routing.** One instance per user with their own keys;
  strict-by-default routing; `no_store` for zero retention. Backpressure responses (503/429)
  carry no prompt content.
- **Durability across crash and redeploy.** Atomic local writes + private HF Dataset mirror;
  reload-on-boot resumes unfinished panel judges.

## 5. Threat model & security

- **Auth.** Every non-health route is bearer-gated with a constant-time compare
  (`hmac.compare_digest`); an unset `CRITIQUE_TOKEN` refuses service (503) rather than
  serving open.
- **Resource exhaustion / DoS.** `BoundedThreadingHTTPServer` caps concurrent sync workers
  (`MAX_WORKERS=48`) and returns 503+`Retry-After` over capacity without spawning a thread;
  `REQUEST_TIMEOUT_S=30` drops slowloris connections; `MAX_INFLIGHT_JOBS=200` caps the async
  plane with 429+`Retry-After`; `MAX_BODY_BYTES=8MB` bounds request size. Per-user instances
  make these per-tenant by construction; the frontend can tune the env knobs per user.
- **Data at rest.** Job snapshots and the cache live on the instance's local disk and the
  user's *private* HF Dataset. `no_store` opts a job out entirely (trading deploy-resume for
  zero retention). Metrics store no prompt/output text unless explicitly enabled.
- **Provider trust.** The privacy router is the control that keeps prompts off
  training/logging hosts; `:free` and unpinned-proxy routes are classified unsafe.
- **Secret hygiene.** Keys are Space secrets, never committed; `data/` is gitignored.

## 6. Evaluation

- **Offline simulation (zero quota).** A `MOCK_MODE` provider yields deterministic,
  fault-injectable responses; nine sims exercise rotation/failover/budget/profile isolation
  (`sim_scenarios`), research ReAct (`sim_research`), crash-resume (`sim_persistence`),
  orchestration + fanout diversity (`sim_orchestrator`), the privacy router (`sim_privacy`),
  the cache incl. the privacy-key (`sim_cache`), ingestion + budget fitting (`sim_repopack`),
  the template library (`sim_templates`), and the concurrency bounds — 503/slowloris/429
  (`sim_server_limits`). All green.
- **Live verification.** Every roster slug is probed (16-token call) before catalog entry
  (`docs/ROSTER-2026-06.md`); failures are recorded and excluded, not shipped broken.
- **Whole-repo reality check.** A 163k-token review of this codebase confirmed strong hosts
  (Kimi, MiniMax) genuinely reason over the full repo, surfaced two real P1 bugs since fixed
  (cache privacy bypass; `privacy_blocked` fallthrough) and several false positives (later
  disproved), and exposed reliability variance (qwen3.5-397b returned empty content → demoted
  from the default panel).

## 7. Limitations & future work

- **`/api/run` does not context-fit the prompt.** The repo-pack per-judge budgeting lives in
  the `/api/panel` `files` form, not the orchestrator path — so a large prompt sent through a
  template hits small-context hosts (e.g. Cloudflare llama-3.3, 24k) with **413** and only
  survives via cross-provider failover to big-context hosts. Fix: have the scheduler skip
  hosts whose `ctx` cannot fit the estimated input, or extend ingestion budgeting to `/api/run`.
- **Per-request event loop on the sync plane.** Each sync request runs `asyncio.run` with a
  fresh `httpx.AsyncClient`, discarding connection pooling; acceptable under the worker cap,
  but a shared loop/client would be more efficient at scale.
- **Benchmarks are indicative.** `benchmarks.json` scores are hand-curated and dated, used
  only to order the roster and define "frontier" — not an authoritative ranking.
- **Provider breadth.** Current pool is four providers; expanding to Groq, Cerebras, Mistral
  La Plateforme, and HF Inference (each with a privacy block + live probe) widens the
  frontier-safety margin (planned).

## 8. Reproducibility

Duplicate the Hugging Face Space, set your own provider keys as Space secrets
(`NVIDIA_API_KEY`, `CF_API_TOKEN`+`CF_ACCOUNT_ID`, `GITHUB_TOKEN`, `OPENROUTER_API_KEY`) and
a `CRITIQUE_TOKEN`; optionally `METRICS_HF_REPO`/`JOBS_HF_REPO` for cross-deploy durability.
The service boots with whatever keys are present and reports its live posture at `/health`
and `/api/roster`. Everything is stdlib + `httpx`; no build step. See `README.md` for the
API surface and `PRIVACY.md` for the full privacy/off-switch matrix.
