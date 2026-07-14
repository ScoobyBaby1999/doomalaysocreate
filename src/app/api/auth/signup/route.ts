import { NextResponse } from 'next/server'
import { db } from '@/lib/db'
import { hashPassword } from '@/lib/password'
import {
  createSession,
  setSessionCookie,
  errorResponse,
} from '@/lib/session'

export const dynamic = 'force-dynamic'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => null)
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'invalid body' }, { status: 400 })
    }
    const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : ''
    const password = typeof body.password === 'string' ? body.password : ''
    const name = typeof body.name === 'string' && body.name.trim() ? body.name.trim() : null

    if (!EMAIL_RE.test(email)) {
      return NextResponse.json({ error: 'invalid email' }, { status: 400 })
    }
    if (password.length < 8) {
      return NextResponse.json(
        { error: 'password must be at least 8 characters' },
        { status: 400 },
      )
    }

    const existing = await db.user.findUnique({ where: { email } })
    if (existing) {
      return NextResponse.json({ error: 'email already registered' }, { status: 409 })
    }

    const passwordHash = hashPassword(password)
    const user = await db.user.create({
      data: { email, name, passwordHash },
      select: { id: true, email: true, name: true, createdAt: true },
    })

    const token = await createSession(user.id)
    await setSessionCookie(token)

    return NextResponse.json({ user }, { status: 201 })
  } catch (e) {
    return errorResponse(e)
  }
}
