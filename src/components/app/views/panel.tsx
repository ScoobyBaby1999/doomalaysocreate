'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useEffect, useRef, useState } from 'react'
import { Play, Loader2, CheckCircle2, XCircle, Clock, Gavel } from 'lucide-react'
import { toast } from 'sonner'

const ROLES = [
  { id: 'critiquer', label: '🔍 Critiquer' },
  { id: 'verifier', label: '✅ Verifier' },
  { id: 'generator', label: '✨ Generator' },
  { id: 'transformer', label: '🔄 Transformer' },
  { id: 'parser', label: '📋 Parser' },
  { id: 'planner', label: '🗺️ Planner' },
]
const EFFORTS = [
  { id: 'low', label: 'Low (1 judge)' },
  { id: 'med', label: 'Medium (3)' },
  { id: 'high', label: 'High (5)' },
  { id: 'max', label: 'Max (all)' },
]

export function Panel() {
  const currentSpace = useApp((s) => s.currentSpace)
  const setView = useApp((s) => s.setView)
  const [roster, setRoster] = useState<any>(null)
  const [input, setInput] = useState('')
  const [role, setRole] = useState('critiquer')
  const [effort, setEffort] = useState('med')
  const [panel, setPanel] = useState<string[]>([])
  const [job, setJob] = useState<any>(null)
  const [running, setRunning] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    api.roster().then((r) => {
      setRoster(r)
      setPanel(r.defaultPanel || [])
    }).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  async function run() {
    if (!currentSpace) { toast.error('Select a Space first'); return }
    if (!input.trim()) { toast.error('Enter input to critique'); return }
    setRunning(true)
    setJob(null)
    try {
      const r = await api.runPanel({
        spaceId: currentSpace.id,
        input: input.trim(),
        role,
        effort,
        panel: panel.length ? panel : undefined,
      })
      toast.success(`Panel started: ${r.jobId.slice(0, 8)}…`)
      poll(r.jobId)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to start panel')
      setRunning(false)
    }
  }

  function poll(jobId: string) {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const r = await api.getJob(jobId)
        setJob(r.job)
        if (r.job.status === 'complete' || r.job.status === 'error') {
          if (pollRef.current) clearInterval(pollRef.current)
          setRunning(false)
          if (r.job.status === 'error') toast.error('Panel failed')
        }
      } catch {
        /* keep polling */
      }
    }, 1500)
  }

  function toggleModel(slot: string) {
    setPanel((p) => p.includes(slot) ? p.filter((x) => x !== slot) : [...p, slot])
  }

  if (!currentSpace) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <Gavel className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground mb-3">Select or create a Space to run the panel.</p>
        <Button onClick={() => setView('spaces')}>Go to Spaces</Button>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 h-[calc(100vh-9rem)]">
      {/* left: input + controls */}
      <div className="flex flex-col gap-3 min-h-0">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Judge Panel</h1>
          <p className="text-sm text-muted-foreground">Fan your input out to a diverse panel of frontier models. Independent opinions, merged.</p>
        </div>

        <Textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Paste a plan, code, design doc, or any text to critique…"
          className="flex-1 min-h-[200px] resize-none text-sm"
        />

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">Role</label>
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>{ROLES.map((r) => <SelectItem key={r.id} value={r.id} className="text-sm">{r.label}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">Effort</label>
            <Select value={effort} onValueChange={setEffort}>
              <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>{EFFORTS.map((e) => <SelectItem key={e.id} value={e.id} className="text-sm">{e.label}</SelectItem>)}</SelectContent>
            </Select>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs text-muted-foreground">Panel ({panel.length} selected)</label>
            {roster?.defaultPanel && (
              <button onClick={() => setPanel(roster.defaultPanel)} className="text-xs text-primary hover:underline">reset to default</button>
            )}
          </div>
          <div className="border border-border/50 rounded-md p-2 max-h-32 overflow-y-auto space-y-1">
            {roster?.models?.map((m: any) => (
              <label key={m.logical} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-accent/50 rounded px-1.5 py-1">
                <input
                  type="checkbox"
                  checked={panel.includes(m.logical)}
                  onChange={() => toggleModel(m.logical)}
                  className="size-3.5"
                />
                <span className="font-medium">{m.displayName}</span>
                <span className="text-muted-foreground">— {m.hosts.map((h: any) => h.provider).join(', ')}</span>
              </label>
            ))}
          </div>
        </div>

        <Button onClick={run} disabled={running || !input.trim()} className="gap-1.5">
          {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          {running ? 'Running panel…' : 'Run Panel'}
        </Button>
      </div>

      {/* right: results */}
      <div className="flex flex-col min-h-0 border border-border/50 rounded-lg bg-card/40">
        <div className="p-3 border-b border-border/50 flex items-center justify-between">
          <span className="text-sm font-medium">Results</span>
          {job && (
            <Badge variant={job.status === 'complete' ? 'default' : job.status === 'error' ? 'destructive' : 'secondary'}>
              {job.status}
            </Badge>
          )}
        </div>
        <ScrollArea className="flex-1">
          <div className="p-3 space-y-3">
            {!job && (
              <div className="text-center text-sm text-muted-foreground py-12">
                Results will appear here. The panel runs asynchronously — judges stream in as they finish.
              </div>
            )}
            {job?.judges?.map((j: any) => (
              <Card key={j.id} className="border-border/50">
                <CardHeader className="py-2 px-3">
                  <CardTitle className="text-xs flex items-center gap-2">
                    {j.status === 'done' && <CheckCircle2 className="size-3.5 text-emerald-500" />}
                    {j.status === 'error' && <XCircle className="size-3.5 text-destructive" />}
                    {(j.status === 'pending' || j.status === 'running') && <Clock className="size-3.5 text-amber-500 animate-pulse-dot" />}
                    <span className="font-mono">{j.model}</span>
                    {j.routedTo && <span className="text-muted-foreground">→ {j.routedTo}</span>}
                  </CardTitle>
                </CardHeader>
                {j.output && (
                  <CardContent className="py-2 px-3 pt-0">
                    <pre className="text-xs whitespace-pre-wrap break-words font-sans max-h-64 overflow-y-auto">{j.output}</pre>
                  </CardContent>
                )}
                {j.error && (
                  <CardContent className="py-2 px-3 pt-0">
                    <p className="text-xs text-destructive">{j.error}</p>
                  </CardContent>
                )}
              </Card>
            ))}
            {job?.merged && (
              <Card className="border-primary/40 bg-primary/5">
                <CardHeader className="py-2 px-3">
                  <CardTitle className="text-sm">Merged Result</CardTitle>
                </CardHeader>
                <CardContent className="py-2 px-3 pt-0">
                  <pre className="text-xs whitespace-pre-wrap break-words font-sans max-h-96 overflow-y-auto">{job.merged}</pre>
                </CardContent>
              </Card>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  )
}
