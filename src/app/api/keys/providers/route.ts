import { db } from '@/lib/db'
import { requireUser, errorResponse } from '@/lib/session'
import { PROVIDERS, type ProviderDef } from '@/lib/providers'

/**
 * GET /api/keys/providers — the provider catalog enriched with whether the
 * current user has stored keys for each provider (and which labels).
 *
 * Shape:
 * { providers: [{ name, displayName, pool, region, icon, color, limits,
 *                privacy, settingsUrl, extraFields, models, note,
 *                hasKey: boolean, keyLabels: string[] }] }
 */
export async function GET() {
  try {
    const user = await requireUser()

    const userKeys = await db.providerKey.findMany({
      where: { userId: user.id },
      select: { provider: true, label: true },
    })

    // group labels by provider
    const labelsByProvider = new Map<string, string[]>()
    for (const k of userKeys) {
      const arr = labelsByProvider.get(k.provider) ?? []
      arr.push(k.label)
      labelsByProvider.set(k.provider, arr)
    }

    const providers = PROVIDERS.map((p: ProviderDef) => {
      const labels = labelsByProvider.get(p.name) ?? []
      return {
        name: p.name,
        displayName: p.displayName,
        pool: p.pool,
        region: p.region,
        icon: p.icon,
        color: p.color,
        limits: p.limits,
        privacy: p.privacy,
        settingsUrl: p.settingsUrl,
        extraFields: p.extraFields ?? [],
        models: p.models,
        note: p.note,
        hasKey: labels.length > 0,
        keyLabels: labels,
      }
    })

    return Response.json({ providers })
  } catch (e) {
    return errorResponse(e)
  }
}
