---
name: critique
description: >-
  Send a plan, design, proposal, or loom JSON schematic to the hosted multi-model
  judge panel (the loom critique service) and return a consolidated, deduped
  critique. Use when the user asks to "critique this plan", "run my judge panel",
  "get a second opinion on this design", "review this schematic", or similar.
  Works from any repo and from the Claude Code mobile app, since it just calls a
  hosted HTTPS endpoint.
---

# Critique a plan with the hosted judge panel

This skill is a **thin client** over the hosted `loom critique panel` service
(`POST /api/critique`). The service fans the plan out to a diverse panel of judge
LLMs (different model families AND providers, so the critiques are uncorrelated),
then merges them into one consolidated bullet list. One judge being rate-limited
or down never fails the request.

## Inputs you need

Two environment variables identify and authorize the endpoint:

- `CRITIQUE_URL`   — base URL of the service, e.g. `https://<user>-<space>.hf.space`
- `CRITIQUE_TOKEN` — the bearer token (an HF Space secret)

If either is missing, tell the user to set them (they're configured once per
environment / Space) and stop — do not invent a URL or token.

## What counts as "the plan"

Whatever the user wants critiqued, as a single string:
- A markdown plan / design / proposal pasted in the conversation, **or**
- the contents of a file the user points at, **or**
- a loom JSON schematic.

You do **not** need to tell the service which kind it is — leave `format` as
`auto` and the service auto-detects schematic vs markdown and applies the matching
rubric. Only set `format` explicitly if the user asks.

## How to call it

Write the plan to a temp file first (avoids shell-quoting/escaping bugs with long
or multi-line plans), then build the JSON body with a tool that escapes properly:

```bash
# $PLAN_FILE holds the raw plan text
jq -Rs --arg fmt auto '{plan: ., format: $fmt}' "$PLAN_FILE" > /tmp/critique_body.json

curl -sS --max-time 300 \
  -X POST "$CRITIQUE_URL/api/critique" \
  -H "Authorization: Bearer $CRITIQUE_TOKEN" \
  -H "Content-Type: application/json" \
  --data @/tmp/critique_body.json
```

Optional body fields:
- `"panel": ["provider/model", ...]` — override the default judges for this one call.
- `"rubric": "<text>"` — an inline rubric override (use the user's exact wording).

If `jq` isn't available, use `python3 -c` with `json.dumps` to build the body —
never hand-concatenate the plan into the JSON string.

## Presenting the result

The response shape is:

```jsonc
{
  "format_detected": "markdown" | "schematic",
  "judges": [ {"model": "...", "ok": true, "critique": "- ..."},
              {"model": "...", "ok": false, "error": "429 ..."} ],
  "merged": "- consolidated bullets (consensus items tagged)",
  "meta": {"elapsed_s": 12.3, "judges_ok": 2, "judges_total": 4}
}
```

Show the user:
1. The **`merged`** critique first — this is the headline deliverable. Bullets
   tagged `_(flagged by N judges)_` are the strongest signal (multiple independent
   judges agreed); call those out.
2. A one-line panel summary from `meta` (e.g. "3 of 4 judges responded").
3. Only if a judge failed (`ok:false`), briefly note which and why (e.g. one was
   rate-limited) — but don't treat a partial panel as an error; that's by design.

## Beyond critique — the general panel

The same hosted service also exposes `POST /api/panel` for *any* role, not just
critique. Use it when the user wants the panel to **verify** a claim, **generate**
alternatives, **transform** text, **parse** to JSON, or run a **custom system
prompt** — same auth, same `panel` override. Body: `{"input": "...", "role":
"verifier|generator|transformer|parser|planner", "instructions": "...", "merge":
"dedupe|vote|concat|none"}` (or pass `"system"` for a fully custom prompt). The
response has the same shape with a `merged` field (verifier→PASS/FAIL vote,
generator→each model's answer). Reach for this to make frontier models do focused
work while you drive.

## Slow frontier panels — use async + poll

The default panel is frontier reasoning models (GLM 5.1, DeepSeek V4 Pro, Kimi) that
can take minutes. For these, **add `"async": true`** to the POST and you get a
`job_id` immediately, then poll:

```bash
JID=$(curl -sS -X POST "$CRITIQUE_URL/api/panel" -H "Authorization: Bearer $CRITIQUE_TOKEN" \
  -H "Content-Type: application/json" --data @body.json | jq -r .job_id)
# poll every ~15s until .meta.complete == true; show partial results meanwhile
curl -sS "$CRITIQUE_URL/api/jobs/$JID" -H "Authorization: Bearer $CRITIQUE_TOKEN" | jq
```

Each judge is independent: report the ones that have `status:"done"` as they land,
note any still `running`, and don't wait on a straggler — present what's finished.

## Caveats to relay when relevant

- The service is hosted on a free Hugging Face Space that **sleeps when idle**, so
  the first request after a nap cold-starts (can take ~30s+). Subsequent calls are
  fast. If the first `curl` times out, retry once.
- If a specific judge consistently returns `not registered` or a 404, its model
  slug in the service's `panel.json` may need fixing — mention this to the user.
