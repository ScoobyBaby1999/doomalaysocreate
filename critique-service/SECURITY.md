# Security

## Secret model

This service holds two kinds of secrets, **both only ever as Hugging Face Space secrets**
(environment variables, encrypted at rest by Hugging Face) — never in the repo:

- **`CRITIQUE_TOKEN`** — the bearer token guarding every API route. The service refuses
  to serve (`503`) if it is unset; there are no unauthenticated endpoints except
  `/health` (which exposes no secrets).
- **Provider API keys** — `NVIDIA_API_KEY`, `CF_API_TOKEN`/`CF_ACCOUNT_ID`,
  `GITHUB_TOKEN`, `OPENROUTER_API_KEY`, optional `HF_TOKEN`, etc. A provider only joins
  rotation when its key is present (`providers.make_provider_registry`).

### How keys are handled
- The **only** use of a provider key is `Authorization: Bearer <key>` sent to that
  provider (`scheduler.py`). Keys are never logged, never returned by any endpoint, and
  never written to disk.
- `/health`, `/api/stats`, `/api/roster`, `/api/metrics` report config/health but **no
  key material** (`/health` shows `token_configured: true/false`, never the value).
- `provider.__repr__` masks `api_key`, so even an accidental `log_event(provider=...)`
  (oplog's JSON fallback reprs unknown objects) cannot leak it.
- `.env` is gitignored (`.env`, `.env.*`, except `.env.example` which holds only empty,
  commented placeholders). A real `.env` is for local dev only and must never be committed.

### Verification
`tools/sim_secrets.py` (MOCK_MODE, zero quota) boots the server with marker-valued keys
and asserts no endpoint response contains them, and that `repr(provider)` is masked. Run
it in CI to keep this invariant.

## Rotating a secret
1. Generate a new value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`
   for the token; provider keys from each provider's dashboard).
2. Update it in **Space → Settings → Secrets** and restart the Space.
3. The old value stops working immediately. Nothing in the repo references the value, so
   no code change is needed.

**If a `CRITIQUE_TOKEN` is ever shared in chat/logs/screenshots, treat it as compromised
and rotate it.** Provider keys that were only ever Space secrets were never exposed.

## Availability hardening (free multi-user)
See `PRIVACY.md` → "Availability & abuse-resistance": bounded HTTP workers
(`MAX_WORKERS` → 503), request timeout (`REQUEST_TIMEOUT_S`, slowloris), and an async
job cap (`MAX_INFLIGHT_JOBS` → 429). Backpressure responses carry no prompt content.

## Reporting
This is an open-source project; report vulnerabilities via a private channel to the
maintainer before public disclosure.
