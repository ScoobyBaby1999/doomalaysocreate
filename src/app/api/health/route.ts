import { NextResponse } from 'next/server'
import { PROVIDERS, ROLES } from '@/lib/providers'

export const dynamic = 'force-dynamic'

export async function GET() {
  return NextResponse.json({
    status: 'ok',
    service: 'doomalaysocreate',
    version: '0.1.0',
    providers: PROVIDERS.length,
    roles: ROLES.map((r) => r.id),
    timestamp: new Date().toISOString(),
  })
}
