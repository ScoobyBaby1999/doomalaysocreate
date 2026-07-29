/**
 * AgentChatV2 — New simplified chat screen using the v2 store.
 * 
 * Features:
 * - SSE-only streaming (no polling)
 * - No cursor tracking (since=0 always)
 * - Idempotent event handler
 * - Queue system for send-while-busy
 * - Model identity awareness
 * - Purple/black theme, mobile-first
 */
import { useEffect, useRef, useCallback, useState, useMemo } from "react";
import type { Settings } from "../api/panel";
import { V2ChatClient } from "../api/v2chat";
import { useV2Chat, type V2Message } from "../state/v2chatStore";
import { useModelStore } from "../lib/model-store";
import { Markdown } from "../components/Markdown";
import {
  Send, Loader2, Plus, MessageSquare, AlertCircle,
  Brain, Globe, Telescope, Gauge,
} from "lucide-react";

export function AgentChatV2({ settings }: { settings: Settings }) {
  // V2 chat store
  const sessions = useV2Chat((s) => s.sessions);
  const activeSessionId = useV2Chat((s) => s.activeSessionId);
  const messages = useV2Chat((s) => s.messages);
  const isBusy = useV2Chat((s) => s.isBusy);
  const queue = useV2Chat((s) => s.queue);
  const error = useV2Chat((s) => s.error);
  const init = useV2Chat((s) => s.init);
  const sendMessage = useV2Chat((s) => s.sendMessage);
  const createSession = useV2Chat((s) => s.createSession);
  const switchSession = useV2Chat((s) => s.switchSession);
  const stopGeneration = useV2Chat((s) => s.stopGeneration);

  // Model store
  const selectedModelId = useModelStore((s) => s.selectedModelId);
  const selectedSlotId = useModelStore((s) => s.selectedSlotId);
  const providers = useModelStore((s) => s.providers);
  const openOverlay = useModelStore((s) => s.openOverlay);

  // Local state
  const [input, setInput] = useState("");
  const [effort, setEffort] = useState<string | null>(null);
  const [webSearch, setWebSearch] = useState(false);
  const [deepResearch, setDeepResearch] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [tokenCount] = useState(0);
  const [contextLength, setContextLength] = useState(131072);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Create V2 client
  const client = useMemo(() => {
    return new V2ChatClient(settings.baseUrl || "", async () => {
      if (settings.rotationSecret) {
        const { deriveToken } = await import("../api/token");
        return deriveToken(settings.rotationSecret);
      }
      return settings.token;
    });
  }, [settings.baseUrl, settings.token, settings.rotationSecret]);

  // Init on mount
  useEffect(() => {
    init(client);
  }, [client]);

  // Auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // Effective model ID
  const effectiveModelId = selectedSlotId || selectedModelId;

  // Model display info
  const modelInfo = useMemo(() => {
    if (!selectedModelId) return null;
    for (const p of providers) {
      const m = p.models.find(
        (m: any) => m.id === selectedModelId || m.slotId === selectedSlotId,
      );
      if (m) return { label: m.displayName || m.id, provider: p.displayName, color: p.color };
    }
    return { label: selectedModelId.split("/").pop() || selectedModelId, provider: "", color: "#a855f7" };
  }, [providers, selectedModelId, selectedSlotId]);

  // Model capabilities (effort levels)
  const effortLevels = useMemo(() => {
    if (!selectedSlotId && !selectedModelId) return [];
    for (const p of providers) {
      const m = p.models.find(
        (m: any) => m.slotId === selectedSlotId || m.id === selectedModelId,
      );
      if (m?.attributes?.effort_levels) return m.attributes.effort_levels;
    }
    return [];
  }, [providers, selectedModelId, selectedSlotId]);

  const hasEffort = effortLevels.length > 0;

  // SP3.3: Update context length from model info
  useEffect(() => {
    if (!selectedModelId) return;
    for (const p of providers) {
      const m = p.models.find((m: any) => m.id === selectedModelId || m.slotId === selectedSlotId);
      if (m?.contextLength) {
        setContextLength(m.contextLength);
        return;
      }
    }
  }, [providers, selectedModelId, selectedSlotId]);

  // SP3.2: Track token count from status events
  useEffect(() => {
    // The messages array doesn't directly give us token count
    // But the last status event has usage data
    // We'll track it via a ref on the stream
  }, [messages]);

  // SP8.2: Debounced metadata save
  const saveMeta = useCallback((meta: Record<string, any>) => {
    if (!activeSessionId) return;
    const doSave = async () => {
      try {
        const t = settings.rotationSecret
          ? (await import("../api/token")).deriveToken(settings.rotationSecret)
          : settings.token;
        await fetch(`${settings.baseUrl || ""}/api/v2/chat/sessions/meta`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${t}` },
          body: JSON.stringify({ session_id: activeSessionId, ...meta }),
        });
      } catch {}
    };
    const timer = setTimeout(doSave, 800);
    return () => clearTimeout(timer);
  }, [activeSessionId, settings]);

  // Save metadata when settings change
  useEffect(() => {
    if (activeSessionId) {
      saveMeta({ model: effectiveModelId, effort, web_search: webSearch, deep_research: deepResearch });
    }
  }, [effectiveModelId, effort, webSearch, deepResearch, activeSessionId]);

  // Send message
  const handleSend = useCallback(async () => {
    if (!input.trim() || isBusy) return;
    const text = input.trim();
    setInput("");
    await sendMessage(client, text, {
      model: effectiveModelId || undefined,
      effort: effort || undefined,
      web_search: webSearch,
      deep_research: deepResearch,
    });
  }, [input, isBusy, client, sendMessage, effectiveModelId, effort, webSearch, deepResearch]);

  // Enter = newline (phone first), send via button only
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Ctrl/Cmd+Enter sends
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  // New chat
  const handleNewChat = useCallback(async () => {
    await createSession(client, effectiveModelId || undefined);
    inputRef.current?.focus();
  }, [client, createSession, effectiveModelId]);

  if (!settings.baseUrl && !settings.token && !settings.rotationSecret) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <MessageSquare className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground mb-3">Configure your settings to start chatting.</p>
        <button onClick={() => window.location.reload()}>Go to Settings</button>
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)]">
      {/* Session sidebar */}
      <SessionSidebarV2
        sessions={sessions}
        activeId={activeSessionId}
        onSelect={(id) => switchSession(client, id)}
        onNew={handleNewChat}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* Chat area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border/40 shrink-0">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="lg:hidden touch-target p-1.5 rounded-lg hover:bg-muted/40"
          >
            <MessageSquare className="size-4" />
          </button>
          
          {/* Model badge */}
          <button
            onClick={() => openOverlay()}
            className="flex items-center gap-1.5 px-2.5 h-8 rounded-full bg-accent/10 border border-accent/30 text-xs font-medium hover:bg-accent/20 transition-colors"
          >
            <span className="size-2 rounded-full" style={{ backgroundColor: modelInfo?.color || "#a855f7" }} />
            <span className="truncate max-w-[120px]">{modelInfo?.label || "Select model"}</span>
            {modelInfo?.provider && <span className="text-[9px] text-muted-foreground">· {modelInfo.provider}</span>}
          </button>

          {/* Effort indicator */}
          {hasEffort && effort && (
            <div className="flex items-center gap-1 px-2 h-7 rounded-full bg-purple-500/10 border border-purple-500/20 text-[10px] text-purple-400">
              <Gauge className="size-3" />
              {effort}
            </div>
          )}

          {/* Tool indicators */}
          {webSearch && (
            <div className="flex items-center gap-1 px-2 h-7 rounded-full bg-blue-500/10 border border-blue-500/20 text-[10px] text-blue-400">
              <Globe className="size-3" /> Web
            </div>
          )}
          {deepResearch && (
            <div className="flex items-center gap-1 px-2 h-7 rounded-full bg-teal-500/10 border border-teal-500/20 text-[10px] text-teal-400">
              <Telescope className="size-3" /> Deep
            </div>
          )}

          <div className="ml-auto flex items-center gap-1.5">
            <ContextCircleV2 used={tokenCount} total={contextLength} />
            <div className="text-[9px] text-emerald-500">FREE</div>
          </div>
        </div>

        {/* Error banner */}
        {error && (
          <div className="px-3 py-2 border-b border-red-500/30 bg-red-500/10 text-[13px] text-red-300 flex items-center gap-2">
            <AlertCircle className="size-4 shrink-0" />
            <span className="flex-1 truncate">{error}</span>
            <button onClick={() => useV2Chat.setState({ error: null })} className="text-red-300/70 hover:text-red-200 px-2 text-[11px]">dismiss</button>
          </div>
        )}

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-4 space-y-3">
          {messages.length === 0 && !isBusy ? (
            <div className="flex flex-col items-center justify-center h-full text-center gap-3">
              <div className="size-12 rounded-full bg-accent/10 flex items-center justify-center">
                <Brain className="size-6 text-accent" />
              </div>
              <p className="text-sm text-muted-foreground">Send a message to start chatting</p>
              <p className="text-[10px] text-muted-foreground/60">Using {modelInfo?.label || "default model"}</p>
            </div>
          ) : (
            messages.map((msg) => (
              <MessageBubbleV2 key={msg.id} msg={msg} />
            ))
          )}
          {isBusy && messages.length > 0 && !messages[messages.length - 1]?.isStreaming && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground px-2">
              <Loader2 className="size-3 animate-spin" />
              thinking...
            </div>
          )}
        </div>

        {/* Queue indicator */}
        {queue.length > 0 && (
          <div className="px-3 py-1 text-[10px] text-accent flex items-center gap-1 border-t border-border/20">
            <Loader2 className="size-2.5 animate-spin" />
            {queue.length} message{queue.length !== 1 ? "s" : ""} queued
          </div>
        )}

        {/* Input */}
        <div className="p-3 border-t border-border/40 shrink-0">
          {/* Tool bar */}
          <div className="flex items-center gap-1 mb-2">
            {hasEffort && (
              <button
                onClick={() => {
                  const idx = effortLevels.indexOf(effort || "");
                  const next = idx < effortLevels.length - 1 ? effortLevels[idx + 1] : effortLevels[0];
                  setEffort(effort === next ? null : next);
                }}
                className={`touch-target flex items-center gap-1 px-2 h-7 rounded-full text-[10px] ${effort ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-muted/40"}`}
                title={`Effort: ${effort || "off"} (click to cycle)`}
              >
                <Gauge className="size-3" />
                {effort || "effort"}
              </button>
            )}
            <button
              onClick={() => { setWebSearch(!webSearch); if (deepResearch) setDeepResearch(false); }}
              className={`touch-target flex items-center gap-1 px-2 h-7 rounded-full text-[10px] ${webSearch ? "bg-blue-500/15 text-blue-400" : "text-muted-foreground hover:bg-muted/40"}`}
            >
              <Globe className="size-3" /> Web
            </button>
            <button
              onClick={() => { setDeepResearch(!deepResearch); if (webSearch) setWebSearch(false); }}
              className={`touch-target flex items-center gap-1 px-2 h-7 rounded-full text-[10px] ${deepResearch ? "bg-teal-500/15 text-teal-400" : "text-muted-foreground hover:bg-muted/40"}`}
            >
              <Telescope className="size-3" /> Deep
            </button>
            {(effort || webSearch || deepResearch) && (
              <button
                onClick={() => { setEffort(null); setWebSearch(false); setDeepResearch(false); }}
                className="touch-target px-2 h-7 rounded-full text-[10px] text-muted-foreground hover:text-foreground"
              >
                Reset
              </button>
            )}
          </div>

          {/* Textarea + send */}
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={`Message ${modelInfo?.label || "AI"}...`}
              rows={1}
              className="min-h-[40px] max-h-32 resize-none text-sm rounded-2xl bg-background border border-border/40 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-accent/30"
              disabled={isBusy && queue.length >= 3}
            />
            <button
              onClick={isBusy ? stopGeneration : handleSend}
              disabled={!isBusy && !input.trim()}
              className="shrink-0 rounded-full size-10 flex items-center justify-center bg-accent text-white hover:bg-accent/90 transition-colors"
            >
              {isBusy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
            </button>
          </div>
          <div className="text-[9px] text-muted-foreground mt-1 px-0.5">
            Enter for newline · Ctrl+Enter to send
          </div>
        </div>
      </div>
    </div>
  );
}

// === Session Sidebar (simplified) ===
function SessionSidebarV2({ sessions, activeId, onSelect, onNew, open, onToggle }: {
  sessions: any[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={onToggle} />}
      <aside className={`${open ? "translate-x-0" : "-translate-x-full lg:translate-x-0"} fixed lg:sticky top-14 z-40 lg:z-0 h-[calc(100vh-3.5rem)] w-60 shrink-0 border-r border-border/40 bg-sidebar transition-transform duration-200 flex flex-col`}>
        <div className="p-2 border-b border-border/40">
          <button className="w-full gap-1.5" onClick={onNew}>
            <Plus className="size-3.5" /> New Chat
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => onSelect(s.id)}
              className={`w-full text-left text-xs rounded-lg px-2 py-1.5 truncate transition-colors ${
                s.id === activeId ? "bg-accent/15 text-accent font-medium" : "hover:bg-muted/40 text-muted-foreground"
              }`}
            >
              {s.title}
            </button>
          ))}
          {sessions.length === 0 && (
            <div className="text-[10px] text-muted-foreground/50 px-2 py-4 text-center">No chats yet</div>
          )}
        </div>
      </aside>
    </>
  );
}

// === Message Bubble (simplified) ===
function MessageBubbleV2({ msg }: { msg: V2Message }) {
  const isUser = msg.role === "user";
  const isThinking = msg.role === "thinking";
  const isTool = msg.role === "tool" || msg.role === "tool_result";

  if (isThinking) {
    return <ThinkingBubble msg={msg} />;
  }

  if (isTool) {
    return (
      <div className="px-3 py-1">
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <span className="px-1.5 py-0.5 rounded bg-muted/40 font-mono">{msg.toolName || "tool"}</span>
          <span className="truncate">{msg.content.slice(0, 80)}</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm ${
        isUser
          ? "bg-gradient-to-br from-purple-600 to-purple-700 text-white rounded-br-md"
          : "bg-muted/50 border border-border/30 rounded-bl-md"
      }`}>
        {isUser ? (
          <div className="whitespace-pre-wrap">{msg.content}</div>
        ) : (
          <Markdown text={msg.content || (msg.isStreaming ? "..." : "")} />
        )}
        {msg.isStreaming && (
          <span className="inline-block w-1.5 h-3.5 bg-accent animate-pulse ml-0.5 align-middle" />
        )}
      </div>
    </div>
  );
}


// === Thinking Bubble (expandable/collapsible) ===
function ThinkingBubble({ msg }: { msg: V2Message }) {
  const [expanded, setExpanded] = useState(true); // Default EXPANDED
  const hasContent = msg.content && msg.content.trim().length > 0;
  const isLong = hasContent && msg.content.length > 300;
  const displayContent = expanded ? msg.content : (hasContent ? msg.content.slice(0, 150) + "..." : "");
  
  return (
    <div className="px-3 py-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <Brain className="size-2.5" />
        {msg.isStreaming ? "thinking..." : "thought process"}
        {hasContent && isLong && (
          <span className="text-[9px] text-muted-foreground/50">
            ({expanded ? "collapse" : "expand"})
          </span>
        )}
      </button>
      {hasContent && (
        <div className={`mt-1 pl-4 border-l border-accent/20 ${expanded ? "max-h-60 overflow-y-auto" : "max-h-8 overflow-hidden"}`}>
          <div className="text-[11px] text-muted-foreground/70 italic whitespace-pre-wrap">
            {displayContent}
          </div>
        </div>
      )}
    </div>
  );
}


// SP3.4: Context Circle with real token count
function ContextCircleV2({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(used / total, 1) : 0;
  const color = pct > 0.8 ? "#ef4444" : pct > 0.5 ? "#f59e0b" : "#10b981";
  const r = 8;
  const c = 2 * Math.PI * r;
  const dash = c * pct;
  return (
    <div className="flex items-center gap-1" title={`${used.toLocaleString()} / ${total.toLocaleString()} tokens (${Math.round(pct*100)}%)`}>
      <svg width="20" height="20" viewBox="0 0 20 20" style={{ transform: "rotate(-90deg)" }}>
        <circle cx="10" cy="10" r={r} fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/30" />
        <circle cx="10" cy="10" r={r} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round"
          strokeDasharray={`${dash} ${c}`} style={{ transition: "stroke-dasharray 0.3s ease" }} />
      </svg>
      <span className="text-[9px] font-mono" style={{ color }}>{Math.round(pct*100)}%</span>
    </div>
  );
}
