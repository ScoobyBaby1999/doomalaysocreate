import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import {
  requireUser,
  requireMember,
  ForbiddenError,
  errorResponse,
} from '@/lib/session'

export const dynamic = 'force-dynamic'

export async function DELETE(
  _request: Request,
  {
    params,
  }: {
    params: Promise<{ id: string; userId: string }>
  },
) {
  try {
    const user = await requireUser()
    const { id, userId: targetUserId } = await params
    const membership = await requireMember(id, user.id)

    // Only owners or admins can remove members.
    if (membership.role !== 'owner' && membership.role !== 'admin') {
      throw new ForbiddenError('only owners or admins can remove members')
    }

    const target = await db.spaceMember.findUnique({
      where: { spaceId_userId: { spaceId: id, userId: targetUserId } },
    })
    if (!target) {
      return NextResponse.json({ error: 'member not found' }, { status: 404 })
    }

    // Cannot remove the space owner.
    if (target.role === 'owner') {
      return NextResponse.json(
        { error: 'cannot remove the space owner' },
        { status: 400 },
      )
    }

    // If an admin is removing themselves and they are the last admin/owner,
    // we block to avoid leaving the space with no privileged member.
    if (target.userId === user.id && membership.role === 'admin') {
      const privileged = await db.spaceMember.count({
        where: {
          spaceId: id,
          role: { in: ['owner', 'admin'] },
        },
      })
      if (privileged <= 1) {
        return NextResponse.json(
          { error: 'cannot remove the last privileged member' },
          { status: 400 },
        )
      }
    }

    await db.spaceMember.delete({
      where: { spaceId_userId: { spaceId: id, userId: targetUserId } },
    })
    return NextResponse.json({ ok: true })
  } catch (e) {
    return errorResponse(e)
  }
}
