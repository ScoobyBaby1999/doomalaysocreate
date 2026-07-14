'use client'

import { useEffect } from 'react'
import { useApp } from './store'
import { api } from './api-client'
import { Landing } from './views/landing'
import { Auth } from './views/auth'
import { Dashboard } from './views/dashboard'
import { Spaces } from './views/spaces'
import { Chat } from './views/chat'
import { Panel } from './views/panel'
import { Templates } from './views/templates'
import { Keys } from './views/keys'
import { Metrics } from './views/metrics'
import { Settings } from './views/settings'
import { AppSidebar, AppTopbar, AppFooter } from './chrome'

export function AppShell() {
  const session = useApp((s) => s.session)
  const view = useApp((s) => s.view)
  const setSession = useApp((s) => s.setSession)
  const setSpaces = useApp((s) => s.setSpaces)

  // bootstrap session on mount
  useEffect(() => {
    let alive = true
    api
      .me()
      .then((r) => alive && setSession(r.user))
      .catch(() => alive && setSession(null))
    return () => {
      alive = false
    }
  }, [setSession])

  // load spaces once authed
  useEffect(() => {
    if (session && session !== 'loading') {
      api.listSpaces().then((r) => setSpaces(r.spaces)).catch(() => {})
    }
  }, [session, setSpaces])

  if (session === 'loading') {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-4 grid-bg">
        <div className="text-4xl animate-pulse-dot">🔥</div>
        <div className="text-sm text-muted-foreground">booting doomalaysocreate…</div>
      </div>
    )
  }

  if (!session) {
    return (
      <div className="min-h-screen flex flex-col">
        <div className="flex-1">
          {view === 'auth' ? <Auth /> : <Landing />}
        </div>
        <AppFooter />
      </div>
    )
  }

  return (
    <div className="min-h-screen flex flex-col">
      <AppTopbar />
      <div className="flex-1 flex w-full max-w-[1600px] mx-auto w-full">
        <AppSidebar />
        <main className="flex-1 min-w-0 px-4 sm:px-6 lg:px-8 py-6">
          {view === 'dashboard' && <Dashboard />}
          {view === 'spaces' && <Spaces />}
          {view === 'chat' && <Chat />}
          {view === 'panel' && <Panel />}
          {view === 'templates' && <Templates />}
          {view === 'keys' && <Keys />}
          {view === 'metrics' && <Metrics />}
          {view === 'settings' && <Settings />}
        </main>
      </div>
      <AppFooter />
    </div>
  )
}
