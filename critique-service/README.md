# loom critique panel — standalone service

A slim, token-guarded HTTP service that exposes [loom](https://github.com/)'s
**multi-model critique capability**: a panel of judge LLMs reads a *plan* and
returns specific, actionable critiques, which are merged into one consolidated
list. Built to be deployed free on **Hugging Face Spaces (Docker)** and reachable
from anywhere — including the Claude Code mobile app on Android.

> Ported from loom's `backend/` — it reuses loom's provider registry, the
> `SlotScheduler`/`call_slot` rate-limit-aware rotation, and the `critiquer`
> rubric. The dashboard, task queue, and file-writing state machinery are left
> behind. See `../the-loom` history / `HANDOFF.md` for the original design intent.

## Why a panel

Three near-identical models give false confidence. The panel is deliberately
**diverse across model families AND providers** so the critiques are
*uncorrelated*. Default panel (editable in `panel.json`):

Default panel — **one judge per provider, each on its own free channel** (so no
single provider's quota is a bottleneck):

| Judge | Channel | Model ID | Free via |
|---|---|---|---|
| GLM 5.1 | Z.ai | `glm-5.1` | `ZAI_API_KEY` |
| Nemotron Ultra 253B | NVIDIA NIM | `nvidia/llama-3.1-nemotron-ultra-253b-v1` | `NVIDIA_API_KEY` |
| DeepSeek-R1 | OpenRouter | `deepseek/deepseek-r1:free` | `OPENROUTER_API_KEY` |
| Qwen-3-235B | Cerebras | `qwen-3-235b-a22b-instruct-2507` | `CEREBRAS_API_KEY` |

## API

### `GET /health`
Unauthenticated liveness + config summary (no secrets). Used by HF's healthcheck.

### `POST /api/critique`  *(Bearer token required)*

```jsonc
// request
{
  "plan":   "<markdown plan text  OR  loom schematic JSON>",
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
  as a loom schematic (schematic-aware rubric); otherwise markdown.
- **Resilient:** judges fan out in parallel with per-provider pacing; a judge that
  429s/errors returns `ok:false` and is excluded from the merge — it never fails
  the whole request.
- **Consensus:** merged bullets raised by >1 judge are tagged `_(flagged by N judges)_`.

## The judge panel is editable — `panel.json`

`panel.json` is the source of truth for the panel. **Any `provider/model` you name
there is registered on the fly against that provider's API key**, so you can use
models newer than loom's built-in catalog without touching code. If a model slug
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
> (NVIDIA NIM) are newer than loom's registry. Confirm them against each provider's
> model catalog; correct the strings in `panel.json` if needed.

## Run locally

```bash
cd critique-service
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
2. Push the **contents of this `critique-service/` directory** to the Space repo
   (the `Dockerfile` must be at the Space root).
3. In **Space → Settings → Secrets**, add:
   - `CRITIQUE_TOKEN` — your bearer token
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   - the provider keys your panel uses: `ZAI_API_KEY`, `NVIDIA_API_KEY`,
     `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY` (and `GROQ_API_KEY` if you add Groq judges).
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

## Security

- The endpoint is **always token-guarded** — if `CRITIQUE_TOKEN` is unset the
  service refuses `/api/critique` with `503` rather than serving openly.
- **Never commit a real `.env`** (it's gitignored). All keys live in HF Secrets.
- No file writes, no chat endpoint, no unauthenticated routes.

## Files

```
critique-service/
├── critique_service.py   # the HTTP server + endpoint + parallel fan-out
├── providers.py          # ported provider/model registry (gpt-oss excluded)
├── scheduler.py          # ported SlotScheduler / call_slot (pacing, cooldowns)
├── merge.py              # deterministic cross-judge bullet merge + consensus
├── oplog.py              # structured telemetry to stderr (HF Space logs)
├── content/
│   ├── roles.py          # Roles enum + prompt templating + refusal/fence helpers
│   └── prompts/
│       ├── critiquer.md          # markdown-plan rubric (loom's, verbatim)
│       ├── verifier.md           # yes/no LLM-judge rubric (loom's, verbatim)
│       └── schematic_critiquer.md# new: loom-schematic-aware rubric
├── panel.json            # the editable judge panel
├── Dockerfile            # HF Spaces Docker image
├── requirements.txt      # just httpx; everything else is stdlib
└── .env.example          # local-dev key template
```
