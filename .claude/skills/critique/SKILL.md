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

Environment variables identify and authorize the endpoint:

- `CRITIQUE_URL`   — base URL of the service, e.g. `https://<user>-<space>.hf.space`
- `CRITIQUE_ROTATION_SECRET` — **preferred.** A long-lived root secret that NEVER
  travels on the wire; you derive the current bearer token from it on every call
  (TOTP-style — see "How to call it"). A leaked wire token self-expires once its
  time window passes, so this is the safe default for shared/exposed use.
- `CRITIQUE_TOKEN` — a static bearer token. Still supported as a fallback when no
  rotation secret is set.

Resolution order: if `CRITIQUE_ROTATION_SECRET` is set, derive a fresh token from it
each call; otherwise use the static `CRITIQUE_TOKEN`. You need `CRITIQUE_URL` plus at
least one of the two. If `CRITIQUE_URL` is missing, or neither token source is set,
tell the user to configure them (once per environment / Space) and stop — do not
invent a URL, token, or secret.

## Installing this skill so `/critique` works in ANY chat

This skill is **project-scoped** (it lives in this repo's `.claude/skills/`), so by
default `/critique` only appears in sessions opened inside this repo. To use it from a
fresh chat anywhere — another repo, the desktop app, or mobile — install it at **user
scope** once:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/critique ~/.claude/skills/critique
```

Then set the env vars in that environment (shell profile, or the Claude Code
environment settings used by web/mobile). Prefer the rotation secret:

```bash
export CRITIQUE_URL=https://<your-space>.hf.space
export CRITIQUE_ROTATION_SECRET=<your CRITIQUE_ROTATION_SECRET>   # preferred
# export CRITIQUE_TOKEN=<your CRITIQUE_TOKEN>                     # static fallback
```

Now `/critique` (and "critique this plan" / "run my judge panel") work in every session.
Nothing else from this repo is needed — the skill is a self-contained HTTPS client.

For the full panel/template/repo-budgeting playbook, see the backend's
`lib/docs/USAGE.md`.

## What counts as "the plan"

Whatever the user wants critiqued, as a single string:
- A markdown plan / design / proposal pasted in the conversation, **or**
- the contents of a file the user points at, **or**
- a loom JSON schematic.

You do **not** need to tell the service which kind it is — leave `format` as
`auto` and the service auto-detects schematic vs markdown and applies the matching
rubric. Only set `format` explicitly if the user asks.

## How to call it

**First, resolve the bearer token.** If `CRITIQUE_ROTATION_SECRET` is set, derive the
current wire token from it (it rotates every `TOKEN_WINDOW_S`, default 1h; the server
also accepts the previous window, so clock skew is fine). Otherwise fall back to the
static `CRITIQUE_TOKEN`. Recompute this at the start of every call — never cache or
echo the token, and never print the secret:

```bash
if [ -n "$CRITIQUE_ROTATION_SECRET" ]; then
  # TOTP-style derivation — must match the server's authtoken.py exactly.
  TOK=$(python3 - <<'PY'
import base64, hmac, hashlib, os, time
secret = os.environ["CRITIQUE_ROTATION_SECRET"].encode("utf-8")
window = int(time.time() // int(os.environ.get("TOKEN_WINDOW_S", "3600")))
mac = hmac.new(secret, str(window).encode("ascii"), hashlib.sha256).digest()
print(base64.urlsafe_b64encode(mac[:18]).decode("ascii"))
PY
)
else
  TOK="$CRITIQUE_TOKEN"
fi
# If this repo is checked out you can equivalently run:
#   TOK=$(CRITIQUE_ROTATION_SECRET=$CRITIQUE_ROTATION_SECRET python lib/tools/gen_token.py)
```

Then write the plan to a temp file first (avoids shell-quoting/escaping bugs with long
or multi-line plans), and build the JSON body with a tool that escapes properly:

```bash
# $PLAN_FILE holds the raw plan text
jq -Rs --arg fmt auto '{plan: ., format: $fmt}' "$PLAN_FILE" > /tmp/critique_body.json

curl -sS --max-time 300 \
  -X POST "$CRITIQUE_URL/api/critique" \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  --data @/tmp/critique_body.json
```

All other routes below take the same `-H "Authorization: Bearer $TOK"`; reuse the `$TOK`
you derived above (re-derive if a call returns 401 after a long gap — the window rolled).

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
JID=$(curl -sS -X POST "$CRITIQUE_URL/api/panel" -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" --data @body.json | jq -r .job_id)
# poll every ~15s until .meta.complete == true; show partial results meanwhile
curl -sS "$CRITIQUE_URL/api/jobs/$JID" -H "Authorization: Bearer $TOK" | jq
```

Each judge is independent: report the ones that have `status:"done"` as they land,
note any still `running`, and don't wait on a straggler — present what's finished.

## Caveats to relay when relevant

- The service is hosted on a free Hugging Face Space that **sleeps when idle**, so
  the first request after a nap cold-starts (can take ~30s+). Subsequent calls are
  fast. If the first `curl` times out, retry once.
- If a specific judge consistently returns `not registered` or a 404, its model
  slug in the service's `panel.json` may need fixing — mention this to the user.
