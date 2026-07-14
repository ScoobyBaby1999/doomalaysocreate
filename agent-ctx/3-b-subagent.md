# Agent work record — Task 3-b

**Task ID:** 3-b
**Agent:** subagent (backend: provider key vault + roster + metrics)
**Scope:** Only API routes under `src/app/api/` for keys, roster, metrics.

## Files created
- `src/app/api/keys/route.ts` — GET (list user's keys as DTO, never key value) + POST (validate provider, reject `zai`, encrypt key + optional extra, upsert by `[userId, provider, label]`).
- `src/app/api/keys/[id]/route.ts` — DELETE (owner-scoped; 404 for other users' keys).
- `src/app/api/keys/providers/route.ts` — GET (catalog enriched with `hasKey` + `keyLabels`).
- `src/app/api/roster/route.ts` — GET (returns `buildRoster()`, `PROVIDERS`, `DEFAULT_PANEL`, `ROLES`, `EFFORTS`, `MERGES`, `TEMPLATES`).
- `src/app/api/metrics/route.ts` — GET (`?spaceId=` required, optional `?provider=`; `requireMember`; aggregates ALL metrics in space → `totals`, `byProvider`, `recent` last 50).

## Design notes
- `params` awaited (`const { id } = await params`) per Next.js 16.
- All routes use `Response.json(...)` and `errorResponse(e)` for error handling.
- Encrypted key value NEVER leaves the server — only DTOs (`{ id, provider, label, hasKey: true, createdAt }`) are returned.
- Built-in `zai` provider is rejected at POST `/api/keys` with 400 (no user key needed).
- Upsert uses `findFirst` + `update`/`create` because the unique constraint includes `label`.
- Metrics route returns `successRate = ok/calls` (a 0–1 fraction) and `throttle429 = count where code==='429'`.
- DELETE returns 404 (not 403) for keys owned by other users, to avoid leaking existence.
- Lint: my 5 files produce no errors/warnings. (There is a pre-existing lint error in `src/lib/ai.ts` which is outside my scope and I did NOT modify it.)

## Verified
- `bun run lint` — only `src/lib/ai.ts` errors (pre-existing, not mine).
- Dev server still healthy (GET / 200).
