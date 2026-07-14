import ZAI from 'z-ai-web-dev-sdk'
import { decrypt, decryptJSON } from './crypto'
import { db } from './db'
import { getProvider, buildRoster, type ProviderDef } from './providers'
import type { Role, Merge } from './types'

// ---------------------------------------------------------------------------
// Key resolution: each user brings their OWN provider keys (encrypted at rest).
// The built-in `zai` provider needs no user key (server-side via the SDK).
// ---------------------------------------------------------------------------

export interface ResolvedKey {
  provider: string
  apiKey: string
  extra?: Record<string, string>
}

export async function resolveUserKey(userId: string, provider: string): Promise<ResolvedKey | null> {
  if (provider === 'zai') return { provider, apiKey: 'builtin' } // server SDK
  const row = await db.providerKey.findFirst({
    where: { userId, provider },
    orderBy: { createdAt: 'asc' },
  })
  if (!row) return null
  const extra = row.encryptedExtra ? decryptJSON<Record<string, string>>(row.encryptedExtra) : undefined
  return { provider, apiKey: decrypt(row.encryptedKey), extra }
}

// ---------------------------------------------------------------------------
// Built-in zai SDK (zero-config default)
// ---------------------------------------------------------------------------

let _zai: Awaited<ReturnType<typeof ZAI.create>> | null = null
async function zai() {
  if (!_zai) _zai = await ZAI.create()
  return _zai
}

// ---------------------------------------------------------------------------
// Role → system prompt (ported from the original Python rubrics, condensed)
// ---------------------------------------------------------------------------

const ROLE_RUBRICS: Record<Role, string> = {
  critiquer:
    'You are a principal-engineer critiquer. Read the input and produce a consolidated list of concrete issues, risks, and improvements. ' +
    'Format: a markdown bullet list. Each bullet: `[SEVERITY] area — issue — evidence — fix`. SEVERITY is CRIT/HIGH/MED/LOW. ' +
    'Be specific and technical. Do not pad. Independent, uncorrelated opinions are the point.',
  verifier:
    'You are a verifier. Decide whether the input PASSES or FAILS its stated/implicit goals. ' +
    'Respond with a first line `VERDICT: PASS` or `VERDICT: FAIL`, then 3-5 bullet reasons. ' +
    'Cite the specific part of the input each reason refers to.',
  generator:
    'You are a generator. Produce three distinct alternative approaches to what the input describes. ' +
    'Label them `## Option A/B/C`. For each: a one-paragraph summary plus a bullet list of trade-offs.',
  transformer:
    'You are a transformer. Rewrite the input to improve clarity, correctness, and structure while preserving intent. ' +
    'Output only the rewritten text, no commentary.',
  parser:
    'You are a parser. Extract the structured information from the input into a single JSON object. ' +
    'Output ONLY valid JSON (no prose, no markdown fences). Use sensible keys.',
  planner:
    'You are a planner. Turn the input into an ordered, actionable step-by-step plan. ' +
    'Output a markdown numbered list. Each step: a short title, then a bullet list of sub-tasks.',
  custom: '',
}

export function systemPromptFor(role: Role, custom?: string): string {
  if (custom) return custom
  return ROLE_RUBRICS[role] || ROLE_RUBRICS.critiquer
}

// ---------------------------------------------------------------------------
// Single chat completion — built-in zai (SDK) or OpenAI-compatible (user key)
// ---------------------------------------------------------------------------

export interface ChatMsg {
  role: 'user' | 'assistant' | 'system'
  content: string
}

export interface CompletionResult {
  content: string
  model: string
  provider: string
  tokensIn?: number
  tokensOut?: number
  latencyMs: number
}

export async function complete(
  provider: string,
  model: string,
  messages: ChatMsg[],
  opts: { system?: string; maxTokens?: number; temperature?: number; key?: ResolvedKey } = {}
): Promise<CompletionResult> {
  const start = Date.now()
  const sys = opts.system ? [{ role: 'system' as const, content: opts.system }] : []
  const full = [...sys, ...messages]

  if (provider === 'zai') {
    const z = await zai()
    // The SDK uses 'assistant' role slot for system-style framing per its docs,
    // but it also accepts a leading system message. We pass messages through and
    // also set an assistant system preamble for compatibility.
    const sdkMessages = opts.system
      ? [{ role: 'assistant' as const, content: opts.system }, ...messages]
      : messages
    const completion = await z.chat.completions.create({
      // @ts-expect-error SDK accepts standard OpenAI messages shape
      messages: sdkMessages,
      model,
      thinking: { type: 'disabled' },
    } as Record<string, unknown>)
    const content = completion.choices[0]?.message?.content ?? ''
    return {
      content,
      model,
      provider: 'zai',
      latencyMs: Date.now() - start,
    }
  }

  // OpenAI-compatible dispatch for user-provided keys
  const def = getProvider(provider)
  if (!def) throw new Error(`unknown provider: ${provider}`)
  const key = opts.key
  if (!key) throw new Error(`no API key stored for provider: ${provider}`)

  const url = buildProviderUrl(def, key)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${key.apiKey}`,
  }
  if (provider === 'openrouter') {
    headers['HTTP-Referer'] = 'https://huggingface.co/spaces'
    headers['X-Title'] = 'doomalaysocreate panel'
  }

  const body: Record<string, unknown> = {
    model,
    messages: full,
    max_tokens: opts.maxTokens ?? 4096,
    temperature: opts.temperature ?? 0.7,
    stream: false,
  }
  const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${provider} ${res.status}: ${txt.slice(0, 300)}`)
  }
  const data = (await res.json()) as {
    choices?: { message?: { content?: string } }[]
    usage?: { prompt_tokens?: number; completion_tokens?: number }
  }
  const content = data.choices?.[0]?.message?.content ?? ''
  return {
    content,
    model,
    provider,
    tokensIn: data.usage?.prompt_tokens,
    tokensOut: data.usage?.completion_tokens,
    latencyMs: Date.now() - start,
  }
}

function buildProviderUrl(def: ProviderDef, key: ResolvedKey): string {
  let url = def.baseUrl
  if (def.extraFields) {
    for (const ef of def.extraFields) {
      const val = key.extra?.[ef.key] || ''
      url = url.replace(`{${ef.key}}`, encodeURIComponent(val))
    }
  }
  return url
}

// ---------------------------------------------------------------------------
// Streaming chat — emits 'delta' tokens over a ReadableStream for SSE.
// Falls back to a single full-chunk stream if the provider doesn't stream.
// ---------------------------------------------------------------------------

export interface StreamCallbacks {
  onDelta: (text: string) => void
  onDone: (info: { tokensIn?: number; tokensOut?: number; latencyMs: number }) => void
  onError: (err: Error) => void
}

export async function streamChat(
  provider: string,
  model: string,
  messages: ChatMsg[],
  opts: { system?: string; maxTokens?: number; temperature?: number; key?: ResolvedKey },
  cb: StreamCallbacks
): Promise<void> {
  const start = Date.now()
  const sys = opts.system ? [{ role: 'system' as const, content: opts.system }] : []
  const full = [...sys, ...messages]

  try {
    if (provider === 'zai') {
      const z = await zai()
      const sdkMessages = opts.system
        ? [{ role: 'assistant' as const, content: opts.system }, ...messages]
        : messages
      const result = await z.chat.completions.create({
        // @ts-expect-error SDK accepts standard OpenAI messages shape
        messages: sdkMessages,
        model,
        stream: true,
        thinking: { type: 'disabled' },
      } as Record<string, unknown>)

      // The SDK returns a raw ReadableStream when stream:true and the response
      // content-type is text/event-stream or text/plain. Otherwise it returns
      // a parsed JSON object (non-streaming fallback).
      if (result && typeof (result as ReadableStream<Uint8Array>).getReader === 'function') {
        let acc = ''
        const reader = (result as ReadableStream<Uint8Array>).getReader()
        const decoder = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          const lines = buf.split('\n')
          buf = lines.pop() || ''
          for (const line of lines) {
            const t = line.trim()
            if (!t || t.startsWith(':')) continue
            if (t.startsWith('data:')) {
              const data = t.slice(5).trim()
              if (data === '[DONE]') continue
              try {
                const obj = JSON.parse(data) as { choices?: { delta?: { content?: string }; message?: { content?: string } }[] }
                const delta = obj.choices?.[0]?.delta?.content || obj.choices?.[0]?.message?.content
                if (delta) {
                  acc += delta
                  cb.onDelta(delta)
                }
              } catch {
                /* skip partial */
              }
            }
          }
        }
        // flush any trailing buffer
        if (buf.trim().startsWith('data:')) {
          const data = buf.trim().slice(5).trim()
          if (data && data !== '[DONE]') {
            try {
              const obj = JSON.parse(data) as { choices?: { delta?: { content?: string } }[] }
              const delta = obj.choices?.[0]?.delta?.content
              if (delta) { acc += delta; cb.onDelta(delta) }
            } catch { /* skip */ }
          }
        }
        cb.onDone({ tokensOut: Math.ceil(acc.length / 4), latencyMs: Date.now() - start })
        return
      }

      // Non-streaming JSON fallback (SDK returned a parsed object)
      const completion = result as { choices?: { message?: { content?: string } }[]; usage?: { prompt_tokens?: number; completion_tokens?: number } }
      const content = completion.choices?.[0]?.message?.content ?? ''
      if (content) cb.onDelta(content)
      cb.onDone({
        tokensIn: completion.usage?.prompt_tokens,
        tokensOut: completion.usage?.completion_tokens ?? Math.ceil(content.length / 4),
        latencyMs: Date.now() - start,
      })
      return
    }

    // OpenAI-compatible streaming for user keys
    const def = getProvider(provider)
    if (!def) throw new Error(`unknown provider: ${provider}`)
    const key = opts.key
    if (!key) throw new Error(`no API key stored for provider: ${provider}`)
    const url = buildProviderUrl(def, key)
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${key.apiKey}`,
    }
    if (provider === 'openrouter') {
      headers['HTTP-Referer'] = 'https://huggingface.co/spaces'
      headers['X-Title'] = 'doomalaysocreate panel'
    }
    const body = JSON.stringify({
      model,
      messages: full,
      max_tokens: opts.maxTokens ?? 4096,
      temperature: opts.temperature ?? 0.7,
      stream: true,
    })
    const res = await fetch(url, { method: 'POST', headers, body })
    if (!res.ok || !res.body) {
      const txt = await res.text().catch(() => '')
      throw new Error(`${provider} ${res.status}: ${txt.slice(0, 300)}`)
    }
    let acc = ''
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() || ''
      for (const line of lines) {
        const t = line.trim()
        if (!t.startsWith('data:')) continue
        const data = t.slice(5).trim()
        if (data === '[DONE]') continue
        try {
          const obj = JSON.parse(data) as { choices?: { delta?: { content?: string } }[] }
          const delta = obj.choices?.[0]?.delta?.content
          if (delta) {
            acc += delta
            cb.onDelta(delta)
          }
        } catch {
          /* skip partial */
        }
      }
    }
    cb.onDone({ tokensOut: Math.ceil(acc.length / 4), latencyMs: Date.now() - start })
  } catch (e) {
    cb.onError(e instanceof Error ? e : new Error(String(e)))
  }
}

// ---------------------------------------------------------------------------
// Panel: fan out to many judges in parallel, each using the requesting user's
// own key for its provider. A judge failing never fails the panel.
// ---------------------------------------------------------------------------

export interface JudgeSpec {
  /** "provider/model" physical slot OR a logical model id (we pick the first host). */
  slot: string
}

export interface JudgeResult {
  model: string
  routedTo: string
  ok: boolean
  output: string | null
  error: string | null
  latencyMs: number
}

export function parseSlot(slot: string, userId_keys: Map<string, ResolvedKey>): { provider: string; model: string; key: ResolvedKey | null } | null {
  // "provider/model"
  const slash = slot.indexOf('/')
  if (slash > 0) {
    const provider = slot.slice(0, slash)
    const model = slot.slice(slash + 1)
    return { provider, model, key: userId_keys.get(provider) ?? null }
  }
  // logical model id: find first provider in roster hosting it that the user has a key for (or zai)
  const roster = buildRoster()
  const entry = roster.find((m) => m.logical === slot)
  if (!entry) return null
  // prefer zai, then any provider the user has a key for
  const ordered = [...entry.hosts].sort((a, b) => {
    const score = (p: string) => (p === 'zai' ? 0 : userId_keys.has(p) ? 1 : 2)
    return score(a.provider) - score(b.provider)
  })
  const host = ordered[0]
  return { provider: host.provider, model: host.modelId, key: userId_keys.get(host.provider) ?? null }
}

export async function runJudge(
  userId: string,
  slot: string,
  input: string,
  role: Role,
  systemOverride: string | undefined,
  opts: { maxTokens?: number }
): Promise<JudgeResult> {
  const start = Date.now()
  // Build a map of the user's resolved keys for slot resolution.
  const roster = buildRoster()
  // Pre-resolve: gather all providers the user might need.
  const userKeys = new Map<string, ResolvedKey>()
  const slash = slot.indexOf('/')
  const candidates = slash > 0 ? [slot.slice(0, slash)] : roster.flatMap((m) => m.hosts.map((h) => h.provider))
  for (const p of [...new Set(candidates)]) {
    if (p === 'zai') { userKeys.set('zai', { provider: 'zai', apiKey: 'builtin' }); continue }
    const k = await resolveUserKey(userId, p).catch(() => null)
    if (k) userKeys.set(p, k)
  }
  const parsed = parseSlot(slot, userKeys)
  if (!parsed) {
    return { model: slot, routedTo: slot, ok: false, output: null, error: `unknown model: ${slot}`, latencyMs: Date.now() - start }
  }
  const { provider, model, key } = parsed
  const routedTo = `${provider}/${model}`
  // Built-in zai works with no key; other providers REQUIRE a key.
  if (provider !== 'zai' && !key) {
    return { model: slot, routedTo, ok: false, output: null, error: `no API key stored for provider "${provider}". Add it in Keys.`, latencyMs: Date.now() - start }
  }
  try {
    const sys = systemPromptFor(role, systemOverride)
    const result = await complete(provider, model, [{ role: 'user', content: input }], {
      system: sys,
      maxTokens: opts.maxTokens ?? 8192,
      key: key ?? undefined,
    })
    // record a metric (best-effort)
    await recordMetric(userId, provider, model, role, result.latencyMs, result.tokensIn, result.tokensOut, true, 'success').catch(() => {})
    return { model: slot, routedTo, ok: true, output: result.content, error: null, latencyMs: result.latencyMs }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    const code = msg.includes('429') ? '429' : msg.includes('5') && /\d{3}/.test(msg) ? '5xx' : 'error'
    await recordMetric(userId, provider, model, role, Date.now() - start, 0, 0, false, code).catch(() => {})
    return { model: slot, routedTo, ok: false, output: null, error: msg, latencyMs: Date.now() - start }
  }
}

// ---------------------------------------------------------------------------
// Merge — consolidate judge outputs per the chosen mode
// ---------------------------------------------------------------------------

export function mergeOutputs(judges: JudgeResult[], mode: Merge, role: Role): string {
  const ok = judges.filter((j) => j.ok && j.output)
  if (ok.length === 0) {
    const errs = judges.map((j) => `- ${j.model}: ${j.error}`).join('\n')
    return `No judges succeeded.\n\nFailures:\n${errs}`
  }
  if (mode === 'none' || ok.length === 1) {
    return ok.map((j) => `### ${j.model} → ${j.routedTo}\n\n${j.output}`).join('\n\n---\n\n')
  }
  if (mode === 'concat') {
    return ok.map((j) => `## ${j.model}\n\n${j.output}`).join('\n\n---\n\n')
  }
  if (mode === 'vote') {
    const verdicts = ok.map((j) => {
      const m = (j.output || '').match(/VERDICT:\s*(PASS|FAIL)/i)
      return { model: j.model, verdict: m ? m[1].toUpperCase() : 'UNKNOWN', tail: (j.output || '').replace(m?.[0] || '', '').trim() }
    })
    const pass = verdicts.filter((v) => v.verdict === 'PASS').length
    const fail = verdicts.filter((v) => v.verdict === 'FAIL').length
    const tally = `## Vote tally: ${pass} PASS / ${fail} FAIL / ${verdicts.length - pass - fail} UNKNOWN\n\n`
    return tally + verdicts.map((v) => `### ${v.model} — ${v.verdict}\n${v.tail}`).join('\n\n')
  }
  // dedupe (default): split into bullets, normalise, count consensus
  const bulletCounts = new Map<string, { count: number; sample: string }>()
  for (const j of ok) {
    const bullets = (j.output || '')
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => /^[-*]/.test(l))
    for (const b of bullets) {
      const norm = b.replace(/^[-*]\s*/, '').toLowerCase().replace(/[^a-z0-9 ]/g, '').trim().slice(0, 80)
      if (!norm) continue
      const ex = bulletCounts.get(norm)
      if (ex) ex.count++
      else bulletCounts.set(norm, { count: 1, sample: b.replace(/^[-*]\s*/, '') })
    }
  }
  const sorted = [...bulletCounts.values()].sort((a, b) => b.count - a.count)
  const header = `## Merged ${role} (${ok.length}/${judges.length} judges succeeded)\n\n`
  const body = sorted
    .map((b) => {
      const tag = b.count > 1 ? ` _(flagged by ${b.count} judges)_` : ''
      return `- ${b.sample}${tag}`
    })
    .join('\n')
  const footer = `\n\n---\n_Conflict-free merge. Items flagged by multiple independent judges are the strongest signal._`
  return header + body + footer
}

// ---------------------------------------------------------------------------
// Metrics (best-effort)
// ---------------------------------------------------------------------------

async function recordMetric(
  userId: string,
  provider: string,
  model: string,
  role: Role,
  latencyMs: number,
  tokensIn: number | undefined,
  tokensOut: number | undefined,
  ok: boolean,
  code: string
) {
  // Need a spaceId; metrics are recorded at the call site which knows spaceId.
  // We store against the user's most recent space if not provided.
  // (The route handlers pass spaceId explicitly via a wrapper; this fallback
  //  records under the user's first space for safety.)
  const conv = await db.conversation.findFirst({ where: { userId }, orderBy: { updatedAt: 'desc' } })
  const spaceId = conv?.spaceId ?? 'system'
  if (spaceId === 'system') return
  await db.metric.create({
    data: { spaceId, userId, provider, model, role, latencyMs, tokensIn: tokensIn ?? 0, tokensOut: tokensOut ?? 0, ok, code },
  })
}

export async function recordMetricFor(
  spaceId: string,
  userId: string,
  provider: string,
  model: string,
  role: string,
  latencyMs: number,
  tokensIn: number,
  tokensOut: number,
  ok: boolean,
  code: string
) {
  await db.metric.create({
    data: { spaceId, userId, provider, model, role, latencyMs, tokensIn, tokensOut, ok, code },
  })
}
