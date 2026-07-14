import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import {
  requireUser,
  errorResponse,
} from '@/lib/session'
import {
  toSpaceDto,
  uniqueSlug,
  slugify,
  shortId,
} from '@/app/api/_lib/spaces'

export const dynamic = 'force-dynamic'

export async function GET() {
  try {
    const user = await requireUser()
    const memberships = await db.spaceMember.findMany({
      where: { userId: user.id },
      include: {
        space: {
          select: {
            id: true,
            slug: true,
            name: true,
            description: true,
            ownerId: true,
            isPublic: true,
            createdAt: true,
            _count: { select: { members: true, conversations: true } },
          },
        },
      },
      orderBy: { space: { createdAt: 'desc' } },
    })

    const spaces = memberships.map((m) => toSpaceDto(m.space, m.role))
    return NextResponse.json({ spaces })
  } catch (e) {
    return errorResponse(e)
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser()
    const body = await request.json().catch(() => null)
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'invalid body' }, { status: 400 })
    }
    const name = typeof body.name === 'string' ? body.name.trim() : ''
    if (!name) {
      return NextResponse.json({ error: 'name is required' }, { status: 400 })
    }
    const description =
      typeof body.description === 'string' && body.description.trim()
        ? body.description.trim()
        : null
    const isPublic = body.isPublic === true

    const requestedSlug =
      typeof body.slug === 'string' && body.slug.trim()
        ? slugify(body.slug) || `space-${shortId()}`
        : slugify(name) || `space-${shortId()}`
    const slug = await uniqueSlug(requestedSlug)

    const space = await db.space.create({
      data: {
        slug,
        name,
        description,
        ownerId: user.id,
        isPublic,
        members: {
          create: { userId: user.id, role: 'owner' },
        },
      },
      include: { _count: { select: { members: true, conversations: true } } },
    })

    const dto = toSpaceDto(
      {
        id: space.id,
        slug: space.slug,
        name: space.name,
        description: space.description,
        ownerId: space.ownerId,
        isPublic: space.isPublic,
        createdAt: space.createdAt,
        _count: space._count,
      },
      'owner',
    )
    return NextResponse.json({ space: dto }, { status: 201 })
  } catch (e) {
    return errorResponse(e)
  }
}
