'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useEffect, useState } from 'react'
import { Boxes, MessageSquare, Gavel, KeyRound, ArrowRight, Plus, Copy } from 'lucide-react'
import { toast } from 'sonner'
import type { View } from '../store'

export function Dashboard() {
  const session = useApp((s) => s.session)
  const spaces = useApp((s) => s.spaces)
  const setView = useApp((s) => s.setView)
  const setSpace = useApp((s) => s.setSpace)
  const upsertSpace = useApp((s) => s.upsertSpace)
  const [keys, setKeys] = useState<number>(0)
  const [health, setHealth] = useState<{ status?: string; providers?: number } | null>(null)

  useEffect(() => {
    api.listKeys().then((r) => setKeys(r.keys.length)).catch(() => {})
    api.health().then(setHealth).catch(() => {})
  }, [])

  const name = session && session !== 'loading' ? session.name || session.email.split('@')[0] : ''

  async function quickCreateSpace() {
    const name = `My Space ${spaces.length + 1}`
    try {
      const r = await api.createSpace({ name })
      upsertSpace(r.space)
      setSpace(r.space)
      toast.success(`Created "${r.space.name}"`)
      setView('chat')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to create space')
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Welcome back{name ? `, ${name}` : ''}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          This is your doomalaysocreate deployment. Each duplicate of this Hugging Face Space is an
          isolated tenant with its own users — and every user brings their own provider API keys.
        </p>
      </div>

      {/* status row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Spaces" value={spaces.length} icon={Boxes} onClick={() => setView('spaces')} />
        <StatCard label="API Keys" value={keys} icon={KeyRound} onClick={() => setView('keys')} />
        <StatCard label="Providers" value={health?.providers ?? '—'} icon={Gavel} onClick={() => setView('panel')} />
        <StatCard label="Service" value={health?.status ?? '…'} icon={MessageSquare} />
      </div>

      {/* quick actions */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card className="border-border/60">
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2"><Plus className="size-4" /> Quick start</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <Button className="w-full justify-between" onClick={quickCreateSpace}>
              Create a Space <ArrowRight className="size-4" />
            </Button>
            <Button variant="outline" className="w-full justify-between" onClick={() => setView('chat')}>
              Start chatting (uses built-in Z.ai) <ArrowRight className="size-4" />
            </Button>
            <Button variant="outline" className="w-full justify-between" onClick={() => setView('panel')}>
              Run the judge panel <ArrowRight className="size-4" />
            </Button>
            <Button variant="outline" className="w-full justify-between" onClick={() => setView('keys')}>
              Add your provider keys <ArrowRight className="size-4" />
            </Button>
          </CardContent>
        </Card>

        <Card className="border-border/60">
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2"><Copy className="size-4" /> Your Spaces</CardTitle>
          </CardHeader>
          <CardContent>
            {spaces.length === 0 ? (
              <p className="text-sm text-muted-foreground">No spaces yet. Create one to get started.</p>
            ) : (
              <div className="space-y-1.5 max-h-64 overflow-y-auto">
                {spaces.slice(0, 6).map((s) => (
                  <button
                    key={s.id}
                    onClick={() => { setSpace(s); setView('chat' as View) }}
                    className="w-full flex items-center justify-between rounded-md border border-border/50 px-3 py-2 text-left text-sm hover:bg-accent transition-colors"
                  >
                    <span className="font-medium truncate">{s.name}</span>
                    <span className="text-xs text-muted-foreground shrink-0 ml-2">{s.role}</span>
                  </button>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function StatCard({ label, value, icon: Icon, onClick }: { label: string; value: number | string; icon: typeof Boxes; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={!onClick}
      className="text-left rounded-lg border border-border/60 bg-card/60 p-3 disabled:cursor-default hover:border-primary/40 transition-colors"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Icon className="size-3.5 text-muted-foreground" />
      </div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
    </button>
  )
}
