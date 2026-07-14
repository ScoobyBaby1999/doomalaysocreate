import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, errorResponse, UnauthorizedError } from '@/lib/session'
import { encrypt, encryptJSON } from '@/lib/crypto'
import { getProvider } from '@/lib/providers'

/** DTO shape we always return for a stored key — NEVER includes the key value. */
function toKeyDTO(k: {
  id: string
  provider: string
  label: string
  createdAt: Date
}) {
  return {
    id: k.id,
    provider: k.provider,
    label: k.label,
    hasKey: true,
    createdAt: k.createdAt,
  }
}

/** GET /api/keys — list the current user's provider keys (no key values). */
export async function GET() {
  try {
    const user = await requireUser()
    const keys = await db.providerKey.findMany({
      where: { userId: user.id },
      orderBy: { createdAt: 'desc' },
      select: { id: true, provider: true, label: true, createdAt: true },
    })
    return Response.json({ keys: keys.map(toKeyDTO) })
  } catch (e) {
    return errorResponse(e)
  }
}

/** POST /api/keys — store (or update) an encrypted provider key for the user. */
export async function POST(req: NextRequest) {
  try {
    const user = await requireUser()
    const body = await req.json().catch(() => null)
    if (!body || typeof body !== 'object') {
      return Response.json({ error: 'Invalid JSON body' }, { status: 400 })
    }

    const provider = typeof body.provider === 'string' ? body.provider.trim() : ''
    const label = typeof body.label === 'string' ? body.label.trim() : ''
    const key = typeof body.key === 'string' ? body.key : ''
    const extra = body.extra

    if (!provider) {
      return Response.json({ error: 'provider is required' }, { status: 400 })
    }
    if (!label) {
      return Response.json({ error: 'label is required' }, { status: 400 })
    }
    if (!key) {
      return Response.json({ error: 'key is required' }, { status: 400 })
    }

    const def = getProvider(provider)
    if (!def) {
      return Response.json({ error: `Unknown provider: ${provider}` }, { status: 400 })
    }

    // The built-in zai provider uses the server key — users never store a key for it.
    if (provider === 'zai') {
      return Response.json(
        { error: 'The zai provider is built-in and does not need a user key.' },
        { status: 400 }
      )
    }

    // Validate `extra` shape if provided.
    if (extra !== undefined && extra !== null) {
      if (typeof extra !== 'object' || Array.isArray(extra)) {
        return Response.json({ error: 'extra must be an object of strings' }, { status: 400 })
      }
      for (const [k, v] of Object.entries(extra as Record<string, unknown>)) {
        if (typeof v !== 'string') {
          return Response.json(
            { error: `extra["${k}"] must be a string` },
            { status: 400 }
          )
        }
      }
    }

    const encryptedKey = encrypt(key)
    const encryptedExtra =
      extra && Object.keys(extra as Record<string, string>).length > 0
        ? encryptJSON(extra as Record<string, string>)
        : null

    // Upsert by unique [userId, provider, label] (label is part of the unique key,
    // so we use findFirst + update/create).
    const existing = await db.providerKey.findFirst({
      where: { userId: user.id, provider, label },
      select: { id: true },
    })

    let saved
    if (existing) {
      saved = await db.providerKey.update({
        where: { id: existing.id },
        data: { encryptedKey, encryptedExtra },
        select: { id: true, provider: true, label: true, createdAt: true },
      })
    } else {
      saved = await db.providerKey.create({
        data: {
          userId: user.id,
          provider,
          label,
          encryptedKey,
          encryptedExtra,
        },
        select: { id: true, provider: true, label: true, createdAt: true },
      })
    }

    return Response.json({ key: toKeyDTO(saved) }, { status: existing ? 200 : 201 })
  } catch (e) {
    if (e instanceof UnauthorizedError) {
      return errorResponse(e)
    }
    return errorResponse(e)
  }
}
