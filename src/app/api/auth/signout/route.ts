import { NextResponse } from 'next/server'
import { cookies } from 'next/headers'
import { db } from '@/lib/db'
import {
  clearSessionCookie,
  SESSION_COOKIE,
  errorResponse,
} from '@/lib/session'

export const dynamic = 'force-dynamic'

export async function POST() {
  try {
    // Best-effort: delete the session row by token if present, then clear cookie.
    const store = await cookies()
    const token = store.get(SESSION_COOKIE)?.value
    if (token) {
      await db.session.delete({ where: { token } }).catch(() => {})
    }
    await clearSessionCookie()
    return NextResponse.json({ ok: true })
  } catch (e) {
    return errorResponse(e)
  }
}
