// /api/templates — list built-in templates (+ any custom ones for the user's spaces).
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, errorResponse } from '@/lib/session'
import { TEMPLATES } from '@/lib/providers'

export const runtime = 'nodejs'

export async function GET(_req: NextRequest) {
  try {
    const user = await requireUser()

    // Custom templates for any space the user is a member of.
    const memberships = await db.spaceMember.findMany({
      where: { userId: user.id },
      select: { spaceId: true },
    })
    const spaceIds = memberships.map((m) => m.spaceId)

    const custom =
      spaceIds.length > 0
        ? await db.template.findMany({
            where: { spaceId: { in: spaceIds } },
            orderBy: { createdAt: 'asc' },
          })
        : []

    const customOut = custom.map((t) => {
      let stages: unknown[] = []
      try {
        stages = JSON.parse(t.stages) as unknown[]
      } catch {
        stages = []
      }
      return {
        id: t.id,
        spaceId: t.spaceId,
        name: t.name,
        description: t.description,
        stages,
        builtin: t.builtin,
        createdAt: t.createdAt.toISOString(),
      }
    })

    return Response.json({ templates: TEMPLATES, custom: customOut })
  } catch (e) {
    return errorResponse(e)
  }
}
