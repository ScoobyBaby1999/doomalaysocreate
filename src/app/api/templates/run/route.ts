// /api/templates/run — kick off a built-in template as an async panel job.
// Each template maps to a role-appropriate system prompt that embodies the
// template's intent; we then reuse the panel kickoff path.
import { NextRequest } from 'next/server'
import { requireUser, requireMember, errorResponse } from '@/lib/session'
import { kickoffPanelJob } from '@/app/api/_lib/panel'
import type { Effort, Role } from '@/lib/types'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const TEMPLATE_PROMPTS: Record<
  string,
  { role: Role; system: string; merge?: 'dedupe' | 'vote' | 'concat' | 'none' }
> = {
  repo_audit: {
    role: 'critiquer',
    system:
      'You are a principal engineer auditing a repository or system. ' +
      'Identify architectural risks, security holes, code smells, missing tests, and operational gaps. ' +
      'For each finding, output a markdown bullet in the form: ' +
      '`[SEVERITY] area — issue — evidence — fix`, where SEVERITY is CRIT/HIGH/MED/LOW. ' +
      'Be specific and technical; cite the part of the input you are referring to.',
    merge: 'dedupe',
  },
  redteam: {
    role: 'critiquer',
    system:
      'Adversarially attack this plan/system; enumerate failure modes and exploitation paths. ' +
      'Think like a determined, creative attacker. For each issue, output a markdown bullet: ' +
      '`[SEVERITY] attack vector — exploitation step — mitigation`. ' +
      'Cover input manipulation, auth bypass, resource exhaustion, data leaks, and edge cases.',
    merge: 'dedupe',
  },
  design_doc: {
    role: 'generator',
    system:
      'You are a senior engineer drafting a design document. Produce a structured design doc ' +
      'for the requested system with these sections (markdown headings): Overview, Goals & Non-Goals, ' +
      'Architecture, Data Model, API Surface, Failure Modes & Mitigations, Rollout Plan. ' +
      'Be concrete: name components, sketch schemas, list endpoints.',
    merge: 'concat',
  },
  panel_debate: {
    role: 'critiquer',
    system:
      'You are a panelist in a structured debate on the topic below. ' +
      'State your position clearly, give your strongest supporting arguments, ' +
      'then steelman the opposing view, then explain why your position still holds. ' +
      'Use markdown with headings `## Position`, `## Arguments`, `## Steelman`, `## Rebuttal`.',
    merge: 'concat',
  },
}

const VALID_EFFORTS: Effort[] = ['low', 'med', 'high', 'max']

export async function POST(req: NextRequest) {
  try {
    const user = await requireUser()
    const body = await req.json().catch(() => ({}))
    const { spaceId, template, prompt, effort, async: _async } = body as {
      spaceId?: string
      template?: string
      prompt?: string
      effort?: Effort
      async?: boolean
    }

    void _async // we are always async; flag accepted for API compatibility

    if (!spaceId || typeof spaceId !== 'string') {
      return Response.json({ error: 'spaceId is required' }, { status: 400 })
    }
    if (typeof prompt !== 'string' || prompt.trim().length === 0) {
      return Response.json({ error: 'prompt is required' }, { status: 400 })
    }
    if (typeof template !== 'string' || !template.trim()) {
      return Response.json({ error: 'template is required' }, { status: 400 })
    }
    const tpl = TEMPLATE_PROMPTS[template]
    if (!tpl) {
      return Response.json(
        { error: `unknown template: ${template}` },
        { status: 400 }
      )
    }
    const effortId: Effort =
      effort && VALID_EFFORTS.includes(effort) ? effort : 'med'

    await requireMember(spaceId, user.id)

    const result = await kickoffPanelJob({
      spaceId,
      userId: user.id,
      input: prompt,
      role: tpl.role,
      system: tpl.system,
      effort: effortId,
      merge: tpl.merge ?? 'dedupe',
      template,
    })

    return Response.json({ jobId: result.jobId, status: result.status }, { status: 202 })
  } catch (e) {
    return errorResponse(e)
  }
}
