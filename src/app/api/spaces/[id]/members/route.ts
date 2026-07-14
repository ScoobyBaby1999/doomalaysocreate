import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import {
  requireUser,
  requireMember,
  ForbiddenError,
  errorResponse,
} from '@/lib/session'

export const dynamic = 'force-dynamic'

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    await requireMember(id, user.id)

    const members = await db.spaceMember.findMany({
      where: { spaceId: id },
      include: {
        user: { select: { id: true, email: true, name: true } },
      },
      orderBy: { joinedAt: 'asc' },
    })

    return NextResponse.json({
      members: members.map((m) => ({
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

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const user = await requireUser()
    const { id } = await params
    const membership = await requireMember(id, user.id)
    if (membership.role !== 'owner' && membership.role !== 'admin') {
      throw new ForbiddenError('only owners or admins can invite members')
    }

    const body = await request.json().catch(() => null)
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'invalid body' }, { status: 400 })
    }
    const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : ''
    const role = typeof body.role === 'string' ? body.role : 'member'
    if (!email) {
      return NextResponse.json({ error: 'email is required' }, { status: 400 })
    }
    if (role !== 'admin' && role !== 'member') {
      return NextResponse.json({ error: 'role must be admin or member' }, { status: 400 })
    }

    const targetUser = await db.user.findUnique({ where: { email } })
    if (!targetUser) {
      return NextResponse.json({ error: 'user not found' }, { status: 404 })
    }

    const existing = await db.spaceMember.findUnique({
      where: { spaceId_userId: { spaceId: id, userId: targetUser.id } },
    })
    if (existing) {
      return NextResponse.json({ error: 'already a member' }, { status: 409 })
    }

    const member = await db.spaceMember.create({
      data: { spaceId: id, userId: targetUser.id, role },
      include: {
        user: { select: { id: true, email: true, name: true } },
      },
    })

    return NextResponse.json(
      {
        member: {
          id: member.id,
          userId: member.userId,
          role: member.role,
          joinedAt: member.joinedAt.toISOString(),
          email: member.user.email,
          name: member.user.name,
        },
      },
      { status: 201 },
    )
  } catch (e) {
    return errorResponse(e)
  }
}
