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

## Auto-rotating bearer tokens (recommended)

A static `CRITIQUE_TOKEN` is a standing liability: once it appears in a chat, log, or
screenshot it is compromised forever. To make the wire token self-expire, set a
**`CRITIQUE_ROTATION_SECRET`** instead (`authtoken.py`):

- The root secret **never travels on the wire**. The actual bearer token is
  `HMAC(secret, current_time_window)` and **rotates every `TOKEN_WINDOW_S`** (default 1h),
  TOTP-style. A leaked token stops working once the window (plus a one-window grace for
  clock skew / in-flight requests) passes.
- No HF API writes, no restarts, no coordination — server and client each derive the
  token from the shared secret and the clock.
- Clients fetch the current token with `python tools/gen_token.py` (reads the secret from
  `$CRITIQUE_ROTATION_SECRET`, a file path arg, or `./.rotation_secret` — gitignored).
- `/health` reports `token_rotation.{enabled, window_s, seconds_until_rotation}` — never
  the secret or any token.
- **For maximum safety set ONLY `CRITIQUE_ROTATION_SECRET`** (leave `CRITIQUE_TOKEN`
  unset) so no standing credential exists. Both may be set during a transition; a static
  token, while present, remains a standing liability.

`tools/sim_authtoken.py` proves window/grace math, tamper + stale-window rejection,
end-to-end auth with a derived token, and static back-compat.

## Rotating a secret
1. Generate a new value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`
   for the token; provider keys from each provider's dashboard).
2. Update it in **Space → Settings → Secrets** and restart the Space.
3. The old value stops working immediately. Nothing in the repo references the value, so
   no code change is needed.

**If a `CRITIQUE_TOKEN` is ever shared in chat/logs/screenshots, treat it as compromised
and rotate it.** Provider keys that were only ever Space secrets were never exposed.

## SSRF protection (outbound fetches)

The gateway makes outbound HTTP on a caller's behalf in two places, both guarded by
`ssrfguard.assert_public_url` (resolve the host, reject any private / loopback /
link-local / reserved / multicast / unspecified address — including cloud metadata
`169.254.169.254` and IPv4-mapped IPv6):
- **`web_tools.web_fetch`** (a URL the research agent's model chose) — redirects are
  followed manually with the guard re-checked on every hop (no `follow_redirects`).
- **`repopack.fetch_repo_files`** (`repo_url` tarball) — a redirect-validating opener
  re-checks every hop, so a 302 off `codeload.github.com` can't reach an internal host.

Escape hatch `SSRF_ALLOW_PRIVATE=1` (local dev only). **Residual:** DNS rebinding
(resolve-public-then-connect-private) is not fully closed — that needs pinning the
resolved IP through the connection. The guard blocks the realistic metadata/internal
vectors. Verified by `tools/sim_ssrf.py` (network-free).

## Availability hardening (free multi-user)
See `PRIVACY.md` → "Availability & abuse-resistance": bounded HTTP workers
(`MAX_WORKERS` → 503), request timeout (`REQUEST_TIMEOUT_S`, slowloris), and an async
job cap (`MAX_INFLIGHT_JOBS` → 429). Backpressure responses carry no prompt content.

## Reporting
This is an open-source project; report vulnerabilities via a private channel to the
maintainer before public disclosure.
