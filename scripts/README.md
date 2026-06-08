# scripts/

## Replay — autonomous research loop

Named after hippocampal replay: the brain's sleep-phase consolidation loop that re-runs a memory trace through cortex until it stabilises. Our version re-runs a markdown draft through three different free LLMs until a validator clears it.

```
draft (DeepSeek V3)  →  validate (local)  →  critique (Llama 3.3 70B)  →  revise (Qwen 2.5 72B)  →  validate  →  …
```

A single free-model pass is shallower than an Opus pass. Stacking three cheap passes plus an epistemic validator closes most of the gap without consuming Anthropic usage.

## Setup

```bash
pip install httpx
cp .env.example .env
# paste OPENROUTER_API_KEY=sk-or-v1-... into .env
```

Get a free key at https://openrouter.ai/keys. No credit card required; daily rate limits apply but the three free models we use cover 11 research papers comfortably over a day.

## Running

```bash
# one pass over all pending prompts
python scripts/conductor.py

# watch mode: pass, sleep, pass — until everything is done or capped-out
python scripts/conductor.py --watch

# one prompt only (good for smoke-testing)
python scripts/conductor.py --only 07_markdown_frontmatter_alternatives

# dry run — no network, just print the plan
python scripts/conductor.py --dry

# check current state without running
python scripts/conductor.py --status
```

`scripts/research_runner.py` still works — it's a thin shim over `conductor.py` with legacy flags tolerated.

## What the validator enforces

The validator is the heart of the loop. It exists because weaker LLMs fail in a specific way: they write with borrowed authority. It refuses to let the pipeline publish confident-sounding bullshit.

**Structural** (hard fails, force a re-revise):
- H1 title, required trailing sections `## Sources & Confidence` and `## What I'm not sure about`
- Word count ≥ 4000
- ≥5 URLs
- No refusal / meta-AI language
- No more than 30% broken URLs (otherwise the model is hallucinating citations)

**Epistemic** (soft flags, fed into the critique stage):
- Confidence boosters without a same-sentence citation ("state-of-the-art", "definitively", "revolutionary", …)
- Numeric claims without attribution in ±2 sentences
- References to years ≥2 years old (prompts the reviser to confirm or mark as outdated)
- Over half of sources T4/T5 (forums, unknown) — asks for stronger sources

Source tiers: T1 peer-reviewed, T2 primary vendor docs, T3 named-author blogs, T4 forum folklore, T5 unknown/broken.

## Files

- `prompts/system_draft.md` — voice, structure, honesty rules; few-shots with the Opus exemplar
- `prompts/system_critique.md` — terse reviewer; groups issues by category
- `prompts/system_revise.md` — applies critique, returns full revised body
- `replay.py` — three-stage loop + frontmatter helper
- `validator.py` — structural + epistemic checks
- `conductor.py` — state file, retry policy, watch loop
- `research_runner.py` — legacy CLI shim

## State

State lives at `vault/research/_state.json`:

```json
{
  "01_transformer_internals": {
    "stem": "01_transformer_internals",
    "status": "done",
    "attempts": 1,
    "rounds": 2,
    "word_count": 5218,
    "last_error": null,
    "last_updated": "2026-04-19T..."
  }
}
```

Safe to kill and restart. `done` entries are skipped. `failed` entries retry up to 3 attempts; `--retry-failed` forces more.

Failed drafts are shelved under `vault/research/_failed/<stem>.attempt<N>.md` so nothing useful is lost.

## Adding a new research topic

Drop a markdown file in `vault/research/_prompts/NN_topic_name.md`. Number prefix controls sort order; filename stem becomes the output filename. The conductor auto-picks it up on the next pass. No code changes needed.

Prompt structure that the validator understands:

- A numbered `1. **Topic name** — ...` list for the TOC
- An **End with ...** directive for the closing section
- An explicit "Include a comparison table" / "decision matrix" directive if wanted

## Rerunning

Delete the output file in `vault/research/<stem>.md` and reset the state entry (or set `status: pending`) to force a re-run. The runner does not auto-overwrite a `done` entry.
