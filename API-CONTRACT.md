# API contract — loom panel gateway

This frontend talks to the backend **only** over HTTP. The backend lives on its own
branch (`c`) of the same repo, deployed as a Hugging Face Space. Point the app at it in
**Settings** (`baseUrl` + `token`). Typed mirror: `src/api/panel.ts`.

## Auth
Every route except `/health` requires `Authorization: Bearer <token>`, where `<token>` is
the `CRITIQUE_TOKEN` (or a short-lived token derived from `CRITIQUE_ROTATION_SECRET`).

## Endpoints the app uses

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + config (no secrets): `frontier_ok`, `workers`, `jobs_inflight`, `token_rotation` |
| GET | `/api/roster` | per-model benchmark + hosts + `privacy_safe_routable` + the ≥2-frontier guarantee |
| POST | `/api/panel` | run the panel for any role / custom system prompt; `"async": true` → returns `{job_id}` |
| POST | `/api/run` | run a built-in template (`repo_audit`, `design_doc`, `redteam`, `panel_debate`) or a custom schematic |
| GET | `/api/jobs/:id` | poll an async job; `?trace=true` adds the per-hop routing chain |

## Request (POST /api/panel)
```jsonc
{
  "input": "<text>",            // OR "files": {path: content} for whole-repo review
  "role": "critiquer|verifier|generator|transformer|parser|planner",
  "system": "<custom system prompt, overrides role>",
  "panel": ["kimi-k2.6", "glm-5.1", "..."],   // optional; defaults to panel.json
  "effort": "low|med|high|max",
  "privacy": "strict|fallback|off",            // default strict
  "reasoning": false, "research": false,
  "max_tokens": 16000,
  "async": true                                // recommended; frontier panels are slow
}
```

## Snapshot (GET /api/jobs/:id)
```jsonc
{
  "job_id": "...", "status": "running|complete",
  "judges": [{ "model", "status", "ok", "routed_to", "output|critique", "error",
               "progress": { "content_chars", "reasoning_chars", "tail" },   // live streaming
               "coverage": { "files_included", "files_total", "dropped" },   // repo review
               "cached": false }],
  "merged": "<consolidated result>",
  "meta": { "judges_ok", "judges_total", "judges_settled", "complete", "age_s" },
  "pack": { "files", "skipped", "total_tokens" }   // present for repo-review requests
}
```
Poll every ~2s while `status == "running"`; render each judge's `progress` for the
"watch it think" stream. A partial panel (some judges `error`) is normal, not a failure.

Full usage (panel, templates, repo-context budgeting): see the backend's
`critique-service/docs/USAGE.md`.
