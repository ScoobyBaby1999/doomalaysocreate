// Shared panel-job kickoff logic, used by both /api/panel and /api/templates/run.
// Lives under src/app/api/_lib (underscore-prefixed = private folder, not a route).

import { db } from '@/lib/db'
import { runJudge, mergeOutputs, type JudgeResult } from '@/lib/ai'
import { DEFAULT_PANEL, EFFORTS } from '@/lib/providers'
import type { Effort, Merge, Role } from '@/lib/types'

export interface KickoffInput {
  spaceId: string
  userId: string
  input: string
  role: Role
  system?: string
  panel?: string[]
  effort?: Effort
  merge?: Merge
  maxTokens?: number
  template?: string
}

export interface KickoffResult {
  jobId: string
  status: string
  judges: { id: string; model: string; status: string }[]
}

/**
 * Create a PanelJob + PanelJudge rows, then fire-and-forget the background
 * judge execution. Returns the freshly-created job (status 'running') and
 * the list of pending judge rows. NEVER awaits the background work.
 */
export async function kickoffPanelJob(opts: KickoffInput): Promise<KickoffResult> {
  const {
    spaceId,
    userId,
    input,
    role,
    system,
    panel,
    effort,
    merge,
    maxTokens,
    template,
  } = opts

  // Resolve the panel: explicit > DEFAULT_PANEL.
  const chosenPanel = panel && panel.length > 0 ? panel.slice() : DEFAULT_PANEL.slice()

  // Resolve effort: cap judges count accordingly.
  const effortId: Effort = effort ?? 'med'
  const effortDef = EFFORTS.find((e) => e.id === effortId) ?? EFFORTS[1]
  const judgeCount = effortDef.judges
  const slicedPanel = chosenPanel.slice(0, Math.max(1, judgeCount))

  const mergeId: Merge = merge ?? 'dedupe'

  const job = await db.panelJob.create({
    data: {
      spaceId,
      userId,
      status: 'running',
      input,
      role,
      system: system ?? null,
      panel: JSON.stringify(slicedPanel),
      effort: effortId,
      merge: mergeId,
      template: template ?? null,
      judges: {
        create: slicedPanel.map((slot) => ({
          model: slot,
          status: 'pending',
        })),
      },
    },
    include: { judges: true },
  })

  // Fire-and-forget the background run. We MUST NOT await this before returning.
  // Promise.allSettled().then(...) keeps it on the same process.
  void runPanelBackground({
    jobId: job.id,
    judges: job.judges.map((j) => ({ id: j.id, model: j.model })),
    userId,
    input,
    role,
    system,
    maxTokens,
    merge: mergeId,
  }).catch(async (e) => {
    const msg = e instanceof Error ? e.message : String(e)
    try {
      await db.panelJob.update({
        where: { id: job.id },
        data: { status: 'error', error: msg },
      })
    } catch {
      /* best-effort */
    }
  })

  return {
    jobId: job.id,
    status: 'running',
    judges: job.judges.map((j) => ({ id: j.id, model: j.model, status: j.status })),
  }
}

async function runPanelBackground(args: {
  jobId: string
  judges: { id: string; model: string }[]
  userId: string
  input: string
  role: Role
  system?: string
  maxTokens?: number
  merge: Merge
}): Promise<void> {
  const { jobId, judges, userId, input, role, system, maxTokens, merge } = args
  const start = Date.now()

  // Mark every judge 'running'.
  try {
    await db.panelJudge.updateMany({
      where: { jobId },
      data: { status: 'running' },
    })
  } catch {
    /* best-effort */
  }

  const results = await Promise.allSettled(
    judges.map(async (judge): Promise<JudgeResult> => {
      const result = await runJudge(userId, judge.model, input, role, system, {
        maxTokens,
      })
      try {
        await db.panelJudge.update({
          where: { id: judge.id },
          data: {
            status: result.ok ? 'done' : 'error',
            output: result.output,
            error: result.error,
            routedTo: result.routedTo,
            ok: result.ok,
          },
        })
      } catch {
        /* best-effort */
      }
      return result
    })
  )

  // Build a stable JudgeResult list even when a judge threw synchronously.
  const settled: JudgeResult[] = results.map((r, i) =>
    r.status === 'fulfilled'
      ? r.value
      : {
          model: judges[i]?.model ?? '?',
          routedTo: judges[i]?.model ?? '?',
          ok: false,
          output: null,
          error: r.reason instanceof Error ? r.reason.message : String(r.reason),
          latencyMs: 0,
        }
  )

  let merged: string
  try {
    merged = mergeOutputs(settled, merge, role)
  } catch (e) {
    merged = `Merge failed: ${e instanceof Error ? e.message : String(e)}`
  }

  const okCount = settled.filter((r) => r.ok).length
  const elapsedS = Math.max(0, Math.round((Date.now() - start) / 1000))

  try {
    await db.panelJob.update({
      where: { id: jobId },
      data: {
        status: 'complete',
        merged,
        meta: JSON.stringify({
          judgesOk: okCount,
          judgesTotal: settled.length,
          judgesSettled: settled.length,
          complete: true,
          elapsedS,
        }),
      },
    })
  } catch {
    /* best-effort */
  }
}
