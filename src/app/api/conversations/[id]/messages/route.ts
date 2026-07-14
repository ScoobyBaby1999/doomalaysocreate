// /api/conversations/[id]/messages — list messages in a conversation.
// Access: the conversation must belong to the current user, AND the user must
// still be a member of the conversation's space.
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, requireMember, errorResponse } from '@/lib/session'
import type { MessageDTO } from '@/lib/types'

export const runtime = 'nodejs'

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> }
) {
  try {
    const user = await requireUser()
    const { id } = await ctx.params

    const conv = await db.conversation.findUnique({ where: { id } })
    if (!conv) {
      return Response.json({ error: 'conversation not found' }, { status: 404 })
    }
    if (conv.userId !== user.id) {
      return Response.json({ error: 'forbidden' }, { status: 403 })
    }
    await requireMember(conv.spaceId, user.id)

    const rows = await db.message.findMany({
      where: { conversationId: id },
      orderBy: { createdAt: 'asc' },
    })

    const out: MessageDTO[] = rows.map((m) => ({
      id: m.id,
      role: m.role as 'user' | 'assistant' | 'system',
      content: m.content,
      model: m.model,
      provider: m.provider,
      createdAt: m.createdAt.toISOString(),
    }))
    return Response.json({ messages: out })
  } catch (e) {
    return errorResponse(e)
  }
}
