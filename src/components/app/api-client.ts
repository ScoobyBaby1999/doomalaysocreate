'use client'

import type {
  UserDTO,
  SpaceDTO,
  ProviderKeyDTO,
  ConversationDTO,
  MessageDTO,
  PanelJobDTO,
  MetricDTO,
} from '@/lib/types'

async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const j = await res.json()
      msg = j.error || msg
    } catch {
      /* ignore */
    }
    const err = new Error(msg) as Error & { status: number }
    err.status = res.status
    throw err
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

type Any = any

export const api = {
  me: () => jfetch<{ user: UserDTO }>('/api/auth/me'),
  signup: (email: string, password: string, name?: string) =>
    jfetch<{ user: UserDTO }>('/api/auth/signup', { method: 'POST', body: JSON.stringify({ email, password, name }) }),
  signin: (email: string, password: string) =>
    jfetch<{ user: UserDTO }>('/api/auth/signin', { method: 'POST', body: JSON.stringify({ email, password }) }),
  signout: () => jfetch<{ ok: boolean }>('/api/auth/signout', { method: 'POST' }),

  listSpaces: () => jfetch<{ spaces: SpaceDTO[] }>('/api/spaces'),
  createSpace: (data: { name: string; slug?: string; description?: string; isPublic?: boolean }) =>
    jfetch<{ space: SpaceDTO }>('/api/spaces', { method: 'POST', body: JSON.stringify(data) }),
  getSpace: (id: string) => jfetch<{ space: SpaceDTO; members: Any[] }>(`/api/spaces/${id}`),
  updateSpace: (id: string, data: Partial<Pick<SpaceDTO, 'name' | 'description' | 'isPublic'>>) =>
    jfetch<{ space: SpaceDTO }>(`/api/spaces/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteSpace: (id: string) => jfetch<{ ok: boolean }>(`/api/spaces/${id}`, { method: 'DELETE' }),
  duplicateSpace: (id: string, data?: { name?: string; slug?: string }) =>
    jfetch<{ space: SpaceDTO }>(`/api/spaces/${id}/duplicate`, { method: 'POST', body: JSON.stringify(data || {}) }),
  listMembers: (id: string) => jfetch<{ members: Any[] }>(`/api/spaces/${id}/members`),
  inviteMember: (id: string, email: string, role: 'admin' | 'member') =>
    jfetch<{ member: Any }>(`/api/spaces/${id}/members`, { method: 'POST', body: JSON.stringify({ email, role }) }),
  removeMember: (id: string, userId: string) =>
    jfetch<{ ok: boolean }>(`/api/spaces/${id}/members/${userId}`, { method: 'DELETE' }),

  listKeys: () => jfetch<{ keys: ProviderKeyDTO[] }>('/api/keys'),
  addKey: (data: { provider: string; label: string; key: string; extra?: Record<string, string> }) =>
    jfetch<{ key: ProviderKeyDTO }>('/api/keys', { method: 'POST', body: JSON.stringify(data) }),
  deleteKey: (id: string) => jfetch<{ ok: boolean }>(`/api/keys/${id}`, { method: 'DELETE' }),
  listProviders: () => jfetch<Any>('/api/keys/providers'),

  roster: () => jfetch<Any>('/api/roster'),

  listConversations: (spaceId: string) =>
    jfetch<{ conversations: ConversationDTO[] }>(`/api/conversations?spaceId=${spaceId}`),
  createConversation: (data: { spaceId: string; title?: string; model?: string; provider?: string }) =>
    jfetch<{ conversation: ConversationDTO }>('/api/conversations', { method: 'POST', body: JSON.stringify(data) }),
  getMessages: (id: string) => jfetch<{ messages: MessageDTO[] }>(`/api/conversations/${id}/messages`),
  renameConversation: (id: string, title: string) =>
    jfetch<{ conversation: ConversationDTO }>(`/api/conversations/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }),
  deleteConversation: (id: string) =>
    jfetch<{ ok: boolean }>(`/api/conversations/${id}`, { method: 'DELETE' }),

  runPanel: (data: Any) =>
    jfetch<{ jobId: string; status: string; judges: Any[] }>('/api/panel', { method: 'POST', body: JSON.stringify({ ...data, async: true }) }),
  getJob: (id: string) => jfetch<{ job: PanelJobDTO }>(`/api/jobs/${id}`),

  listTemplates: () => jfetch<{ templates: Any[] }>('/api/templates'),
  runTemplate: (data: { spaceId: string; template: string; prompt: string; effort?: string }) =>
    jfetch<{ jobId: string }>('/api/templates/run', { method: 'POST', body: JSON.stringify({ ...data, async: true }) }),

  metrics: (spaceId: string, provider?: string) =>
    jfetch<{ spaceId: string; totals: Any; byProvider: Any[]; recent: Any[] }>(
      `/api/metrics?spaceId=${spaceId}${provider ? `&provider=${provider}` : ''}`,
    ),

  health: () => jfetch<Any>('/api/health'),
}

export type { UserDTO, SpaceDTO, ConversationDTO, MessageDTO, PanelJobDTO, MetricDTO }
