import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import {
  requireUser,
  requireMember,
  errorResponse,
} from '@/lib/session'
import { uniqueSlug, getSpaceDtoForUser } from '@/app/api/_lib/spaces'

export const dynamic = 'force-dynamic'

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    await requireMember(id, user.id)

    const original = await db.space.findUnique({ where: { id } })
    if (!original) {
      return NextResponse.json({ error: 'space not found' }, { status: 404 })
    }

    const body = await request.json().catch(() => ({}))
    const obj = (body && typeof body === 'object' ? body : {}) as {
      name?: string
      slug?: string
    }
    const name =
      typeof obj.name === 'string' && obj.name.trim()
        ? obj.name.trim()
        : `${original.name} (copy)`
    const requestedSlug =
      typeof obj.slug === 'string' && obj.slug.trim() ? obj.slug.trim() : name
    const slug = await uniqueSlug(requestedSlug)

    // Create a new space owned by the current user, copying the config only.
    // Conversations / panel jobs / members are NOT copied — a duplicate is a
    // fresh environment with the same description + visibility.
    const space = await db.space.create({
      data: {
        slug,
        name,
        description: original.description,
        ownerId: user.id,
        isPublic: original.isPublic,
        members: {
          create: { userId: user.id, role: 'owner' },
        },
      },
      include: { _count: { select: { members: true, conversations: true } } },
    })

    const dto = await getSpaceDtoForUser(space.id, user.id)
    return NextResponse.json({ space: dto }, { status: 201 })
  } catch (e) {
    return errorResponse(e)
  }
}
