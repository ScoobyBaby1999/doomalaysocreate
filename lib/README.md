---
title: Doomalaysocreate
emoji: "\U0001F525"
colorFrom: gray
colorTo: green
sdk: docker
pinned: false
license: mit
hf_oauth: true
---

# doomalaysocreate model panel — standalone service

A slim, token-guarded HTTP service that exposes doomalaysocreate's **multi-model panel**: it
fans an input out to a panel of frontier LLMs — each on its own provider — and
merges their outputs. Run it as a **critique panel** (the `/api/critique` preset)
or as a **general panel** (`/api/panel`) for any role: critique, verify, generate,
transform, parse, plan — or a fully custom system prompt. Built to deploy free on
**Hugging Face Spaces (Docker)**, reachable from anywhere including the Claude Code
mobile app on Android.

> Ported from doomalaysocreate's `backend/` and evolved into a **cost-aware routing gateway**:
> it reuses doomalaysocreate's `SlotScheduler`/`call_slot`, now *activated* for live rotation +
> **cross-provider failover**, plus a **per-profile metrics substrate** that records
> real-world cost/throttle/latency so model selection and fan-out staging can be
> tuned over time without burning quota.

## Why a gateway

Top models as on-demand aids: Claude does the heavy lifting, a panel of frontier
models gives critical, *uncorrelated* second opinions. A request asks for a
**logical model** (e.g. `llama-3.3-70b`); the gateway picks the best free host for
it, paces to its rate limit, and **bounces to another provider hosting the same
model** on any 429/5xx/empty — so no single quota is a bottleneck and a throttle on
one host never sinks the request.

**Providers** live in `providers_catalog.json` (core recurring-free pool + opt-in
trial/paid). **Logical models → host candidates** live in `models_catalog.json`.
Both are editable with zero code changes. xAI Grok is excluded entirely.

| Pool | Providers | Key(s) |
|---|---|---|
| core (always rotated) | nvidia, google, cerebras, openrouter, groq, cloudflare, github-models | `NVIDIA_API_KEY`, `GOOGLE_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `CF_API_TOKEN`+`CF_ACCOUNT_ID`, `GITHUB_TOKEN` |
| opt-in (`OPTIN_PROVIDERS=`) | zai, moonshot, fireworks, sambanova | `ZAI_API_KEY`, `MOONSHOT_API_KEY`, … |

A request may still name a physical `provider/model` slot directly (anything with a
`/`) to bypass logical routing — fully backward-compatible with the old `panel.json`.

## API

### `GET /health`
Unauthenticated liveness + config summary (no secrets). Lists configured providers,
the default panel, available roles and merge modes. Used by HF's healthcheck.

### `POST /api/panel`  *(Bearer token required)* — the general endpoint

```jsonc
{
  "input":        "<the text to operate on>",
  "role":         "critiquer|verifier|generator|transformer|parser|planner",  // default critiquer
  "instructions": "<task-specific directive spliced into the role rubric>",    // optional
  "output_rules": "<formatting/constraint rules>",                             // optional
  "system":       "<fully custom system prompt; overrides role if given>",     // optional
  "panel":        ["llama-3.3-70b", "glm-5.1", "provider/model", ...], // logical names OR physical slots; defaults to panel.json
  "merge":        "dedupe|vote|concat|none", // optional; sensible default per role
  "max_tokens":   16384,                      // optional; upper bound (1..131072). GitHub Models clamps to its ~4k ceiling automatically
  "profile":      "default",                  // optional; metrics namespace (a user may have many)
  "effort":       "low|med|high|max"          // optional; manual fan-out width + token/timeout budget
}
```

Response: `{ "role", "merge", "judges": [{model, ok, routed_to, output|error}], "merged", "meta" }`
— `routed_to` is the physical `provider/model` a logical judge actually landed on after failover.

**Profiles & effort.** Every call is tagged with a `profile` (a user can have many);
all metrics are namespaced per profile so cost/throttle behaviour is tracked per
identity, not globally. `effort` is a **manual** knob (no auto-prediction):
`low`→1 judge/½ tokens, `med`→3, `high`→5/1.5×, `max`→all/2×.

**Whole-repo review.** Instead of `input` (or `plan` on `/api/critique`), pass a
codebase and the panel reviews it in one pass — validated live: kimi/glm/nemotron
genuinely reasoned over this repo's full ~110k-token pack:

```jsonc
{
  "files":    {"src/app.py": "<content>", ...},      // client-packed form, OR:
  "repo_url": "https://github.com/owner/repo",       // server fetches the tarball
  "repo_ref": "main",                                 // optional branch/tag/sha
  "repo_token": "ghp_...",                            // optional, private repos (fetch-only, never stored)
  "diff_mode": {"base_ref": "main", "head_ref": "pr-branch"},  // optional: review the delta
  "include_lockfiles": false,
  "role": "critiquer", "panel": ["kimi-k2.6", "glm-5.1"], "async": true
}
```

Binaries/lockfiles/`node_modules` are skipped; each judge's pack is **fitted to its
own context window** (generated/bulk files dropped first, tests+docs kept — they
define intended behavior) and its result carries `"coverage"` saying exactly which
files it saw. Oversized-for-every-budget requests get a clear 400 with the math.
Merge defaults: critiquer→`dedupe` (consolidated bullets, consensus tagged),
verifier→`vote` (PASS/FAIL tally + reasons), generator/transformer/parser/planner→`concat`
(each model's full answer, labelled).

### Async jobs — decoupled judges (recommended for frontier panels)

Frontier reasoning models are slow (DeepSeek V4 Pro can think for minutes). Add
`"async": true` to any `POST /api/critique` or `POST /api/panel` and you get a
**`job_id` back instantly** (HTTP 202). Each judge then runs **completely
independently** on a background loop — one stalling or failing never affects the
others — and you poll for partial results:

```
POST /api/panel  { ..., "async": true }      -> 202 { "job_id": "...", "status": "running", "judges": [pending...] }
GET  /api/jobs/<job_id>   (same bearer token) -> { "status": "running|complete",
                                                   "judges": [{model, status: pending|running|done|error, output|error}],
                                                   "merged": "<merge of whatever has finished so far>",
                                                   "meta": {judges_settled, judges_total, complete} }
```

Poll until `meta.complete` is true (or just read partial results whenever you like).
Per-judge timeout is `JUDGE_TIMEOUT_S` (default 900s); jobs are kept in memory for
6h. This is the decoupled, no-rotation execution model — each model is its own
fire-and-forget task.

### `POST /api/critique`  *(Bearer token required)*

```jsonc
// request
{
  "plan":   "<markdown plan text  OR  doomalaysocreate schematic JSON>",
  "format": "auto" | "markdown" | "schematic",   // default "auto"
  "panel":  ["provider/model", ...],             // optional; defaults to panel.json
  "rubric": "<optional inline rubric override>"  // optional
}
```

```jsonc
// response
{
  "format_detected": "markdown",
  "judges": [
    {"model": "openrouter/z-ai/glm-5.1", "ok": true,  "critique": "- ...\n- ..."},
    {"model": "cerebras/qwen-3-235b...",  "ok": false, "error": "429:HTTP 429 ..."}
  ],
  "merged": "- <deduped, consolidated bullets; consensus items tagged>",
  "meta": {"elapsed_s": 12.3, "judges_ok": 2, "judges_total": 4}
}
```

- **Auto-detect:** a `plan` that parses as JSON with `stages`/`task_type` is treated
  as a doomalaysocreate schematic (schematic-aware rubric); otherwise markdown.
- **Resilient:** judges fan out in parallel with per-provider pacing; a judge that
  429s/errors returns `ok:false` and is excluded from the merge — it never fails
  the whole request.
- **Consensus:** merged bullets raised by >1 judge are tagged `_(flagged by N judges)_`.

## Metrics & telemetry — `GET /api/stats`, `GET /api/metrics`

The point of the gateway is to **accumulate operational data** so routing decisions
improve over time. Every judge call (real or mock) emits one per-profile event:
provider, model, role, effort, latency, in/out tokens, ok/fail code, attempts,
which host it routed to. Two read endpoints (same bearer token):

```
GET /api/stats                  -> live rotation/health: per-provider call counts,
                                   cooling/blacklisted slots, plus the slot snapshot.
GET /api/metrics?profile=<id>   -> per-profile aggregates: by provider (calls, success
                                   rate, throttle_429 rate, avg latency, tokens) and by
                                   model+role. Defaults to profile "default".
```

These answer: *how fast/why does a provider throttle, which model is best at which
role, what does a fan-out actually cost* — the substrate for staging fan-outs
without burning quota. **Persistence:** an HF Space filesystem is ephemeral, so set
`METRICS_PUBLIC_HF_REPO` + `HF_TOKEN` to mirror per-profile JSONL to a **public HF
Dataset** shared across ALL users (loaded on boot, batched/best-effort flush). This
creates a community benchmark dataset. Without them the store runs
in-memory only (still queryable within a session). `set_budget_cooldown` also cools
a provider for a profile once it crosses the catalog's published daily request/token
ceiling.

## Testing with zero quota — `MOCK_MODE`

Set `MOCK_MODE=1` and `scheduler.call_slot` routes to a deterministic synthetic
provider instead of any real endpoint, so the whole rotation/failover/metrics/effort
stack runs with **zero real API calls**. `MOCK_FAULTS="groq=429,google=5xx"` forces
specific failures; per-provider synthetic budgets exhaust into 429s so cooldowns are
observable. The end-to-end scenario suite:

```bash
MOCK_MODE=1 python tools/sim_scenarios.py   # rotation, failover, budget, profile isolation, effort
```

## Orchestrator — templated multi-stage runs with judge + file artifacts

Beyond one-shot panels, the service runs **multi-stage templates** with an
automatic **judge** loop, and can return **multi-file artifacts** (a whole tree of
`.py`/`.md`/`.env`/… files), not just text. This is the "master orchestrator": you
stage a prompt through a template (or a custom schematic), frontier models do the
work across stages with cross-provider rotation, a judge hardens the output, and you
receive each model's **full output as files**.

### Built-in power templates

Each fans work across providers (parallel shards), then a judge loop hardens the result.
Run with `POST /api/run {"template": "<id>", "prompt": "...", "async": true}`:

| Template | What it does |
|---|---|
| **`repo_audit`** | Maps a codebase into subsystems, deep-reviews each in parallel, then **cross-validates** to suppress confident-but-wrong concurrency/race claims (a real LLM-reviewer failure mode). Ships a severity-ranked audit with a "False-positive watch" + "Top 3 to fix first". |
| **`design_doc`** | Extracts requirements, generates **3 genuinely distinct architectures** in parallel, builds a trade-off matrix, commits to a decision, and ships a doc with Risks & Mitigations + Open Questions. |
| **`redteam`** | Enumerates threat/failure surfaces, probes each in parallel for **concrete triggers** (no vague "could be insecure"), and ships a severity-ranked threat report + mitigations checklist. |
| **`panel_debate`** | The uncorrelated-opinions flagship: frames distinct positions, argues each, runs a steelman-then-rebut round, then a moderator synthesis that **preserves irreducible disagreement** instead of forcing false consensus. |

`GET /api/templates` lists all (these + your saved ones); `GET /api/templates/<id>`
returns one's schematic to clone. Templates are plain schematic JSON in
`orchestrator/templates/` — add your own with zero code.

### `POST /api/run`  *(Bearer token required)*

```jsonc
{
  "prompt":    "<the task>",
  "template":  "research_paper",        // a built-in template id (see GET /api/templates)
  "schematic": { ...TaskSchematic... }, // OR a full custom schematic (overrides template)
  "profile":   "default",               // metrics namespace
  "effort":    "low|med|high|max",
  "async":     true                     // recommended; multi-stage runs are slow
}
```

Resolution order: inline `schematic` → built-in `template` → `freeform` fallback.
Returns a job; poll `GET /api/jobs/<id>`. The final `result`:

```jsonc
{
  "ok": true,
  "task": "…",
  "body": "<final stitched markdown>",
  "artifacts": [ {"path": "src/app.py", "content": "...", "truncated": false}, ... ],
  "rounds": 1,                          // judge-driven revise rounds that fired
  "judge": { "ok": true, "hard_fails": [], "soft_flags": [...] },
  "stages": [ {"name","role","provider","ok","duration_s"}, ... ]
}
```

A **template** (`orchestrator/templates/*.json`) declares `stages[]` (each a `role` +
`instructions`, optional `fanout`, `inputs`, `max_tokens`), cross-cutting
`output_rules`, and a `judge_config` (deterministic `rules`, code `plugins`, and
natural-language `llm_judges`) with a `max_rounds` revise loop. Set
`output_rules.format: "files"` and body-producing stages emit multi-file artifacts.

### Templates — built-in *and* your own

- `GET /api/templates` — list built-in (`research_paper`, `lesson_plan`, `freeform`)
  **and** user-authored templates, each tagged `source: builtin|user`.
- `GET /api/templates/<id>` — fetch one template's schematic.
- `POST /api/templates` — `{ "id": "...", "schematic": { ...TaskSchematic... } }` —
  validate and **persist** your own template (mirrored to the HF Dataset so it
  survives restarts; built-in ids are read-only).
- `DELETE /api/templates/<id>` — remove a user template.
- `POST /api/run` with `"template": "auto"` runs the **planner**: it classifies the
  prompt and builds a schematic on the fly (no template needed).

### Multi-file artifact protocol (marker blocks + nonce)

In file mode each file is wrapped in sentinel lines carrying a per-request nonce:

```
===== BEGIN FILE [a7f3c1] path=src/app.py =====
<verbatim file content — may contain ``` or markdown>
===== END FILE [a7f3c1] path=src/app.py =====
```

Parsed by sentinel (nested fences survive); a missing END is recovered + flagged
`truncated`; zero markers falls back losslessly to one `output.md`; paths are
sanitized (no abs / no `..`). **Panel mode** can do this too — add `"artifacts": true`
to `POST /api/panel` and **each model's reply is parsed into its OWN file tree**
(nothing merged), so you keep every model's full output.

### Client — materialize the result as files

`tools/orchestrate_client.py` submits a run, polls, and writes the result into a
workspace (`runs/<ts>-<task>/<path…>` + `_body.md` + `_judge.md` + `_manifest.json`):

```bash
python tools/orchestrate_client.py --template research_paper \
  --input-file prompt.md --profile research --out runs/
# custom schematic that emits a code tree:
python tools/orchestrate_client.py --schematic-file build.json \
  --input "Build a CLI todo app in Python." --out runs/
```

## The judge panel is editable — `panel.json`

`panel.json` is the source of truth for the panel. **Any `provider/model` you name
there is registered on the fly against that provider's API key**, so you can use
models newer than doomalaysocreate's built-in catalog without touching code. If a model slug
is wrong, that one judge simply returns `ok:false` and the request still succeeds —
fix the string and you're done.

```jsonc
{
  "judges": [
    {"slot": "openrouter/z-ai/glm-5.1",                        "weight": 1},
    {"slot": "nvidia/nvidia/llama-3.1-nemotron-ultra-253b-v1", "weight": 1},
    {"slot": "nvidia/deepseek-ai/deepseek-r1",                 "weight": 1},
    {"slot": "cerebras/qwen-3-235b-a22b-instruct-2507",        "weight": 1}
  ],
  "max_parallel": 4,
  "rubric": "critiquer"
}
```

> ⚠️ **Verify two slugs:** `z-ai/glm-5.1` (OpenRouter) and the Nemotron Ultra id
> (NVIDIA NIM) are newer than doomalaysocreate's registry. Confirm them against each provider's
> model catalog; correct the strings in `panel.json` if needed.

## Run locally

```bash
cd lib
cp .env.example .env          # fill in CRITIQUE_TOKEN + the provider keys you use
pip install -r requirements.txt
python critique_service.py    # listens on 0.0.0.0:7860 (override with $PORT)

curl -sS -X POST http://127.0.0.1:7860/api/critique \
  -H "Authorization: Bearer $CRITIQUE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan":"# My plan\n1. ...\n2. ..."}' | jq .
```

## Deploy on Hugging Face Spaces (free)

1. Create a new **Space** → SDK **Docker** → hardware **CPU basic (free)**.
2. Push the **contents of this `lib/` directory** to the Space repo
   (the `Dockerfile` must be at the Space root).
3. In **Space → Settings → Secrets**, add:
   - `CRITIQUE_TOKEN` — your bearer token
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   - the core provider keys you want rotated: `NVIDIA_API_KEY`, `GOOGLE_API_KEY`,
     `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
     `CF_API_TOKEN`+`CF_ACCOUNT_ID`, `GITHUB_TOKEN` (any subset).
    - optionally `METRICS_PUBLIC_HF_REPO` + `HF_TOKEN` to contribute per-profile metrics to the **public community benchmark dataset**,
     and `OPTIN_PROVIDERS` (+ `ZAI_API_KEY`/`MOONSHOT_API_KEY`) for the opt-in pool.
4. The Space builds and serves on port **7860**. Public URL:
   `https://<user>-<space>.hf.space`. Test:

   ```bash
   curl -sS https://<user>-<space>.hf.space/api/critique \
     -H "Authorization: Bearer $CRITIQUE_TOKEN" \
     -d '{"plan":"..."}'
   ```

**Honest caveat:** free CPU is fine (work is I/O-bound on the LLM APIs), but the
Space sleeps when idle (~48h) — the first request after a nap cold-starts for a
bit. Acceptable for occasional plan critiques.

## Invoke from Android (and any repo)

The `.claude/skills/critique/` skill at the repo root is a thin client over this
endpoint. In a Claude Code session (mobile app → cloud session, or desktop), set
the environment's `CRITIQUE_URL` and `CRITIQUE_TOKEN`, then just say *"critique this
plan with my judge panel"* — the skill POSTs the plan here and shows the merged
critique. The phone is a **client**, not the server; the hosted Space is the
reachable backbone.

## Privacy — one space per user, your own keys

Deploy model: **duplicate the Space and add your own provider keys** — nothing is
shared between users, so your prompts/outputs stay in your own instance. On top of
that, a **privacy router** keeps data off providers that train on / log it:

- Per request: `"privacy": "strict" | "fallback" | "off"` (default **strict** — never
  routes to a training/logging host; `fallback` uses one only as a last resort).
- `"no_store": true` — run without persisting the prompt/output anywhere.
- `GET /api/roster` + `frontier_ok` on `/health` show the live posture and the
  **≥2 privacy-safe frontier models** guarantee.

Full per-provider opt-out steps and every off-switch: **[`PRIVACY.md`](PRIVACY.md)**.

The complete system design — architecture, subsystems, guarantees, threat model, and
evaluation — is in **[`docs/DESIGN.md`](docs/DESIGN.md)**.

## Security

- The endpoint is **always token-guarded** — if `CRITIQUE_TOKEN` is unset the
  service refuses `/api/critique` with `503` rather than serving openly.
- **Never commit a real `.env`** (it's gitignored). All keys live in HF Secrets.
- No chat endpoint, no unauthenticated routes. Job/cache state is written only to
  your instance's local disk + your **private** HF Dataset mirror.
- **Bounded for multi-user**: `MAX_WORKERS` (sync request cap → 503+Retry-After),
  `REQUEST_TIMEOUT_S` (slowloris drop), `MAX_INFLIGHT_JOBS` (async cap → 429+Retry-After).
  Backpressure responses carry no prompt content; `/health` reports `workers` + `jobs_inflight`.

## Files

```
lib/
├── critique_service.py   # HTTP server: endpoints, profiles, effort, /api/stats + /api/metrics
├── providers.py          # catalog-driven provider/slot registry (reads the *_catalog.json)
├── providers_catalog.json# provider pool + published free-tier limits (core + opt-in)
├── models_catalog.json   # logical model -> ordered (provider, model) host candidates
├── scheduler.py          # SlotScheduler / call_slot: rotation, failover seam, pacing, cooldowns
├── jobs.py               # route_judge (failover) + sync/async + submit_run (orchestrator)
├── metrics.py            # per-profile metrics capture + aggregation + HF-Dataset persistence
├── artifacts.py          # multi-file artifact protocol (marker blocks + nonce) parse/build
├── orchestrate.py        # template loader + schematic resolution + RunResult serialization
├── orchestrator/         # ported multi-stage engine: schematic, roles, planner, orchestrator
│   ├── templates/*.json  #   built-in templates (research_paper, lesson_plan, freeform)
│   └── prompts/role_*.md #   role skeletons (generator, reviewer, transformer, …)
├── judge/                # plugin judge engine: rules (engine) + plugins + llm_judge
├── mock_provider.py      # MOCK_MODE synthetic provider (role/format-aware; zero real calls)
├── tools/sim_scenarios.py   # quota-free routing scenarios
├── tools/sim_orchestrator.py# quota-free orchestrator + judge + artifact scenarios
├── tools/sim_service.py     # quota-free /api/run wiring + materialization scenarios
├── tools/orchestrate_client.py # run a template, materialize artifacts as files
├── merge.py              # deterministic cross-judge bullet merge + consensus
├── oplog.py              # structured telemetry to stderr (HF Space logs)
├── content/
│   ├── roles.py          # Roles enum + prompt templating + refusal/fence helpers
│   └── prompts/
│       ├── critiquer.md          # markdown-plan rubric (doomalaysocreate's, verbatim)
│       ├── verifier.md           # yes/no LLM-judge rubric (doomalaysocreate's, verbatim)
│       └── schematic_critiquer.md# new: doomalaysocreate-schematic-aware rubric
├── panel.json            # the editable judge panel
├── Dockerfile            # HF Spaces Docker image
├── requirements.txt      # just httpx; everything else is stdlib
└── .env.example          # local-dev key template
```
