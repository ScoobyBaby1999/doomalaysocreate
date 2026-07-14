import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, errorResponse } from '@/lib/session'

/** DELETE /api/keys/[id] — delete one of the current user's own provider keys. */
export async function DELETE(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const user = await requireUser()
    const { id } = await params

    if (!id) {
      return Response.json({ error: 'id is required' }, { status: 400 })
    }

    // Only allow deleting a key that belongs to the current user.
    const existing = await db.providerKey.findUnique({
      where: { id },
      select: { userId: true },
    })

    if (!existing || existing.userId !== user.id) {
      // 404 rather than 403 to avoid leaking the existence of other users' keys.
      return Response.json({ error: 'Key not found' }, { status: 404 })
    }

    await db.providerKey.delete({ where: { id } })
    return Response.json({ ok: true })
  } catch (e) {
    return errorResponse(e)
  }
}
