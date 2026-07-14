import { cookies } from 'next/headers'
import { randomBytes } from 'crypto'
import { db } from './db'

export const SESSION_COOKIE = process.env.SESSION_COOKIE || 'hootsession'
const SESSION_TTL_MS = 1000 * 60 * 60 * 24 * 30 // 30 days

export type CurrentUser = {
  id: string
  email: string
  name: string | null
}

export async function createSession(userId: string): Promise<string> {
  const token = randomBytes(32).toString('hex')
  const expiresAt = new Date(Date.now() + SESSION_TTL_MS)
  await db.session.create({ data: { userId, token, expiresAt } })
  return token
}

export async function setSessionCookie(token: string) {
  const store = await cookies()
  store.set(SESSION_COOKIE, token, {
    httpOnly: true,
    sameSite: 'lax',
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: SESSION_TTL_MS / 1000,
  })
}

export async function clearSessionCookie() {
  const store = await cookies()
  store.delete(SESSION_COOKIE)
}

export async function getUser(): Promise<CurrentUser | null> {
  const store = await cookies()
  const token = store.get(SESSION_COOKIE)?.value
  if (!token) return null
  const session = await db.session.findUnique({
    where: { token },
    include: { user: true },
  })
  if (!session) return null
  if (session.expiresAt < new Date()) {
    await db.session.delete({ where: { id: session.id } }).catch(() => {})
    return null
  }
  return { id: session.user.id, email: session.user.email, name: session.user.name }
}

export async function requireUser(): Promise<CurrentUser> {
  const u = await getUser()
  if (!u) throw new UnauthorizedError()
  return u
}

export class UnauthorizedError extends Error {
  status = 401
  constructor() {
    super('Unauthorized')
  }
}

/** Returns the current active space membership for the user, or null. */
export async function getMembership(spaceId: string, userId: string) {
  return db.spaceMember.findUnique({
    where: { spaceId_userId: { spaceId, userId } },
  })
}

export async function requireMember(spaceId: string, userId: string) {
  const m = await getMembership(spaceId, userId)
  if (!m) throw new ForbiddenError('not a member of this space')
  return m
}

export class ForbiddenError extends Error {
  status = 403
  constructor(msg = 'Forbidden') {
    super(msg)
  }
}

/** Standard JSON error helper for route handlers. */
export function errorResponse(e: unknown) {
  const err = e as { status?: number; message?: string }
  const status = err?.status ?? 500
  const message = err?.message ?? 'Internal Server Error'
  return Response.json({ error: message }, { status })
}
