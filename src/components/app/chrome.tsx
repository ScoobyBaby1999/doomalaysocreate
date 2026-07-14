'use client'

import { useApp, type View } from './store'
import { api } from './api-client'
import { Button } from '@/components/ui/button'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  LayoutDashboard,
  Boxes,
  MessageSquare,
  Gavel,
  Wand2,
  KeyRound,
  BarChart3,
  Settings as SettingsIcon,
  LogOut,
  Menu,
  Plus,
  ChevronDown,
} from 'lucide-react'
import { toast } from 'sonner'
import { useRouter } from 'next/navigation'

const NAV: { id: View; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { id: 'spaces', label: 'Spaces', icon: Boxes },
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'panel', label: 'Judge Panel', icon: Gavel },
  { id: 'templates', label: 'Templates', icon: Wand2 },
  { id: 'keys', label: 'API Keys', icon: KeyRound },
  { id: 'metrics', label: 'Metrics', icon: BarChart3 },
  { id: 'settings', label: 'Settings', icon: SettingsIcon },
]

export function AppTopbar() {
  const session = useApp((s) => s.session)
  const setView = useApp((s) => s.setView)
  const signOutLocal = useApp((s) => s.signOutLocal)
  const sidebarOpen = useApp((s) => s.sidebarOpen)
  const setSidebarOpen = useApp((s) => s.setSidebarOpen)
  const router = useRouter()

  const name = session && session !== 'loading' ? session.name || session.email : ''
  const initials = name
    ? name.slice(0, 2).toUpperCase()
    : (session && session !== 'loading' ? session.email.slice(0, 2).toUpperCase() : '??')

  return (
    <header className="sticky top-0 z-40 w-full border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex h-14 items-center gap-3 px-4 sm:px-6 max-w-[1600px] mx-auto">
        <Button
          variant="ghost"
          size="icon"
          className="lg:hidden"
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label="Toggle navigation"
        >
          <Menu className="size-5" />
        </Button>
        <button
          onClick={() => setView('dashboard')}
          className="flex items-center gap-2 font-semibold tracking-tight"
        >
          <span className="text-xl">🔥</span>
          <span className="hidden sm:inline">doomalaysocreate</span>
        </button>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="default" className="gap-1.5" onClick={() => setView('spaces')}>
            <Plus className="size-3.5" /> <span className="hidden sm:inline">New Space</span>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" className="gap-2 h-9 px-1.5">
                <Avatar className="size-7">
                  <AvatarFallback className="bg-primary/15 text-primary text-xs">{initials}</AvatarFallback>
                </Avatar>
                <ChevronDown className="size-3.5 text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuLabel className="font-normal">
                <div className="flex flex-col">
                  <span className="text-sm font-medium truncate">{name}</span>
                  <span className="text-xs text-muted-foreground truncate">
                    {session && session !== 'loading' ? session.email : ''}
                  </span>
                </div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => setView('settings')}>
                <SettingsIcon className="mr-2 size-4" /> Settings
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setView('keys')}>
                <KeyRound className="mr-2 size-4" /> API Keys
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                onClick={async () => {
                  try {
                    await api.signout()
                  } catch {
                    /* ignore */
                  }
                  signOutLocal()
                  toast.success('Signed out')
                  router.refresh()
                }}
              >
                <LogOut className="mr-2 size-4" /> Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </header>
  )
}

export function AppSidebar() {
  const view = useApp((s) => s.view)
  const setView = useApp((s) => s.setView)
  const sidebarOpen = useApp((s) => s.sidebarOpen)
  const setSidebarOpen = useApp((s) => s.setSidebarOpen)
  const spaces = useApp((s) => s.spaces)
  const currentSpace = useApp((s) => s.currentSpace)
  const setSpace = useApp((s) => s.setSpace)

  return (
    <>
      {/* mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={`
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
          fixed lg:sticky top-14 lg:top-14 z-40 lg:z-0
          h-[calc(100vh-3.5rem)] w-64 shrink-0
          border-r border-sidebar-border bg-sidebar
          transition-transform duration-200 ease-out
          flex flex-col
        `}
      >
        <nav className="flex-1 overflow-y-auto p-3 space-y-1">
          <div className="px-2 pb-2 pt-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Workspace
          </div>
          {NAV.map((item) => {
            const Icon = item.icon
            const active = view === item.id
            return (
              <button
                key={item.id}
                onClick={() => setView(item.id)}
                className={`w-full flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
                  active
                    ? 'bg-primary/15 text-primary font-medium'
                    : 'text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'
                }`}
              >
                <Icon className="size-4 shrink-0" />
                {item.label}
              </button>
            )
          })}
        </nav>

        {spaces.length > 0 && (
          <div className="border-t border-sidebar-border p-3">
            <div className="px-2 pb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Active Space
            </div>
            <select
              value={currentSpace?.id || ''}
              onChange={(e) => {
                const s = spaces.find((x) => x.id === e.target.value)
                setSpace(s || null)
              }}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs"
            >
              {spaces.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} {s.role === 'owner' ? '★' : ''}
                </option>
              ))}
            </select>
            {currentSpace && (
              <div className="mt-1.5 px-1 text-[10px] text-muted-foreground">
                {currentSpace.memberCount} members · {currentSpace.conversationCount} chats
              </div>
            )}
          </div>
        )}
      </aside>
    </>
  )
}

export function AppFooter() {
  return (
    <footer className="mt-auto border-t border-border/60 bg-background/60">
      <div className="max-w-[1600px] mx-auto px-4 sm:px-6 py-3 flex flex-col sm:flex-row items-center justify-between gap-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-2">
          <span>🔥 doomalaysocreate</span>
          <span className="opacity-40">·</span>
          <span>multi-tenant AI panel gateway</span>
        </div>
        <div className="flex items-center gap-3">
          <span>each user brings their own provider keys</span>
          <span className="opacity-40">·</span>
          <span>encrypted at rest (AES-256-GCM)</span>
        </div>
      </div>
    </footer>
  )
}
