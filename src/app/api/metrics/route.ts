import { NextRequest } from 'next/server'
import { db } from '@/lib/db'
import { requireUser, requireMember, errorResponse } from '@/lib/session'

interface MetricRow {
  provider: string
  model: string
  latencyMs: number
  tokensIn: number
  tokensOut: number
  ok: boolean
  code: string | null
}

interface Bucket {
  provider: string
  model: string
  calls: number
  ok: number
  throttle429: number
  latencySum: number
  tokensIn: number
  tokensOut: number
}

/**
 * GET /api/metrics?spaceId=...&provider=...
 *
 * Aggregates ALL metrics in a space (all members). The caller must be a member.
 * Returns:
 * { spaceId, totals, byProvider: [...], recent: [last 50] }
 */
export async function GET(req: NextRequest) {
  try {
    const user = await requireUser()

    const { searchParams } = new URL(req.url)
    const spaceId = searchParams.get('spaceId')?.trim()
    const providerFilter = searchParams.get('provider')?.trim() || null

    if (!spaceId) {
      return Response.json({ error: 'spaceId is required' }, { status: 400 })
    }

    // Caller must be a member of the space to see its metrics.
    await requireMember(spaceId, user.id)

    const where: { spaceId: string; provider?: string } = { spaceId }
    if (providerFilter) where.provider = providerFilter

    const rows: MetricRow[] = await db.metric.findMany({
      where,
      select: {
        provider: true,
        model: true,
        latencyMs: true,
        tokensIn: true,
        tokensOut: true,
        ok: true,
        code: true,
      },
      orderBy: { createdAt: 'desc' },
    })

    // Aggregate totals + byProvider in JS (dashboard volume is fine for SQLite).
    const buckets = new Map<string, Bucket>()
    let calls = 0
    let okCount = 0
    let throttle429 = 0
    let latencySum = 0
    let tokensIn = 0
    let tokensOut = 0

    for (const r of rows) {
      calls += 1
      if (r.ok) okCount += 1
      if (r.code === '429') throttle429 += 1
      latencySum += r.latencyMs || 0
      tokensIn += r.tokensIn || 0
      tokensOut += r.tokensOut || 0

      const key = `${r.provider}::${r.model}`
      let b = buckets.get(key)
      if (!b) {
        b = {
          provider: r.provider,
          model: r.model,
          calls: 0,
          ok: 0,
          throttle429: 0,
          latencySum: 0,
          tokensIn: 0,
          tokensOut: 0,
        }
        buckets.set(key, b)
      }
      b.calls += 1
      if (r.ok) b.ok += 1
      if (r.code === '429') b.throttle429 += 1
      b.latencySum += r.latencyMs || 0
      b.tokensIn += r.tokensIn || 0
      b.tokensOut += r.tokensOut || 0
    }

    const byProvider = Array.from(buckets.values())
      .map((b) => ({
        provider: b.provider,
        model: b.model,
        calls: b.calls,
        successRate: b.calls > 0 ? b.ok / b.calls : 0,
        throttle429: b.throttle429,
        avgLatencyMs: b.calls > 0 ? Math.round(b.latencySum / b.calls) : 0,
        tokensIn: b.tokensIn,
        tokensOut: b.tokensOut,
      }))
      .sort((a, b) => b.calls - a.calls)

    const totals = {
      calls,
      successRate: calls > 0 ? okCount / calls : 0,
      throttle429,
      avgLatencyMs: calls > 0 ? Math.round(latencySum / calls) : 0,
      tokensIn,
      tokensOut,
    }

    const recent = await db.metric.findMany({
      where,
      orderBy: { createdAt: 'desc' },
      take: 50,
    })

    return Response.json({
      spaceId,
      totals,
      byProvider,
      recent,
    })
  } catch (e) {
    return errorResponse(e)
  }
}
