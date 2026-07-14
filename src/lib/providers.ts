// Provider catalog — the multi-provider world of doomalaysocreate.
// Each provider is a host where frontier models live. Users bring their OWN API keys
// for these (stored encrypted). The built-in `zai` provider uses z-ai-web-dev-sdk
// with a server-side key so the app works with zero configuration.

export type ProviderPool = 'core' | 'optin' | 'builtin'

export interface ProviderDef {
  name: string
  displayName: string
  pool: ProviderPool
  region: string
  envVar?: string // the env var the original Python backend read; here for reference
  baseUrl: string
  icon: string // emoji
  color: string // tailwind-ish hex
  limits: { rpm?: number; rpd?: number; concurrency?: number; note?: string }
  privacy: { trainsOnData: boolean | 'per-model'; tier: number; note?: string }
  settingsUrl: string
  /** companion fields a key may need (e.g. cloudflare account id) */
  extraFields?: { key: string; label: string; required?: boolean }[]
  /** sample models this provider hosts (logical names) */
  models: { id: string; displayName: string; contextLength: number; family: string; free?: boolean }[]
  note?: string
}

export const PROVIDERS: ProviderDef[] = [
  {
    name: 'zai',
    displayName: 'Z.ai (Built-in)',
    pool: 'builtin',
    region: 'global',
    baseUrl: 'z-ai-web-dev-sdk',
    icon: 'Z',
    color: '#10b981',
    limits: { rpm: 60, note: 'built-in via z-ai-web-dev-sdk, no key needed from user' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: '#',
    models: [
      { id: 'glm-4.6', displayName: 'GLM-4.6', contextLength: 131072, family: 'glm', free: true },
      { id: 'glm-4.5', displayName: 'GLM-4.5', contextLength: 131072, family: 'glm', free: true },
      { id: 'glm-4.5v', displayName: 'GLM-4.5V (vision)', contextLength: 65536, family: 'glm', free: true },
    ],
    note: 'Default zero-config provider. Uses the platform server key; perfect for trying the app.',
  },
  {
    name: 'nvidia',
    displayName: 'NVIDIA NIM',
    pool: 'core',
    region: 'us',
    envVar: 'NVIDIA_API_KEY',
    baseUrl: 'https://integrate.api.nvidia.com/v1/chat/completions',
    icon: 'N',
    color: '#76b900',
    limits: { rpm: 40, concurrency: 4, note: 'free credits' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://build.nvidia.com/',
    models: [
      { id: 'deepseek-v4-flash', displayName: 'DeepSeek V4 Flash', contextLength: 262144, family: 'deepseek', free: true },
      { id: 'qwen3.5-397b', displayName: 'Qwen 3.5 397B', contextLength: 262144, family: 'qwen', free: true },
      { id: 'llama-4-maverick', displayName: 'Llama 4 Maverick', contextLength: 262144, family: 'llama', free: true },
      { id: 'minimax-m2.7', displayName: 'MiniMax M2.7', contextLength: 262144, family: 'minimax', free: true },
    ],
    note: 'Generous rpm, broad model roster.',
  },
  {
    name: 'cloudflare',
    displayName: 'Cloudflare Workers AI',
    pool: 'core',
    region: 'global',
    envVar: 'CF_API_TOKEN',
    baseUrl: 'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1/chat/completions',
    icon: 'C',
    color: '#f38020',
    limits: { rpm: 30, rpd: 10000, concurrency: 2, note: 'Workers AI free allocation' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://dash.cloudflare.com/?to=/:account/ai',
    extraFields: [{ key: 'CF_ACCOUNT_ID', label: 'Cloudflare Account ID', required: true }],
    models: [
      { id: 'kimi-k2.6', displayName: 'Kimi K2.6', contextLength: 262144, family: 'kimi', free: true },
      { id: 'nemotron-120b', displayName: 'Nemotron 120B', contextLength: 256000, family: 'nemotron', free: true },
      { id: 'gemma-4-31b', displayName: 'Gemma 4 31B', contextLength: 256000, family: 'gemma', free: true },
      { id: 'llama-3.3-70b', displayName: 'Llama 3.3 70B', contextLength: 24576, family: 'llama', free: true },
    ],
    note: 'Fast. Always pass max_tokens explicitly (defaults to 256).',
  },
  {
    name: 'github-models',
    displayName: 'GitHub Models',
    pool: 'core',
    region: 'us',
    envVar: 'GITHUB_TOKEN',
    baseUrl: 'https://models.github.ai/inference/chat/completions',
    icon: 'G',
    color: '#6e7681',
    limits: { rpm: 15, rpd: 150, concurrency: 1, note: 'low-tier rate limits' },
    privacy: { trainsOnData: false, tier: 2 },
    settingsUrl: 'https://github.com/settings/tokens',
    models: [
      { id: 'deepseek-v3', displayName: 'DeepSeek V3', contextLength: 65536, family: 'deepseek', free: true },
      { id: 'phi-4-reasoning', displayName: 'Phi-4 Reasoning', contextLength: 32768, family: 'phi', free: true },
      { id: 'llama-3.3-70b', displayName: 'Llama 3.3 70B', contextLength: 65536, family: 'llama', free: true },
    ],
    note: 'Clamps max output to ~4k.',
  },
  {
    name: 'openrouter',
    displayName: 'OpenRouter',
    pool: 'core',
    region: 'us',
    envVar: 'OPENROUTER_API_KEY',
    baseUrl: 'https://openrouter.ai/api/v1/chat/completions',
    icon: 'O',
    color: '#8b5cf6',
    limits: { rpm: 20, rpd: 50, concurrency: 2, note: 'shared free quota' },
    privacy: { trainsOnData: 'per-model', tier: 3, note: "':free' routes require logging consent" },
    settingsUrl: 'https://openrouter.ai/keys',
    models: [
      { id: 'nemotron-3-ultra-550b:free', displayName: 'Nemotron 3 Ultra 550B (Free)', contextLength: 131072, family: 'nemotron', free: true },
      { id: 'kimi-k2.6:free', displayName: 'Kimi K2.6 (Free)', contextLength: 131072, family: 'kimi', free: true },
      { id: 'gemma-4-31b:free', displayName: 'Gemma 4 31B (Free)', contextLength: 131072, family: 'gemma', free: true },
    ],
    note: ':free routes are NOT privacy-safe (logging consent).',
  },
  {
    name: 'opencode-zen',
    displayName: 'OpenCode Zen',
    pool: 'core',
    region: 'us',
    envVar: 'OPENCODE_ZEN_API_KEY',
    baseUrl: 'https://opencode.ai/zen/v1/chat/completions',
    icon: 'Z',
    color: '#06b6d4',
    limits: { rpm: 30, rpd: 100, concurrency: 2, note: 'free tier + pay-as-you-go' },
    privacy: { trainsOnData: 'per-model', tier: 1 },
    settingsUrl: 'https://opencode.ai/auth',
    models: [
      { id: 'deepseek-v4-flash-free', displayName: 'DeepSeek V4 Flash (Free)', contextLength: 262144, family: 'deepseek', free: true },
      { id: 'nemotron-3-ultra-free', displayName: 'Nemotron 3 Ultra (Free)', contextLength: 131072, family: 'nemotron', free: true },
      { id: 'mimo-v2.5-free', displayName: 'MiMo V2.5 (Free)', contextLength: 131072, family: 'mimo', free: true },
    ],
    note: 'Curated gateway; free models have $0 inference.',
  },
  {
    name: 'groq',
    displayName: 'Groq',
    pool: 'core',
    region: 'us',
    envVar: 'GROQ_API_KEY',
    baseUrl: 'https://api.groq.com/openai/v1/chat/completions',
    icon: 'G',
    color: '#f55036',
    limits: { rpm: 30, rpd: 14400, concurrency: 4, note: 'LPU inference, very fast' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://console.groq.com/keys',
    models: [
      { id: 'llama-3.3-70b-versatile', displayName: 'Llama 3.3 70B Versatile', contextLength: 131072, family: 'llama', free: true },
      { id: 'qwen-3-235b-a22b', displayName: 'Qwen 3 235B', contextLength: 131072, family: 'qwen', free: true },
      { id: 'deepseek-r1-distill-llama-70b', displayName: 'DeepSeek R1 Distill 70B', contextLength: 131072, family: 'deepseek', free: true },
    ],
    note: 'Fastest inference available; great for the panel.',
  },
  {
    name: 'cerebras',
    displayName: 'Cerebras',
    pool: 'optin',
    region: 'us',
    envVar: 'CEREBRAS_API_KEY',
    baseUrl: 'https://api.cerebras.ai/v1/chat/completions',
    icon: 'C',
    color: '#ef4444',
    limits: { rpm: 30, concurrency: 4, note: 'wafer-scale inference' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://cloud.cerebras.ai/',
    models: [
      { id: 'llama-3.3-70b', displayName: 'Llama 3.3 70B', contextLength: 131072, family: 'llama', free: true },
      { id: 'qwen-3-235b-a22b', displayName: 'Qwen 3 235B', contextLength: 131072, family: 'qwen', free: true },
    ],
  },
  {
    name: 'google',
    displayName: 'Google AI Studio',
    pool: 'optin',
    region: 'global',
    envVar: 'GOOGLE_API_KEY',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/models',
    icon: 'G',
    color: '#4285f4',
    limits: { rpm: 15, rpd: 1500, note: 'Gemini free tier' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://aistudio.google.com/app/apikey',
    models: [
      { id: 'gemini-2.5-pro', displayName: 'Gemini 2.5 Pro', contextLength: 1048576, family: 'gemini', free: true },
      { id: 'gemini-2.5-flash', displayName: 'Gemini 2.5 Flash', contextLength: 1048576, family: 'gemini', free: true },
    ],
    note: 'Huge context window; great for whole-repo review.',
  },
  {
    name: 'fireworks',
    displayName: 'Fireworks AI',
    pool: 'optin',
    region: 'us',
    envVar: 'FIREWORKS_API_KEY',
    baseUrl: 'https://api.fireworks.ai/inference/v1/chat/completions',
    icon: 'F',
    color: '#f97316',
    limits: { rpm: 30, note: 'pay-as-you-go' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://fireworks.ai/account/api-keys',
    models: [
      { id: 'deepseek-v4-flash', displayName: 'DeepSeek V4 Flash', contextLength: 262144, family: 'deepseek' },
    ],
  },
  {
    name: 'sambanova',
    displayName: 'SambaNova',
    pool: 'optin',
    region: 'us',
    envVar: 'SAMBANOVA_API_KEY',
    baseUrl: 'https://api.sambanova.ai/v1/chat/completions',
    icon: 'S',
    color: '#e11d48',
    limits: { rpm: 30, note: 'RDU inference' },
    privacy: { trainsOnData: false, tier: 1 },
    settingsUrl: 'https://cloud.sambanova.ai/',
    models: [
      { id: 'llama-3.3-70b', displayName: 'Llama 3.3 70B', contextLength: 131072, family: 'llama', free: true },
    ],
  },
]

export function getProvider(name: string): ProviderDef | undefined {
  return PROVIDERS.find((p) => p.name === name)
}

/** Logical roster: dedupe models across providers, listing every host. */
export interface RosterModel {
  logical: string
  displayName: string
  family: string
  contextLength: number
  free: boolean
  hosts: { provider: string; providerDisplayName: string; icon: string; color: string; modelId: string }[]
}

export function buildRoster(): RosterModel[] {
  const map = new Map<string, RosterModel>()
  for (const p of PROVIDERS) {
    for (const m of p.models) {
      const existing = map.get(m.id)
      const host = {
        provider: p.name,
        providerDisplayName: p.displayName,
        icon: p.icon,
        color: p.color,
        modelId: m.id,
      }
      if (existing) {
        existing.hosts.push(host)
        existing.contextLength = Math.max(existing.contextLength, m.contextLength)
        existing.free = existing.free || !!m.free
      } else {
        map.set(m.id, {
          logical: m.id,
          displayName: m.displayName,
          family: m.family,
          contextLength: m.contextLength,
          free: !!m.free,
          hosts: [host],
        })
      }
    }
  }
  return Array.from(map.values()).sort((a, b) => a.displayName.localeCompare(b.displayName))
}

/** Default judge panel — diverse families across diverse providers. */
export const DEFAULT_PANEL = [
  'zai/glm-4.6',
  'nvidia/deepseek-v4-flash',
  'groq/llama-3.3-70b-versatile',
  'openrouter/kimi-k2.6:free',
]

export const ROLES = [
  { id: 'critiquer', label: 'Critiquer', icon: '🔍', desc: 'Find issues, risks, and improvements' },
  { id: 'verifier', label: 'Verifier', icon: '✅', desc: 'PASS/FAIL vote with reasons' },
  { id: 'generator', label: 'Generator', icon: '✨', desc: 'Produce alternatives' },
  { id: 'transformer', label: 'Transformer', icon: '🔄', desc: 'Rewrite / transform' },
  { id: 'parser', label: 'Parser', icon: '📋', desc: 'Extract to structured JSON' },
  { id: 'planner', label: 'Planner', icon: '🗺️', desc: 'Produce a step plan' },
] as const

export const EFFORTS = [
  { id: 'low', label: 'Low', judges: 1, factor: 0.5, desc: '1 judge, half tokens' },
  { id: 'med', label: 'Medium', judges: 3, factor: 1, desc: '3 judges' },
  { id: 'high', label: 'High', judges: 5, factor: 1.5, desc: '5 judges, 1.5× tokens' },
  { id: 'max', label: 'Max', judges: 99, factor: 2, desc: 'all judges, 2× tokens' },
] as const

export const MERGES = [
  { id: 'dedupe', label: 'Dedupe (consensus)', desc: 'Consolidated bullets; consensus tagged' },
  { id: 'vote', label: 'Vote', desc: 'PASS/FAIL tally + reasons' },
  { id: 'concat', label: 'Concat', desc: 'Each model full answer, labelled' },
  { id: 'none', label: 'None', desc: 'No merge; raw judges' },
] as const

export const TEMPLATES = [
  {
    id: 'repo_audit',
    name: 'Repo Audit',
    icon: '🛡️',
    description: 'Map subsystems → parallel deep-review → cross-validate.',
    stages: ['map', 'review', 'crossvalidate'],
  },
  {
    id: 'design_doc',
    name: 'Design Doc',
    icon: '📐',
    description: 'Generate a design document for a system from a prompt.',
    stages: ['outline', 'draft', 'judge'],
  },
  {
    id: 'redteam',
    name: 'Red Team',
    icon: '⚔️',
    description: 'Attack a plan / system from multiple adversarial angles.',
    stages: ['enumerate', 'attack', 'judge'],
  },
  {
    id: 'panel_debate',
    name: 'Panel Debate',
    icon: '🎙️',
    description: 'Models debate a topic; judge consolidates.',
    stages: ['open', 'rebut', 'judge'],
  },
] as const
