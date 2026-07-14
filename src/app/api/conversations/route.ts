// /api/conversations — list & create conversations for the current user in a space.
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, requireMember, errorResponse } from '@/lib/session'
import type { ConversationDTO } from '@/lib/types'

export const runtime = 'nodejs'

export async function GET(req: NextRequest) {
  try {
    const user = await requireUser()
    const spaceId = req.nextUrl.searchParams.get('spaceId')
    if (!spaceId) {
      return Response.json({ error: 'spaceId query parameter is required' }, { status: 400 })
    }
    await requireMember(spaceId, user.id)

    const rows = await db.conversation.findMany({
      where: { spaceId, userId: user.id },
      include: { _count: { select: { messages: true } } },
      orderBy: { updatedAt: 'desc' },
    })

    const out: ConversationDTO[] = rows.map((r) => ({
      id: r.id,
      spaceId: r.spaceId,
      title: r.title,
      model: r.model,
      provider: r.provider,
      pinned: r.pinned,
      messageCount: r._count.messages,
      createdAt: r.createdAt.toISOString(),
      updatedAt: r.updatedAt.toISOString(),
    }))
    return Response.json({ conversations: out })
  } catch (e) {
    return errorResponse(e)
  }
}

export async function POST(req: NextRequest) {
  try {
    const user = await requireUser()
    const body = await req.json().catch(() => ({}))
    const { spaceId, title, model, provider } = body as {
      spaceId?: string
      title?: string
      model?: string
      provider?: string
    }
    if (!spaceId || typeof spaceId !== 'string') {
      return Response.json({ error: 'spaceId is required' }, { status: 400 })
    }
    await requireMember(spaceId, user.id)

    const conv = await db.conversation.create({
      data: {
        spaceId,
        userId: user.id,
        title: typeof title === 'string' && title.trim() ? title.slice(0, 200) : 'New Chat',
        model: typeof model === 'string' ? model : null,
        provider: typeof provider === 'string' ? provider : null,
      },
    })

    const out: ConversationDTO = {
      id: conv.id,
      spaceId: conv.spaceId,
      title: conv.title,
      model: conv.model,
      provider: conv.provider,
      pinned: conv.pinned,
      messageCount: 0,
      createdAt: conv.createdAt.toISOString(),
      updatedAt: conv.updatedAt.toISOString(),
    }
    return Response.json(out, { status: 201 })
  } catch (e) {
    return errorResponse(e)
  }
}
