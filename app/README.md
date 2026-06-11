# loom app — mobile-first agentic-coder client

A thin, mobile-first client for the loom panel gateway (`../critique-service`). TypeScript
+ React + Vite + Tailwind, designed to be wrapped by **Tauri 2.0** for iOS/Android/desktop
(the same web build runs in the WebView). Browser is a first-class dev/test target.

## Architecture (target)

1. **This client** — streams the panel + agent actions, mobile-first, "alive" UI.
2. **Agent + per-user sandbox** *(backend, future F2)* — the coding-agent loop runs in a
   cloud container with the workspace/shell; the app is a thin streaming client (required
   because iOS forbids local shell execution).
3. **Panel model-backend** (`../critique-service`) — router / panel / templates / privacy
   / cache become the brain the agent calls.

## Status — F0 (done)

- Mobile-first shell (full-height, bottom tab bar, safe-area insets).
- Typed client for the gateway (`src/api/panel.ts`) — `/health`, `/api/roster`,
  `/api/panel` (async submit + poll), `/api/jobs/:id`. **Validated live.**
- Chat screen: send a prompt, watch **every frontier judge stream in parallel** (live
  `content_chars`/`reasoning_chars`/tail counters, then full markdown output).
- Settings: gateway URL + bearer token (stored on-device only), Save & test against
  `/health` + `/api/roster`.
- Markdown + code highlighting (`marked` + `highlight.js`).

## Run (web / dev)

```bash
npm install
npm run dev      # http://localhost:5173 ; /backend is proxied to the gateway
```

Set the gateway in `Settings` (or `VITE_BACKEND_ORIGIN` for the dev proxy) and paste a
bearer token (prefer a short-lived one from `critique-service/tools/gen_token.py`).

```bash
npm run build    # type-check + production build (dist/)
```

## Next

- **F0.5** backend SSE endpoint for true token streaming (today the client polls the job
  snapshot, which already carries per-judge progress).
- **F1** richer chat (merged view, templates, `/api/run` orchestrator, artifacts).
- **F2** agent + per-user sandbox + tool-action streaming.
- Tauri wrap (`src-tauri/`) for mobile/desktop; lazy-load highlight.js languages.
