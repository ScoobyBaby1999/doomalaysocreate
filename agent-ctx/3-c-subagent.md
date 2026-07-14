# Task ID 3-c — chat / panel / jobs / templates backend

Agent: subagent (Task 3-c)
Scope: ONLY files under `src/app/api/` for chat, panel, jobs, templates, conversations.

## Files created
- `src/app/api/_lib/panel.ts` — shared panel-kickoff helper (private folder, not a route).
  Creates a `PanelJob` + `PanelJudge` rows, then fire-and-forgets `Promise.allSettled`
  of `runJudge(...)` calls. Updates each judge row, then writes `merged` + `meta` and
  flips status to `complete` (or `error` on catastrophic failure). Used by both
  `/api/panel` and `/api/templates/run`.
- `src/app/api/conversations/route.ts` — GET (list, `?spaceId=`) + POST (create).
  Maps rows to `ConversationDTO` (incl. `messageCount` via `_count`).
- `src/app/api/conversations/[id]/messages/route.ts` — GET messages (asc by createdAt).
  Enforces both `userId` ownership and current space membership.
- `src/app/api/chat/route.ts` — POST streaming SSE chatbot. Resolves the user's OWN
  provider key via `resolveUserKey`. Builds a `ReadableStream` + `Response` manually
  with `text/event-stream` headers. Persists the user's last message + a new
  `assistant` Message on stream completion. Records a `Metric`. Uses a `pendingWork`
  promise so the `done` event is always flushed before the controller closes.
- `src/app/api/panel/route.ts` — POST kickoff. Validates role/effort/merge enums.
  Returns 202 `{ jobId, status: 'running', judges: [...] }`.
- `src/app/api/jobs/[id]/route.ts` — GET poll. Returns full `PanelJobDTO` with
  judges + parsed `meta`/`panel` JSON. Enforces `userId` ownership.
- `src/app/api/templates/route.ts` — GET lists built-in `TEMPLATES` + custom
  `Template` rows for spaces the user is a member of.
- `src/app/api/templates/run/route.ts` — POST `{spaceId, template, prompt, effort?}`
  → maps template → role-appropriate system prompt (repo_audit/redteam→critiquer,
  design_doc/panel_debate→generator/critiquer) → `kickoffPanelJob(..., { template })`.

## Key implementation notes
- All Next.js 16 route handlers use the new async `params` signature:
  `ctx: { params: Promise<{ id: string }> }` → `const { id } = await ctx.params`.
- Streaming route uses `new ReadableStream({ start(controller) { ... } })` + manual
  `Response` (NOT `NextResponse.json`), per spec.
- SSE format: `data: ${JSON.stringify({type:'delta',content})}\n\n` for deltas,
  `data: ${JSON.stringify({type:'done',conversationId,messageId})}\n\n` on completion,
  `data: ${JSON.stringify({type:'error',error})}\n\n` on failure.
- The background panel work is launched via `void runPanelBackground(...).catch(...)`
  — fire-and-forget on the same process; the 202 response returns immediately.
- `runtime = 'nodejs'` on every route (Prisma + Node streams); streaming/panel
  routes also set `dynamic = 'force-dynamic'`.

## Lint / type status
- `bun run lint`: ONLY 1 error remains, in `src/lib/ai.ts` line 315
  (`@typescript-eslint/no-require-imports` on a `require('./providers')` call inside
  `parseSlot`). This is a **pre-existing Task-1 foundation issue**, NOT in my files.
  Per task rules I did not modify `ai.ts` — flagged here for the integrator.
- `bunx tsc --noEmit`: my files (`src/app/api/**`) produce ZERO TypeScript errors.
  The remaining tsc errors are all in `examples/`, `skills/`, and `src/lib/ai.ts`
  (pre-existing foundation issues: unused `@ts-expect-error` directives and a
  `Record<string, unknown>` → `CreateChatCompletionBody` mismatch on the z-ai SDK).

## No genuine bugs found in ai.ts
The `streamChat` / `runJudge` / `mergeOutputs` / `recordMetricFor` signatures
described in the task spec match the actual exports. I used them as-is.
