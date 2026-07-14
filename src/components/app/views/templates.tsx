'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useEffect, useRef, useState } from 'react'
import { Play, Loader2, Wand2 } from 'lucide-react'
import { toast } from 'sonner'

export function Templates() {
  const currentSpace = useApp((s) => s.currentSpace)
  const setView = useApp((s) => s.setView)
  const [templates, setTemplates] = useState<any[]>([])
  const [sel, setSel] = useState('repo_audit')
  const [prompt, setPrompt] = useState('')
  const [effort, setEffort] = useState('med')
  const [job, setJob] = useState<any>(null)
  const [running, setRunning] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    api.listTemplates().then((r) => setTemplates(r.templates || [])).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  async function run() {
    if (!currentSpace) { toast.error('Select a Space first'); return }
    if (!prompt.trim()) { toast.error('Enter a prompt'); return }
    setRunning(true); setJob(null)
    try {
      const r = await api.runTemplate({ spaceId: currentSpace.id, template: sel, prompt: prompt.trim(), effort })
      toast.success('Template started')
      poll(r.jobId)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
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
        }
      } catch { /* keep */ }
    }, 1500)
  }

  if (!currentSpace) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <Wand2 className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground mb-3">Select a Space to run templates.</p>
        <Button onClick={() => setView('spaces')}>Go to Spaces</Button>
      </div>
    )
  }

  const current = templates.find((t) => t.id === sel)

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Templates</h1>
        <p className="text-sm text-muted-foreground">Multi-stage pipelines with the judge panel: map → review → judge.</p>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {templates.map((t) => (
          <Card key={t.id} className={`border-border/60 cursor-pointer transition-colors ${sel === t.id ? 'border-primary bg-primary/5' : 'hover:border-primary/40'}`} >
            <CardContent className="p-3" onClick={() => setSel(t.id)}>
              <div className="text-2xl mb-1">{t.icon}</div>
              <div className="font-semibold text-sm">{t.name}</div>
              <p className="text-xs text-muted-foreground mt-1">{t.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder={current ? `Prompt for ${current.name}…` : 'Prompt…'}
        rows={4}
        className="text-sm"
      />
      <div className="flex items-center gap-2">
        <Select value={effort} onValueChange={setEffort}>
          <SelectTrigger className="w-40 h-9 text-sm"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="low" className="text-sm">Low</SelectItem>
            <SelectItem value="med" className="text-sm">Medium</SelectItem>
            <SelectItem value="high" className="text-sm">High</SelectItem>
            <SelectItem value="max" className="text-sm">Max</SelectItem>
          </SelectContent>
        </Select>
        <Button onClick={run} disabled={running || !prompt.trim()} className="gap-1.5">
          {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          Run {current?.name}
        </Button>
      </div>
      {job && (
        <Card className="border-border/60">
          <CardContent className="p-3">
            <div className="text-xs text-muted-foreground mb-1">Status: {job.status}</div>
            {job.merged && <pre className="text-xs whitespace-pre-wrap break-words max-h-96 overflow-y-auto">{job.merged}</pre>}
            {job.error && <p className="text-xs text-destructive">{job.error}</p>}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
