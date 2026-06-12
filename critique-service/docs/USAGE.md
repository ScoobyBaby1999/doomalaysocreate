# Using the judge panel — practical guide

How to actually drive the gateway: the panel, the templates, and how to budget realistically
when you feed it a whole repo. All examples assume:

```bash
URL=https://<your-space>.hf.space        # your deployed Space
TOK=<your CRITIQUE_TOKEN>                # bearer token (see SECURITY.md to create/rotate)
```

Every route except `/health` needs `-H "Authorization: Bearer $TOK"`. **Frontier judges are
slow** — prefer `"async": true` and poll `/api/jobs/:id`.

---

## 1. The panel — `POST /api/panel`

One input → fanned out to a diverse panel (different model families AND providers, so the
opinions are uncorrelated) → merged. One judge being down never fails the request.

```bash
# async (recommended): submit, get a job_id, poll
JID=$(curl -sS -X POST "$URL/api/panel" -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{
    "input": "Review this rollout plan: <paste>",
    "role": "critiquer",
    "effort": "high",
    "privacy": "strict",
    "async": true
  }' | jq -r .job_id)

# poll every ~2s until meta.complete; show each judge as it lands
curl -sS "$URL/api/jobs/$JID" -H "Authorization: Bearer $TOK" | jq '.merged, .meta'
```

**Roles** (`role`): `critiquer` (default), `verifier` (PASS/FAIL vote), `generator`
(alternatives), `transformer` (rewrite), `parser`/`planner` (to JSON). Or pass `"system"`
for a fully custom prompt (overrides `role`).

**Knobs that matter:**
- `effort`: `low`=1 judge/½ tokens · `med`=3 · `high`=5/1.5× · `max`=all/2×. Controls
  fan-out width, per-judge token budget, and timeout.
- `privacy`: `strict` (default — never routes to a training/logging host), `fallback`
  (safe first, unsafe only if needed), `off`. See PRIVACY.md.
- `panel`: `["kimi-k2.6","glm-5.1",...]` to override the default judges. Names come from
  `GET /api/roster` (logical models) or physical `provider/model` slots.
- `reasoning: true` deep-thinks; `research: true` adds a web ReAct loop (needs egress).
- `merge`: `dedupe` (consensus-tagged) · `vote` · `concat` · `none`.

**Reading the result:** lead with `merged`. In dedupe mode, bullets tagged
`(flagged by N judges)` are the strongest signal — independent judges agreeing. A partial
panel (some judges `error`/429) is by design, not a failure.

---

## 2. Whole-repo review — the `files` form

Instead of `input`, send the codebase and the panel reviews it in one pass. Each judge gets
the pack **fitted to its own context window**, and reports `coverage` (what it actually saw).

```bash
# build {path: content} for your source files, then:
curl -sS -X POST "$URL/api/panel" -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d @- <<JSON
{ "files": $(python3 -c "import json,glob;print(json.dumps({f:open(f).read() for f in glob.glob('**/*.py',recursive=True)}))"),
  "system": "Principal-engineer review. [SEVERITY] path:symbol - issue - evidence - fix.",
  "panel": ["kimi-k2.6","deepseek-v4-flash","glm-5.1"],
  "effort": "high", "max_tokens": 16000, "privacy": "strict", "async": true }
JSON
```
- `repo_url` (+ `repo_ref`, `repo_token`, `diff_mode`) instead of `files` makes the server
  fetch a GitHub tarball itself.
- Binaries/lockfiles/`node_modules` are skipped; the response includes `pack` (files,
  total_tokens) and each judge's `coverage`.

---

## 3. Budgeting realistically with full repo context

Token math the server uses, so you can size requests instead of guessing:

- **Estimate:** ~`chars / 3` tokens for code, `chars / 4` for prose. A ~500KB source tree
  ≈ **160–185k tokens**.
- **Per-judge fit:** a judge can hold `ctx − system_overhead(~2k) − max_tokens(output)`.
  Context windows in this roster: **262k** (kimi, deepseek-v4-flash, qwen3.5, nemotron-120b,
  minimax), **131k** (glm, nemotron-ultra, mistral, gemma, step), and small **8k–24k**
  (github/cloudflare llama — these get a *reduced* pack or are skipped).
- **Practical rules:**
  - A ~180k-token repo fits **whole** only on the 262k hosts. Put those in `panel`
    (`kimi-k2.6`, `deepseek-v4-flash`, `qwen3.5-397b`) for full-coverage review.
  - Always set `max_tokens` to the output you actually want (8k–16k for a deep audit) —
    it's subtracted from the input budget. Don't leave it tiny "to be safe"; a thorough
    review needs room. Async + the long judge timeout make big generations free.
  - If a judge's window is too small, the `files` form **auto-reduces** its pack (drops
    generated/bulk files first, keeps tests+docs) and tells you in `coverage.dropped`.
    Trust judges with high `files_included/files_total`; treat low-coverage judges as
    partial.
  - For `/api/run` templates (below), the orchestrator now **routes each stage to a host
    big enough** for its prompt, or fails fast with the budget math — so a whole-repo
    `repo_audit` lands on 262k hosts automatically.
- **Rule of thumb:** repo ≤ ~240k tokens → single-pass on 262k judges. Bigger → split by
  subsystem and review in parts (or use `repo_audit`, which maps then fans out per area).

---

## 4. Templates — `POST /api/run`

Multi-stage pipelines with an automatic judge loop. List them: `GET /api/templates`.

```bash
JID=$(curl -sS -X POST "$URL/api/run" -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{
    "template": "design_doc",
    "prompt": "Design a rate limiter for a public free API.",
    "effort": "high", "async": true }' | jq -r .job_id)
curl -sS "$URL/api/jobs/$JID" -H "Authorization: Bearer $TOK" | jq '.result.body, .result.judge'
```

Built-ins:
- **`repo_audit`** — maps subsystems → parallel deep-review → cross-validates (suppresses
  confident-but-wrong concurrency claims) → severity-ranked audit. Pass the codebase as
  `prompt` (or a repopack blob). Routes to big-ctx hosts automatically.
- **`design_doc`** — requirements → 3 distinct architectures in parallel → trade-off →
  decision → doc with Risks & Open Questions.
- **`redteam`** — threat surfaces → parallel concrete-trigger probes → ranked report +
  mitigations checklist.
- **`panel_debate`** — distinct positions → argue → steelman+rebut → synthesis that
  preserves irreducible disagreement.

The run polls like any job; `result.body` is the final document, `result.judge` the verdict,
`result.stages` shows which host ran each stage. Custom pipelines: send a `"schematic"`
object instead of `"template"`.

---

## 5. Quick reference

- See live capacity + the ≥2-frontier guarantee: `GET /api/roster`, `GET /health`.
- Per-profile cost/latency/throttle: `GET /api/metrics?profile=<id>`.
- Privacy modes + every off-switch: `PRIVACY.md`. Auth/token setup: `SECURITY.md`.
- The `/critique` Claude Code skill wraps all of this — see `.claude/skills/critique/`.
