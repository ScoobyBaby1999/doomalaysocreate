// /api/conversations/[id] — rename (PATCH) or delete (DELETE) a conversation.
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, errorResponse } from '@/lib/session'

export const runtime = 'nodejs'

export async function PATCH(
  req: NextRequest,
  ctx: { params: Promise<{ id: string }> }
) {
  try {
    const user = await requireUser()
    const { id } = await ctx.params
    const body = await req.json().catch(() => ({}))
    const { title, pinned } = body as { title?: string; pinned?: boolean }

    const conv = await db.conversation.findUnique({ where: { id } })
    if (!conv) return Response.json({ error: 'not found' }, { status: 404 })
    if (conv.userId !== user.id) return Response.json({ error: 'forbidden' }, { status: 403 })

    const data: Record<string, unknown> = {}
    if (typeof title === 'string' && title.trim()) data.title = title.trim().slice(0, 200)
    if (typeof pinned === 'boolean') data.pinned = pinned
    if (Object.keys(data).length === 0) {
      return Response.json({ error: 'nothing to update' }, { status: 400 })
    }

    const updated = await db.conversation.update({ where: { id }, data })
    return Response.json({
      conversation: {
        id: updated.id,
        spaceId: updated.spaceId,
        title: updated.title,
        model: updated.model,
        provider: updated.provider,
        pinned: updated.pinned,
        messageCount: 0,
        createdAt: updated.createdAt.toISOString(),
        updatedAt: updated.updatedAt.toISOString(),
      },
    })
  } catch (e) {
    return errorResponse(e)
  }
}

export async function DELETE(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> }
) {
  try {
    const user = await requireUser()
    const { id } = await ctx.params

    const conv = await db.conversation.findUnique({ where: { id } })
    if (!conv) return Response.json({ error: 'not found' }, { status: 404 })
    if (conv.userId !== user.id) return Response.json({ error: 'forbidden' }, { status: 403 })

    await db.conversation.delete({ where: { id } })
    return Response.json({ ok: true })
  } catch (e) {
    return errorResponse(e)
  }
}
