// /api/chat — single-model streaming chatbot via Server-Sent Events.
// CRITICAL: each user uses their OWN provider key (resolved server-side).
// The built-in `zai` provider needs no key (server SDK).
import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, requireMember } from '@/lib/session'
import {
  resolveUserKey,
  streamChat,
  recordMetricFor,
  type ChatMsg,
  type StreamCallbacks,
} from '@/lib/ai'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(req: NextRequest) {
  // ---- auth -------------------------------------------------------------
  let user
  try {
    user = await requireUser()
  } catch (e) {
    const err = e as { status?: number; message?: string }
    return Response.json(
      { error: err?.message ?? 'Unauthorized' },
      { status: err?.status ?? 401 }
    )
  }

  // ---- body parse -------------------------------------------------------
  let body: Record<string, unknown>
  try {
    body = await req.json()
  } catch {
    return Response.json({ error: 'invalid JSON body' }, { status: 400 })
  }
  const {
    conversationId,
    spaceId,
    provider,
    model,
    messages,
    system,
    maxTokens,
    temperature,
  } = body as {
    conversationId?: string
    spaceId?: string
    provider?: string
    model?: string
    messages?: { role: 'user' | 'assistant' | 'system'; content: string }[]
    system?: string
    maxTokens?: number
    temperature?: number
  }

  // ---- validation -------------------------------------------------------
  if (!spaceId || typeof spaceId !== 'string') {
    return Response.json({ error: 'spaceId is required' }, { status: 400 })
  }
  if (!provider || typeof provider !== 'string') {
    return Response.json({ error: 'provider is required' }, { status: 400 })
  }
  if (!model || typeof model !== 'string') {
    return Response.json({ error: 'model is required' }, { status: 400 })
  }
  if (!Array.isArray(messages) || messages.length === 0) {
    return Response.json({ error: 'messages must be a non-empty array' }, { status: 400 })
  }
  for (const m of messages) {
    if (!m || typeof m.content !== 'string' || !['user', 'assistant', 'system'].includes(m.role)) {
      return Response.json({ error: 'each message must have role and content' }, { status: 400 })
    }
  }

  // ---- membership -------------------------------------------------------
  try {
    await requireMember(spaceId, user.id)
  } catch (e) {
    const err = e as { status?: number; message?: string }
    return Response.json(
      { error: err?.message ?? 'Forbidden' },
      { status: err?.status ?? 403 }
    )
  }

  // ---- key resolution ---------------------------------------------------
  const key = await resolveUserKey(user.id, provider)
  if (provider !== 'zai' && !key) {
    return Response.json(
      { error: `No API key stored for provider ${provider}. Add it in Keys.` },
      { status: 400 }
    )
  }

  // ---- conversation resolution -----------------------------------------
  let conversationIdFinal: string
  const firstUser = messages.find((m) => m.role === 'user')
  const title = firstUser ? firstUser.content.slice(0, 50).trim() || 'New Chat' : 'New Chat'

  if (conversationId) {
    const conv = await db.conversation.findUnique({ where: { id: conversationId } })
    if (!conv) {
      return Response.json({ error: 'conversation not found' }, { status: 404 })
    }
    if (conv.userId !== user.id) {
      return Response.json({ error: 'forbidden' }, { status: 403 })
    }
    if (conv.spaceId !== spaceId) {
      return Response.json(
        { error: 'conversation does not belong to this space' },
        { status: 400 }
      )
    }
    conversationIdFinal = conv.id
  } else {
    const conv = await db.conversation.create({
      data: { spaceId, userId: user.id, title, model, provider },
    })
    conversationIdFinal = conv.id
  }

  // ---- persist the user's latest message (always append) ----------------
  const lastUser = [...messages].reverse().find((m) => m.role === 'user')
  if (lastUser) {
    await db.message.create({
      data: {
        conversationId: conversationIdFinal,
        role: 'user',
        content: lastUser.content,
      },
    })
  }

  // ---- build the SSE response ------------------------------------------
  const encoder = new TextEncoder()
  const start = Date.now()

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      let closed = false
      const send = (obj: unknown) => {
        if (closed) return
        try {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n\n`))
        } catch {
          /* controller already closed */
        }
      }
      const close = () => {
        if (closed) return
        closed = true
        try {
          controller.close()
        } catch {
          /* already closed */
        }
      }

      let acc = ''
      // Track any async work kicked off inside callbacks so we can await it
      // BEFORE closing the stream (otherwise the 'done' event could be cut off).
      let pendingWork: Promise<void> = Promise.resolve()

      const callbacks: StreamCallbacks = {
        onDelta: (text) => {
          acc += text
          send({ type: 'delta', content: text })
        },
        onDone: (info) => {
          pendingWork = (async () => {
            try {
              const msg = await db.message.create({
                data: {
                  conversationId: conversationIdFinal,
                  role: 'assistant',
                  content: acc,
                  model,
                  provider,
                  tokensIn: info.tokensIn,
                  tokensOut: info.tokensOut,
                  latencyMs: info.latencyMs,
                },
              })
              await db.conversation.update({
                where: { id: conversationIdFinal },
                data: { updatedAt: new Date() },
              })
              send({ type: 'done', conversationId: conversationIdFinal, messageId: msg.id })
              await recordMetricFor(
                spaceId,
                user.id,
                provider,
                model,
                'chat',
                info.latencyMs,
                info.tokensIn ?? 0,
                info.tokensOut ?? 0,
                true,
                'success'
              ).catch(() => {})
            } catch (e) {
              const msg = e instanceof Error ? e.message : String(e)
              send({ type: 'error', error: msg })
              await recordMetricFor(
                spaceId,
                user.id,
                provider,
                model,
                'chat',
                info.latencyMs,
                0,
                0,
                false,
                'error'
              ).catch(() => {})
            }
          })()
        },
        onError: (err) => {
          pendingWork = (async () => {
            send({ type: 'error', error: err.message })
            await recordMetricFor(
              spaceId,
              user.id,
              provider,
              model,
              'chat',
              Date.now() - start,
              0,
              0,
              false,
              'error'
            ).catch(() => {})
          })()
        },
      }

      try {
        const chatMsgs: ChatMsg[] = messages.map((m) => ({
          role: m.role as 'user' | 'assistant' | 'system',
          content: m.content,
        }))
        await streamChat(
          provider,
          model,
          chatMsgs,
          {
            system,
            maxTokens,
            temperature,
            key: key ?? undefined,
          },
          callbacks
        )
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        send({ type: 'error', error: msg })
        await recordMetricFor(
          spaceId,
          user.id,
          provider,
          model,
          'chat',
          Date.now() - start,
          0,
          0,
          false,
          'error'
        ).catch(() => {})
      } finally {
        // Wait for any in-flight callback work (assistant message persist + done event)
        // before closing the stream so the client always gets the 'done' event.
        try {
          await pendingWork
        } catch {
          /* swallow */
        }
        close()
      }
    },
    cancel() {
      /* client disconnected; the start() finally-block already handles close */
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Conversation-Id': conversationIdFinal,
    },
  })
}
