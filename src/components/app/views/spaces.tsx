'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useState } from 'react'
import { Plus, Copy, MoreHorizontal, Trash2, Settings, ArrowRight } from 'lucide-react'
import { toast } from 'sonner'
import type { SpaceDTO } from '@/lib/types'

export function Spaces() {
  const spaces = useApp((s) => s.spaces)
  const setSpaces = useApp((s) => s.setSpaces)
  const upsertSpace = useApp((s) => s.upsertSpace)
  const removeSpace = useApp((s) => s.removeSpace)
  const setSpace = useApp((s) => s.setSpace)
  const setView = useApp((s) => s.setView)
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [isPublic, setIsPublic] = useState(false)
  const [loading, setLoading] = useState(false)

  async function create() {
    if (!name.trim()) return toast.error('Name is required')
    setLoading(true)
    try {
      const r = await api.createSpace({ name: name.trim(), description: desc.trim() || undefined, isPublic })
      upsertSpace(r.space)
      setOpen(false)
      setName(''); setDesc(''); setIsPublic(false)
      toast.success(`Created "${r.space.name}"`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
    } finally {
      setLoading(false)
    }
  }

  async function duplicate(s: SpaceDTO) {
    try {
      const r = await api.duplicateSpace(s.id)
      upsertSpace(r.space)
      toast.success(`Duplicated to "${r.space.name}"`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
    }
  }

  async function remove(s: SpaceDTO) {
    if (!confirm(`Delete "${s.name}"? This cannot be undone.`)) return
    try {
      await api.deleteSpace(s.id)
      removeSpace(s.id)
      toast.success('Deleted')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed')
    }
  }

  function openSpace(s: SpaceDTO) {
    setSpace(s)
    setView('chat')
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Spaces</h1>
          <p className="text-sm text-muted-foreground">Workspaces within this deployment. Duplicate one to fork a fresh environment.</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button className="gap-1.5"><Plus className="size-4" /> New Space</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Create a Space</DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <div className="space-y-1.5">
                <Label htmlFor="sp-name">Name</Label>
                <Input id="sp-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="My Team Workspace" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="sp-desc">Description (optional)</Label>
                <Textarea id="sp-desc" value={desc} onChange={(e) => setDesc(e.target.value)} rows={2} />
              </div>
              <div className="flex items-center justify-between rounded-md border border-border/50 px-3 py-2">
                <div>
                  <div className="text-sm font-medium">Public</div>
                  <div className="text-xs text-muted-foreground">Anyone can discover this space</div>
                </div>
                <Switch checked={isPublic} onCheckedChange={setIsPublic} />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
              <Button onClick={create} disabled={loading}>{loading ? 'Creating…' : 'Create'}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {spaces.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            No spaces yet. Click <span className="font-medium text-foreground">New Space</span> to create your first one.
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {spaces.map((s) => (
            <Card key={s.id} className="border-border/60 hover:border-primary/40 transition-colors">
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <button onClick={() => openSpace(s)} className="min-w-0 text-left flex-1">
                    <div className="font-semibold truncate">{s.name}</div>
                    <div className="text-xs text-muted-foreground truncate mt-0.5">{s.description || 'No description'}</div>
                  </button>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon" className="size-7 shrink-0"><MoreHorizontal className="size-4" /></Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onClick={() => duplicate(s)}><Copy className="mr-2 size-4" /> Duplicate</DropdownMenuItem>
                      <DropdownMenuItem onClick={() => openSpace(s)}><ArrowRight className="mr-2 size-4" /> Open</DropdownMenuItem>
                      {s.role === 'owner' && (
                        <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => remove(s)}>
                          <Trash2 className="mr-2 size-4" /> Delete
                        </DropdownMenuItem>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                <div className="flex items-center gap-3 mt-3 text-xs text-muted-foreground">
                  <span className="inline-flex items-center gap-1"><Settings className="size-3" /> {s.role}</span>
                  <span>{s.memberCount} members</span>
                  <span>{s.conversationCount} chats</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
