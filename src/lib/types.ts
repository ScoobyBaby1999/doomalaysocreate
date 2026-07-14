// Shared TypeScript types for the doomalaysocreate SaaS.

export type Role = 'critiquer' | 'verifier' | 'generator' | 'transformer' | 'parser' | 'planner' | 'custom'
export type Effort = 'low' | 'med' | 'high' | 'max'
export type Merge = 'dedupe' | 'vote' | 'concat' | 'none'
export type SpaceRole = 'owner' | 'admin' | 'member'

export interface UserDTO {
  id: string
  email: string
  name: string | null
  createdAt: string
}

export interface SpaceDTO {
  id: string
  slug: string
  name: string
  description: string | null
  ownerId: string
  isPublic: boolean
  role: SpaceRole
  memberCount: number
  conversationCount: number
  createdAt: string
}

export interface ProviderKeyDTO {
  id: string
  provider: string
  label: string
  hasKey: true
  createdAt: string
  // never includes the key value
}

export interface ConversationDTO {
  id: string
  spaceId: string
  title: string
  model: string | null
  provider: string | null
  pinned: boolean
  messageCount: number
  createdAt: string
  updatedAt: string
}

export interface MessageDTO {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  model: string | null
  provider: string | null
  createdAt: string
}

export interface ChatRequest {
  conversationId?: string
  spaceId: string
  provider: string
  model: string
  messages: { role: 'user' | 'assistant' | 'system'; content: string }[]
  maxTokens?: number
  temperature?: number
}

export interface PanelRequest {
  spaceId: string
  input: string
  role: Role
  system?: string
  panel?: string[] // logical names or provider/model slots
  effort?: Effort
  merge?: Merge
  maxTokens?: number
  async?: boolean
}

export interface PanelJudgeDTO {
  id: string
  model: string
  status: 'pending' | 'running' | 'done' | 'error'
  output: string | null
  error: string | null
  routedTo: string | null
  ok: boolean | null
}

export interface PanelJobDTO {
  id: string
  spaceId: string
  status: 'running' | 'complete' | 'error'
  role: Role
  input: string
  panel: string[]
  effort: Effort
  merge: Merge
  merged: string | null
  meta: { judgesOk: number; judgesTotal: number; judgesSettled: number; complete: boolean; elapsedS: number } | null
  template: string | null
  error: string | null
  judges: PanelJudgeDTO[]
  createdAt: string
}

export interface MetricDTO {
  provider: string
  model: string
  calls: number
  successRate: number
  throttle429: number
  avgLatencyMs: number
  tokensIn: number
  tokensOut: number
}
