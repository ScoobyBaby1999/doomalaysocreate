import { requireUser, errorResponse } from '@/lib/session'
import {
  PROVIDERS,
  buildRoster,
  DEFAULT_PANEL,
  ROLES,
  EFFORTS,
  MERGES,
  TEMPLATES,
} from '@/lib/providers'

/**
 * GET /api/roster — public-ish catalog data: logical roster, provider catalog,
 * default panel, and the role/effort/merge/template enums the UI needs.
 *
 * The provider catalog has no secrets (envVar names only), so it's safe to ship.
 */
export async function GET() {
  try {
    // requireUser to be safe; the SPA only calls this once authenticated.
    await requireUser()

    return Response.json({
      models: buildRoster(),
      providers: PROVIDERS,
      defaultPanel: DEFAULT_PANEL,
      roles: ROLES,
      efforts: EFFORTS,
      merges: MERGES,
      templates: TEMPLATES,
    })
  } catch (e) {
    return errorResponse(e)
  }
}
