import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from 'crypto'

// AES-256-GCM encryption for provider API keys at rest.
// Master key is derived (scrypt) from APP_SECRET so rotating APP_SECRET re-keys.

const SECRET = process.env.APP_SECRET || 'dev-insecure-secret-change-me'
const KEY_LEN = 32 // 256-bit
const SALT = Buffer.from('doomalaysocreate-key-v1', 'utf8')

let _key: Buffer | null = null
function masterKey(): Buffer {
  if (_key) return _key
  _key = scryptSync(SECRET, SALT, KEY_LEN)
  return _key
}

export function encrypt(plaintext: string): string {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', masterKey(), iv)
  const ct = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()])
  const tag = cipher.getAuthTag()
  // format: iv:tag:ct  (all base64)
  return [iv.toString('base64'), tag.toString('base64'), ct.toString('base64')].join(':')
}

export function decrypt(payload: string): string {
  const parts = payload.split(':')
  if (parts.length !== 3) throw new Error('bad ciphertext shape')
  const [ivB64, tagB64, ctB64] = parts
  const iv = Buffer.from(ivB64, 'base64')
  const tag = Buffer.from(tagB64, 'base64')
  const ct = Buffer.from(ctB64, 'base64')
  const decipher = createDecipheriv('aes-256-gcm', masterKey(), iv)
  decipher.setAuthTag(tag)
  const pt = Buffer.concat([decipher.update(ct), decipher.final()])
  return pt.toString('utf8')
}

/** Encrypt an arbitrary JSON-serialisable map (for provider extra fields). */
export function encryptJSON(obj: Record<string, string>): string {
  return encrypt(JSON.stringify(obj))
}

export function decryptJSON<T = Record<string, string>>(payload: string): T {
  return JSON.parse(decrypt(payload)) as T
}
