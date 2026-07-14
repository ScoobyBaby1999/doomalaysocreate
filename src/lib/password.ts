import { scryptSync, randomBytes, timingSafeEqual } from 'crypto'

// Password hashing using Node's scrypt (no external bcrypt dep).
// Format: scrypt$<saltB64>$<hashB64>

export function hashPassword(password: string): string {
  const salt = randomBytes(16)
  const hash = scryptSync(password, salt, 64)
  return `scrypt$${salt.toString('base64')}$${hash.toString('base64')}`
}

export function verifyPassword(password: string, stored: string): boolean {
  const parts = stored.split('$')
  if (parts.length !== 3 || parts[0] !== 'scrypt') return false
  const salt = Buffer.from(parts[1], 'base64')
  const expected = Buffer.from(parts[2], 'base64')
  const hash = scryptSync(password, salt, 64)
  if (hash.length !== expected.length) return false
  return timingSafeEqual(hash, expected)
}
