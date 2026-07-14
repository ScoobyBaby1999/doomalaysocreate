import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import {
  requireUser,
  requireMember,
  ForbiddenError,
  errorResponse,
} from '@/lib/session'
import { getSpaceDtoForUser } from '@/app/api/_lib/spaces'

export const dynamic = 'force-dynamic'

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    await requireMember(id, user.id)

    const space = await db.space.findUnique({
      where: { id },
      include: {
        members: {
          include: {
            user: { select: { id: true, email: true, name: true } },
          },
          orderBy: { joinedAt: 'asc' },
        },
        _count: { select: { members: true, conversations: true } },
      },
    })
    if (!space) {
      return NextResponse.json({ error: 'space not found' }, { status: 404 })
    }

    const membership = space.members.find((m) => m.userId === user.id)
    return NextResponse.json({
      space: {
        id: space.id,
        slug: space.slug,
        name: space.name,
        description: space.description,
        ownerId: space.ownerId,
        isPublic: space.isPublic,
        role: membership?.role ?? 'member',
        memberCount: space._count.members,
        conversationCount: space._count.conversations,
        createdAt: space.createdAt.toISOString(),
      },
      members: space.members.map((m) => ({
        id: m.id,
        userId: m.userId,
        role: m.role,
        joinedAt: m.joinedAt.toISOString(),
        email: m.user.email,
        name: m.user.name,
      })),
    })
  } catch (e) {
    return errorResponse(e)
  }
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    const membership = await requireMember(id, user.id)
    if (membership.role !== 'owner' && membership.role !== 'admin') {
      throw new ForbiddenError('only owners or admins can update this space')
    }

    const body = await request.json().catch(() => null)
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'invalid body' }, { status: 400 })
    }
    const data: { name?: string; description?: string | null; isPublic?: boolean } = {}
    if (typeof body.name === 'string' && body.name.trim()) {
      data.name = body.name.trim()
    }
    if (typeof body.description === 'string') {
      data.description = body.description.trim() || null
    }
    if (typeof body.isPublic === 'boolean') {
      data.isPublic = body.isPublic
    }
    if (Object.keys(data).length === 0) {
      return NextResponse.json({ error: 'no fields to update' }, { status: 400 })
    }

    await db.space.update({ where: { id }, data })

    const dto = await getSpaceDtoForUser(id, user.id)
    return NextResponse.json({ space: dto })
  } catch (e) {
    return errorResponse(e)
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    const membership = await requireMember(id, user.id)
    if (membership.role !== 'owner') {
      throw new ForbiddenError('only the owner can delete this space')
    }

    await db.space.delete({ where: { id } })
    return NextResponse.json({ ok: true })
  } catch (e) {
    return errorResponse(e)
  }
}
