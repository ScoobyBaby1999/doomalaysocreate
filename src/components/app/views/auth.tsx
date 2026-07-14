'use client'

import { useState } from 'react'
import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { toast } from 'sonner'
import { ArrowLeft } from 'lucide-react'

export function Auth() {
  const setSession = useApp((s) => s.setSession)
  const setView = useApp((s) => s.setView)
  const [mode, setMode] = useState<'signin' | 'signup'>('signup')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (password.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }
    setLoading(true)
    try {
      const r = mode === 'signup' ? await api.signup(email, password, name || undefined) : await api.signin(email, password)
      setSession(r.user)
      toast.success(mode === 'signup' ? 'Welcome to doomalaysocreate' : 'Signed in')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-[calc(100vh-3.5rem)] flex items-center justify-center px-4 py-12 grid-bg">
      <div className="w-full max-w-md">
        <button
          onClick={() => setView('landing')}
          className="mb-6 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="size-4" /> Back to home
        </button>
        <Card className="border-border/60 bg-card/80 backdrop-blur">
          <CardHeader>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-2xl">🔥</span>
              <CardTitle className="text-xl">
                {mode === 'signup' ? 'Create your account' : 'Welcome back'}
              </CardTitle>
            </div>
            <CardDescription>
              {mode === 'signup'
                ? 'Spin up your first Space in seconds. No provider key required.'
                : 'Sign in to access your Spaces and the panel.'}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="space-y-4">
              {mode === 'signup' && (
                <div className="space-y-2">
                  <Label htmlFor="name">Name (optional)</Label>
                  <Input id="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" />
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="password">Password</Label>
                <Input id="password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} placeholder="min 8 characters" />
              </div>
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? '…' : mode === 'signup' ? 'Create account' : 'Sign in'}
              </Button>
            </form>
            <div className="mt-4 text-center text-sm text-muted-foreground">
              {mode === 'signup' ? 'Already have an account?' : "Don't have one?"}{' '}
              <button
                onClick={() => setMode(mode === 'signup' ? 'signin' : 'signup')}
                className="text-primary hover:underline font-medium"
              >
                {mode === 'signup' ? 'Sign in' : 'Sign up'}
              </button>
            </div>
          </CardContent>
        </Card>
        <p className="mt-4 text-center text-xs text-muted-foreground">
          By continuing you agree to bring your own provider API keys and use them responsibly.
        </p>
      </div>
    </div>
  )
}
