'use client'

import { create } from 'zustand'
import type {
  UserDTO,
  SpaceDTO,
  ConversationDTO,
} from '@/lib/types'

export type View =
  | 'landing'
  | 'auth'
  | 'dashboard'
  | 'spaces'
  | 'chat'
  | 'panel'
  | 'templates'
  | 'keys'
  | 'metrics'
  | 'settings'

interface AppState {
  session: UserDTO | null | 'loading'
  view: View
  currentSpace: SpaceDTO | null
  spaces: SpaceDTO[]
  conversations: ConversationDTO[]
  currentConversationId: string | null
  sidebarOpen: boolean

  setSession: (u: UserDTO | null) => void
  setView: (v: View) => void
  setSpace: (s: SpaceDTO | null) => void
  setSpaces: (s: SpaceDTO[]) => void
  upsertSpace: (s: SpaceDTO) => void
  removeSpace: (id: string) => void
  setConversations: (c: ConversationDTO[]) => void
  setCurrentConversation: (id: string | null) => void
  setSidebarOpen: (o: boolean) => void
  signOutLocal: () => void
}

export const useApp = create<AppState>((set) => ({
  session: 'loading',
  view: 'landing',
  currentSpace: null,
  spaces: [],
  conversations: [],
  currentConversationId: null,
  sidebarOpen: false,

  setSession: (u) =>
    set((s) => ({
      session: u,
      view: u ? (s.view === 'landing' || s.view === 'auth' ? 'dashboard' : s.view) : 'landing',
    })),
  setView: (v) => set({ view: v, sidebarOpen: false }),
  setSpace: (space) => set({ currentSpace: space }),
  setSpaces: (spaces) => set({ spaces }),
  upsertSpace: (space) =>
    set((s) => {
      const idx = s.spaces.findIndex((x) => x.id === space.id)
      const next = [...s.spaces]
      if (idx >= 0) next[idx] = space
      else next.unshift(space)
      return { spaces: next }
    }),
  removeSpace: (id) =>
    set((s) => ({
      spaces: s.spaces.filter((x) => x.id !== id),
      currentSpace: s.currentSpace?.id === id ? null : s.currentSpace,
    })),
  setConversations: (conversations) => set({ conversations }),
  setCurrentConversation: (id) => set({ currentConversationId: id }),
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
  signOutLocal: () =>
    set({
      session: null,
      view: 'landing',
      currentSpace: null,
      spaces: [],
      conversations: [],
      currentConversationId: null,
    }),
}))
