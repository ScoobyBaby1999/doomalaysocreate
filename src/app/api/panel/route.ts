// /api/panel — kickoff an async multi-model judge panel job.
// Returns 202 with the job id + pending judges list immediately; the judges
// run fire-and-forget in the background and the client polls /api/jobs/[id].
import { NextRequest } from 'next/server'
import { requireUser, requireMember, errorResponse } from '@/lib/session'
import { kickoffPanelJob } from '@/app/api/_lib/panel'
import type { Effort, Merge, PanelJudgeDTO, Role } from '@/lib/types'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const VALID_ROLES: Role[] = ['critiquer', 'verifier', 'generator', 'transformer', 'parser', 'planner', 'custom']
const VALID_EFFORTS: Effort[] = ['low', 'med', 'high', 'max']
const VALID_MERGES: Merge[] = ['dedupe', 'vote', 'concat', 'none']

export async function POST(req: NextRequest) {
  try {
    const user = await requireUser()
    const body = await req.json().catch(() => ({}))
    const {
      spaceId,
      input,
      role,
      system,
      panel,
      effort,
      merge,
      maxTokens,
      async: _async,
    } = body as {
      spaceId?: string
      input?: string
      role?: Role
      system?: string
      panel?: string[]
      effort?: Effort
      merge?: Merge
      maxTokens?: number
      async?: boolean
    }

    void _async // we are always async; flag accepted for API compatibility

    if (!spaceId || typeof spaceId !== 'string') {
      return Response.json({ error: 'spaceId is required' }, { status: 400 })
    }
    if (typeof input !== 'string' || input.trim().length === 0) {
      return Response.json({ error: 'input is required' }, { status: 400 })
    }
    const roleId: Role = role && VALID_ROLES.includes(role) ? role : 'critiquer'
    const effortId: Effort = effort && VALID_EFFORTS.includes(effort) ? effort : 'med'
    const mergeId: Merge = merge && VALID_MERGES.includes(merge) ? merge : 'dedupe'
    if (panel !== undefined && !Array.isArray(panel)) {
      return Response.json({ error: 'panel must be an array of slot strings' }, { status: 400 })
    }
    if (maxTokens !== undefined && (typeof maxTokens !== 'number' || maxTokens <= 0)) {
      return Response.json({ error: 'maxTokens must be a positive number' }, { status: 400 })
    }

    await requireMember(spaceId, user.id)

    const result = await kickoffPanelJob({
      spaceId,
      userId: user.id,
      input,
      role: roleId,
      system,
      panel: panel as string[] | undefined,
      effort: effortId,
      merge: mergeId,
      maxTokens,
    })

    const judges: PanelJudgeDTO[] = result.judges.map((j) => ({
      id: j.id,
      model: j.model,
      status: j.status as PanelJudgeDTO['status'],
      output: null,
      error: null,
      routedTo: null,
      ok: null,
    }))

    return Response.json(
      { jobId: result.jobId, status: result.status, judges },
      { status: 202 }
    )
  } catch (e) {
    return errorResponse(e)
  }
}
