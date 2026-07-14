# Task 3-a — auth + spaces + members + duplicate API

Agent: full-stack-developer (auth+spaces backend)
Scope: All API route handlers under `src/app/api/` for auth + spaces + members + duplicate.

## What I built

10 route handler files + 1 shared helper, all under `src/app/api/`:

```
src/app/api/
  _lib/spaces.ts                                  # SpaceDto + slug helpers (shared)
  health/route.ts                                  GET  (no auth, liveness)
  auth/signup/route.ts                             POST (201 on success, 409 on dup)
  auth/signin/route.ts                             POST (200 ok, 401 invalid creds)
  auth/signout/route.ts                            POST (clears cookie + deletes session row)
  auth/me/route.ts                                 GET  (200 or 401)
  spaces/route.ts                                  GET (list my memberships) + POST (create + auto-owner)
  spaces/[id]/route.ts                             GET (detail+members) + PATCH (owner|admin) + DELETE (owner)
  spaces/[id]/duplicate/route.ts                   POST (config-only clone, "(copy)" suffix, fresh env)
  spaces/[id]/members/route.ts                     GET (list) + POST (invite by email)
  spaces/[id]/members/[userId]/route.ts            DELETE (remove; protect owner + last-privileged)
```

## Key decisions

- **Next.js 16 async params**: every dynamic route awaits `params` (`const { id } = await params`).
- **Slug**: server-generated from name if absent; collisions resolved by appending random hex suffix (bounded retries).
- **Duplicate space** = config-only clone: description + isPublic are copied; conversations, panel jobs, and members are NOT. The calling user becomes owner of the new space. Matches the spec note ("a duplicate is a fresh environment with the same config").
- **Role enforcement layered**:
  - Read access → `requireMember`
  - Write/invite/remove → `owner | admin`
  - Delete space → `owner` only
  - Space owner can never be removed (400).
  - Removing self when self is the last privileged member → blocked with 400.
- **No secrets leaked by /health**: only `PROVIDERS.length` + `ROLES.map(r => r.id)`.
- All errors routed through `errorResponse(e)` from session.ts (so `UnauthorizedError`→401, `ForbiddenError`→403, default 500).

## Validation

- `bunx eslint src/app/api` → 0 errors / 0 warnings.
- `bunx tsc --noEmit` → 0 errors in `src/app/api/**` (remaining errors are in `src/lib/ai.ts` and `examples/`+`skills/` which are other agents' scope).
- End-to-end smoke test via curl with a cookie jar (signup→me→create-space→list→duplicate→detail→members→invite-ghost→patch→signout→unauth). All status codes + payloads correct. Test data removed.

## Files built on top of (NOT modified)

- `src/lib/db.ts` — Prisma client `db`
- `src/lib/session.ts` — `getUser`, `requireUser`, `requireMember`, `createSession`, `setSessionCookie`, `clearSessionCookie`, `errorResponse`, `UnauthorizedError`, `ForbiddenError`, `SESSION_COOKIE`
- `src/lib/password.ts` — `hashPassword`, `verifyPassword`
- `prisma/schema.prisma` — User/Session/Space/SpaceMember models
- `src/lib/providers.ts` — `PROVIDERS`, `ROLES`

## Handoff notes for downstream agents

- 3-b (keys/roster/metrics) and 3-c (chat/panel/jobs/templates) should reuse the same pattern: `requireUser()` for user-scoped, `requireMember(spaceId, user.id)` for space-scoped, `errorResponse(e)` for catch-all.
- The `SpaceDto` shape (id/slug/name/description/ownerId/isPublic/role/memberCount/conversationCount/createdAt ISO string) is the contract the frontend should expect from `GET/POST /api/spaces`, `POST /api/spaces/[id]/duplicate`, `PATCH /api/spaces/[id]`.
- All date fields are returned as ISO strings (`.toISOString()`) — frontend should parse with `new Date()`.
