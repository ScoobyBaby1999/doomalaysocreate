'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { toast } from 'sonner'

export function Settings() {
  const session = useApp((s) => s.session)
  const signOutLocal = useApp((s) => s.signOutLocal)

  const user = session && session !== 'loading' ? session : null

  return (
    <div className="space-y-4 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">Account and deployment info.</p>
      </div>
      <Card className="border-border/60">
        <CardHeader><CardTitle className="text-sm">Account</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <div className="flex justify-between"><span className="text-muted-foreground">Email</span><span>{user?.email}</span></div>
          <div className="flex justify-between"><span className="text-muted-foreground">Name</span><span>{user?.name || '—'}</span></div>
          <div className="flex justify-between"><span className="text-muted-foreground">User ID</span><span className="font-mono text-xs">{user?.id}</span></div>
          <Button variant="destructive" className="mt-2" onClick={async () => {
            try { await api.signout() } catch { /* ignore */ }
            signOutLocal()
            toast.success('Signed out')
          }}>Sign out</Button>
        </CardContent>
      </Card>
      <Card className="border-border/60">
        <CardHeader><CardTitle className="text-sm">Deployment</CardTitle></CardHeader>
        <CardContent className="text-sm space-y-1 text-muted-foreground">
          <p>This is a doomalaysocreate deployment. Duplicate this Hugging Face Space to spin up an isolated tenant with its own users and database.</p>
          <p>Each user stores their own provider API keys (encrypted). The built-in Z.ai provider needs no key.</p>
        </CardContent>
      </Card>
    </div>
  )
}
