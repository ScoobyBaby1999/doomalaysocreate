'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useEffect, useRef, useState, useCallback } from 'react'
import { Send, Plus, MessageSquare, Loader2, AlertCircle } from 'lucide-react'
import { toast } from 'sonner'
import type { ConversationDTO, MessageDTO } from '@/lib/types'

interface PendingMsg { role: 'user' | 'assistant'; content: string; streaming?: boolean }

export function Chat() {
  const currentSpace = useApp((s) => s.currentSpace)
  const setView = useApp((s) => s.setView)
  const conversations = useApp((s) => s.conversations)
  const setConversations = useApp((s) => s.setConversations)
  const currentConversationId = useApp((s) => s.currentConversationId)
  const setCurrentConversation = useApp((s) => s.setCurrentConversation)

  const [roster, setRoster] = useState<any>(null)
  const [providers, setProviders] = useState<any[]>([])
  const [provider, setProvider] = useState('zai')
  const [model, setModel] = useState('glm-4.6')
  const [messages, setMessages] = useState<MessageDTO[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [streamBuf, setStreamBuf] = useState('')
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  // load roster + providers once
  useEffect(() => {
    api.roster().then(setRoster).catch(() => {})
    api.listProviders().then((r) => setProviders(r.providers || [])).catch(() => {})
  }, [])

  // load conversations when space changes
  useEffect(() => {
    if (currentSpace) {
      api.listConversations(currentSpace.id).then((r) => {
        setConversations(r.conversations)
        if (r.conversations.length > 0 && !currentConversationId) {
          setCurrentConversation(r.conversations[0].id)
        } else if (r.conversations.length === 0) {
          setCurrentConversation(null)
        }
      }).catch(() => {})
    }
  }, [currentSpace, setConversations, setCurrentConversation, currentConversationId])

  // load messages when conversation changes
  useEffect(() => {
    if (!currentConversationId) { setMessages([]); return }
    setLoadingMsgs(true)
    api.getMessages(currentConversationId).then((r) => setMessages(r.messages)).catch(() => setMessages([])).finally(() => setLoadingMsgs(false))
  }, [currentConversationId])

  // auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, streamBuf])

  const modelsForProvider = useCallback(() => {
    const p = roster?.providers?.find((x: any) => x.name === provider)
    return p?.models || []
  }, [roster, provider])

  async function send() {
    if (!currentSpace) { toast.error('Select or create a Space first'); return }
    if (!input.trim() || streaming) return
    const text = input.trim()
    setInput('')
    setStreaming(true)
    setStreamBuf('')

    const history = messages.map((m) => ({ role: m.role, content: m.content }))
    const payload = {
      spaceId: currentSpace.id,
      provider,
      model,
      messages: [...history, { role: 'user' as const, content: text }],
    }

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const j = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
        throw new Error(j.error || `HTTP ${res.status}`)
      }
      if (!res.body) throw new Error('No response body')

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let convId = currentConversationId
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() || ''
        for (const part of parts) {
          const line = part.trim()
          if (!line.startsWith('data:')) continue
          const data = line.slice(5).trim()
          try {
            const obj = JSON.parse(data)
            if (obj.type === 'delta') setStreamBuf((b) => b + obj.content)
            else if (obj.type === 'done') {
              convId = obj.conversationId
              if (!currentConversationId) setCurrentConversation(obj.conversationId)
              // refresh conversations list + messages
              api.listConversations(currentSpace.id).then((r) => setConversations(r.conversations)).catch(() => {})
              api.getMessages(obj.conversationId).then((r) => setMessages(r.messages)).catch(() => {})
            } else if (obj.type === 'error') {
              toast.error(obj.error)
            }
          } catch { /* skip */ }
        }
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Chat failed')
    } finally {
      setStreaming(false)
      setStreamBuf('')
    }
  }

  function newChat() {
    setCurrentConversation(null)
    setMessages([])
  }

  if (!currentSpace) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <MessageSquare className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground mb-3">Select or create a Space to start chatting.</p>
        <Button onClick={() => setView('spaces')}>Go to Spaces</Button>
      </div>
    )
  }

  return (
    <div className="flex gap-4 h-[calc(100vh-9rem)]">
      {/* conversation list */}
      <div className="w-56 shrink-0 hidden md:flex flex-col border border-border/50 rounded-lg bg-card/40">
        <div className="p-2 border-b border-border/50">
          <Button size="sm" variant="outline" className="w-full gap-1.5" onClick={newChat}>
            <Plus className="size-3.5" /> New Chat
          </Button>
        </div>
        <ScrollArea className="flex-1">
          <div className="p-1.5 space-y-0.5">
            {conversations.map((c) => (
              <button
                key={c.id}
                onClick={() => setCurrentConversation(c.id)}
                className={`w-full text-left text-xs rounded px-2 py-1.5 truncate transition-colors ${
                  c.id === currentConversationId ? 'bg-primary/15 text-primary' : 'hover:bg-accent'
                }`}
              >
                {c.title}
              </button>
            ))}
            {conversations.length === 0 && (
              <div className="text-xs text-muted-foreground px-2 py-4 text-center">No chats yet</div>
            )}
          </div>
        </ScrollArea>
      </div>

      {/* chat area */}
      <div className="flex-1 flex flex-col min-w-0 border border-border/50 rounded-lg bg-card/40">
        {/* header: model select */}
        <div className="flex items-center gap-2 p-2 border-b border-border/50">
          <Select value={provider} onValueChange={(v) => {
            setProvider(v)
            const p = roster?.providers?.find((x: any) => x.name === v)
            if (p?.models?.[0]) setModel(p.models[0].id)
          }}>
            <SelectTrigger className="w-40 h-8 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              {roster?.providers?.map((p: any) => (
                <SelectItem key={p.name} value={p.name} className="text-xs">
                  {p.icon} {p.displayName}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={model} onValueChange={setModel}>
            <SelectTrigger className="w-48 h-8 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              {modelsForProvider().map((m: any) => (
                <SelectItem key={m.id} value={m.id} className="text-xs">{m.displayName}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {provider !== 'zai' && !providers.find((p) => p.name === provider)?.hasKey && (
            <span className="text-xs text-amber-500 flex items-center gap-1">
              <AlertCircle className="size-3" /> no key
            </span>
          )}
        </div>

        {/* messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4">
          {loadingMsgs ? (
            <div className="flex justify-center py-8"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
          ) : messages.length === 0 && !streaming ? (
            <div className="text-center text-sm text-muted-foreground py-12">
              Start a conversation. Using <span className="text-primary font-medium">{provider}/{model}</span>.
            </div>
          ) : (
            messages.map((m) => <Bubble key={m.id} role={m.role} content={m.content} model={m.model} />)
          )}
          {streaming && (
            <Bubble role="assistant" content={streamBuf || '…'} streaming />
          )}
        </div>

        {/* input */}
        <div className="p-3 border-t border-border/50">
          <div className="flex gap-2 items-end">
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
              placeholder={`Message ${model}…`}
              rows={1}
              className="min-h-[40px] max-h-32 resize-none text-sm"
              disabled={streaming}
            />
            <Button size="icon" onClick={send} disabled={streaming || !input.trim()} className="shrink-0">
              {streaming ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}

function Bubble({ role, content, model, streaming }: { role: string; content: string; model?: string | null; streaming?: boolean }) {
  const isUser = role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
        isUser ? 'bg-primary text-primary-foreground' : 'bg-muted/60'
      }`}>
        <div className="whitespace-pre-wrap break-words">
          {content}
          {streaming && <span className="caret" />}
        </div>
        {!isUser && model && (
          <div className="text-[10px] text-muted-foreground mt-1 opacity-70">{model}</div>
        )}
      </div>
    </div>
  )
}
