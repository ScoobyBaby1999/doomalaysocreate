import { db } from '@/lib/db'
import { randomBytes } from 'crypto'

/** Public Space DTO returned by the spaces endpoints. */
export interface SpaceDto {
  id: string
  slug: string
  name: string
  description: string | null
  ownerId: string
  isPublic: boolean
  role: string
  memberCount: number
  conversationCount: number
  createdAt: string
}

type SpaceWithCounts = {
  id: string
  slug: string
  name: string
  description: string | null
  ownerId: string
  isPublic: boolean
  createdAt: Date
  _count: { members: number; conversations: number }
}

/**
 * Maps a Space record (with `_count.members` and `_count.conversations`)
 * plus a membership role into the public SpaceDto.
 */
export function toSpaceDto(
  space: SpaceWithCounts,
  role: string,
): SpaceDto {
  return {
    id: space.id,
    slug: space.slug,
    name: space.name,
    description: space.description,
    ownerId: space.ownerId,
    isPublic: space.isPublic,
    role,
    memberCount: space._count.members,
    conversationCount: space._count.conversations,
    createdAt: space.createdAt.toISOString(),
  }
}

const SLUG_RE = /[^a-z0-9]+/g

/** Normalize a string into a URL-safe slug. */
export function slugify(input: string): string {
  return input
    .toLowerCase()
    .trim()
    .replace(SLUG_RE, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
}

/** Generate a short id for slug uniqueness collisions. */
export function shortId(): string {
  return randomBytes(4).toString('hex')
}

/**
 * Generate a unique slug, ensuring it does not collide with an existing space.
 * Appends `-<shortid>` until unique.
 */
export async function uniqueSlug(base: string): Promise<string> {
  const root = slugify(base) || `space-${shortId()}`
  let candidate = root
  // bounded loop to avoid pathological infinite loops
  for (let i = 0; i < 8; i++) {
    const exists = await db.space.findUnique({ where: { slug: candidate }, select: { id: true } })
    if (!exists) return candidate
    candidate = `${root}-${shortId()}`
  }
  // last resort: append a longer random suffix
  return `${root}-${randomBytes(6).toString('hex')}`
}

/** Fetch a single space with membership for the given user (or null). */
export async function getSpaceDtoForUser(spaceId: string, userId: string) {
  const membership = await db.spaceMember.findUnique({
    where: { spaceId_userId: { spaceId, userId } },
    include: {
      space: {
        select: {
          id: true,
          slug: true,
          name: true,
          description: true,
          ownerId: true,
          isPublic: true,
          createdAt: true,
          _count: { select: { members: true, conversations: true } },
        },
      },
    },
  })
  if (!membership) return null
  return toSpaceDto(membership.space, membership.role)
}
