// /api/jobs/[id] — poll a panel job.
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, errorResponse } from '@/lib/session'
import type { Effort, Merge, PanelJobDTO, PanelJudgeDTO, Role } from '@/lib/types'

export const runtime = 'nodejs'

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> }
) {
  try {
    const user = await requireUser()
    const { id } = await ctx.params

    const job = await db.panelJob.findUnique({
      where: { id },
      include: { judges: { orderBy: { createdAt: 'asc' } } },
    })
    if (!job) {
      return Response.json({ error: 'job not found' }, { status: 404 })
    }
    if (job.userId !== user.id) {
      return Response.json({ error: 'forbidden' }, { status: 403 })
    }

    const judges: PanelJudgeDTO[] = job.judges.map((j) => ({
      id: j.id,
      model: j.model,
      status: j.status as PanelJudgeDTO['status'],
      output: j.output,
      error: j.error,
      routedTo: j.routedTo,
      ok: j.ok,
    }))

    let meta: PanelJobDTO['meta'] = null
    if (job.meta) {
      try {
        meta = JSON.parse(job.meta) as PanelJobDTO['meta']
      } catch {
        meta = null
      }
    }

    let panel: string[] = []
    try {
      panel = JSON.parse(job.panel) as string[]
    } catch {
      panel = []
    }

    const dto: PanelJobDTO = {
      id: job.id,
      spaceId: job.spaceId,
      status: job.status as PanelJobDTO['status'],
      role: job.role as Role,
      input: job.input,
      panel,
      effort: job.effort as Effort,
      merge: job.merge as Merge,
      merged: job.merged,
      meta,
      template: job.template,
      error: job.error,
      judges,
      createdAt: job.createdAt.toISOString(),
    }
    return Response.json(dto)
  } catch (e) {
    return errorResponse(e)
  }
}
