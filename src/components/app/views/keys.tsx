'use client'

import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { useEffect, useState } from 'react'
import { Plus, Trash2, KeyRound, Check, ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import type { ProviderKeyDTO } from '@/lib/types'

export function Keys() {
  const [providers, setProviders] = useState<any[]>([])
  const [keys, setKeys] = useState<ProviderKeyDTO[]>([])
  const [open, setOpen] = useState(false)
  const [selProvider, setSelProvider] = useState('')
  const [label, setLabel] = useState('')
  const [keyVal, setKeyVal] = useState('')
  const [extra, setExtra] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)

  async function load() {
    try {
      const [p, k] = await Promise.all([api.listProviders(), api.listKeys()])
      setProviders(p.providers || [])
      setKeys(k.keys)
    } catch { /* ignore */ }
  }
  useEffect(() => { load() }, [])

  function openDialog(p: any) {
    setSelProvider(p.name)
    setLabel('')
    setKeyVal('')
    setExtra({})
    if (p.extraFields) {
      const e: Record<string, string> = {}
      for (const f of p.extraFields) e[f.key] = ''
      setExtra(e)
    }
    setOpen(true)
  }

  async function save() {
    if (!keyVal.trim()) return toast.error('API key is required')
    if (!label.trim()) return toast.error('Label is required')
    setLoading(true)
    try {
      await api.addKey({ provider: selProvider, label: label.trim(), key: keyVal.trim(), extra: Object.keys(extra).length ? extra : undefined })
      toast.success('Key saved (encrypted at rest)')
      setOpen(false)
      load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
    } finally {
      setLoading(false)
    }
  }

  async function remove(id: string) {
    if (!confirm('Delete this key?')) return
    try {
      await api.deleteKey(id)
      load()
      toast.success('Key deleted')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">API Keys</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Each user brings their <span className="font-medium text-foreground">own</span> provider API keys.
          Keys are encrypted at rest (AES-256-GCM) and decrypted in-memory only at call time.
          The built-in <span className="text-primary">Z.ai</span> provider needs no key.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {providers.map((p) => {
          const myKeys = keys.filter((k) => k.provider === p.name)
          return (
            <Card key={p.name} className="border-border/60">
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="size-8 rounded-md flex items-center justify-center text-sm font-bold" style={{ background: p.color + '22', color: p.color }}>
                      {p.icon}
                    </span>
                    <div className="min-w-0">
                      <div className="font-semibold text-sm truncate">{p.displayName}</div>
                      <div className="text-xs text-muted-foreground">{p.pool} · {p.region}</div>
                    </div>
                  </div>
                  {p.name === 'zai' ? (
                    <Badge variant="secondary" className="text-[10px]">built-in</Badge>
                  ) : myKeys.length > 0 ? (
                    <Badge className="gap-1 text-[10px]"><Check className="size-2.5" /> {myKeys.length}</Badge>
                  ) : null}
                </div>
                <p className="text-xs text-muted-foreground mt-2 line-clamp-2">{p.note || p.limits?.note || '—'}</p>
                {myKeys.length > 0 && (
                  <div className="mt-2 space-y-1">
                    {myKeys.map((k) => (
                      <div key={k.id} className="flex items-center justify-between text-xs rounded border border-border/40 px-2 py-1">
                        <span className="font-mono truncate">•••• {k.label}</span>
                        <button onClick={() => remove(k.id)} className="text-muted-foreground hover:text-destructive shrink-0 ml-2">
                          <Trash2 className="size-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {p.name !== 'zai' && (
                  <Button size="sm" variant="outline" className="w-full mt-3 gap-1.5 text-xs" onClick={() => openDialog(p)}>
                    <Plus className="size-3" /> Add key
                  </Button>
                )}
                <a href={p.settingsUrl} target="_blank" rel="noreferrer" className="mt-1.5 flex items-center gap-1 text-[10px] text-muted-foreground hover:text-primary">
                  <ExternalLink className="size-2.5" /> get a key
                </a>
              </CardContent>
            </Card>
          )
        })}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><KeyRound className="size-4" /> Add {providers.find((p) => p.name === selProvider)?.displayName} key</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="space-y-1.5">
              <Label htmlFor="k-label">Label</Label>
              <Input id="k-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. personal / work" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="k-val">API Key</Label>
              <Input id="k-val" type="password" value={keyVal} onChange={(e) => setKeyVal(e.target.value)} placeholder="paste your key" />
            </div>
            {providers.find((p) => p.name === selProvider)?.extraFields?.map((f: any) => (
              <div key={f.key} className="space-y-1.5">
                <Label htmlFor={`k-${f.key}`}>{f.label}</Label>
                <Input id={`k-${f.key}`} value={extra[f.key] || ''} onChange={(e) => setExtra({ ...extra, [f.key]: e.target.value })} placeholder={f.key} />
              </div>
            ))}
            <p className="text-xs text-muted-foreground">Stored encrypted. Never returned by the API.</p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={save} disabled={loading}>{loading ? 'Saving…' : 'Save key'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
