import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import { getUser, errorResponse } from '@/lib/session'

export const dynamic = 'force-dynamic'

export async function GET() {
  try {
    const user = await getUser()
    if (!user) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    const full = await db.user.findUnique({
      where: { id: user.id },
      select: { id: true, email: true, name: true, createdAt: true },
    })
    if (!full) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    return NextResponse.json({ user: full })
  } catch (e) {
    return errorResponse(e)
  }
}
