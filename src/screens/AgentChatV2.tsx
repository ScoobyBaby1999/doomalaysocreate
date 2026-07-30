/**
 * AgentChatV2 — Full chat screen with:
 * - Full-screen chat area (fills viewport minus header)
 * - Collapsible header with model select, context, price
 * - Session sidebar with delete/rename/metadata
 * - Tool bar (effort, web search, deep research) above input
 * - Expandable thinking bubbles
 * - Local persistence (survives refresh)
 */
import { useEffect, useRef, useCallback, useState, useMemo } from "react";
import type { Settings } from "../api/panel";
import { V2ChatClient } from "../api/v2chat";
import { useV2Chat, type V2Message } from "../state/v2chatStore";
import { useModelStore } from "../lib/model-store";
import { Markdown } from "../components/Markdown";
import {
  Send, Loader2, Plus, MessageSquare, AlertCircle,
  Brain, Globe, Telescope, Gauge, Trash2, Pencil, Check, X,
  ChevronDown, Download,
} from "lucide-react";

export function AgentChatV2({ settings }: { settings: Settings }) {
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
  const deleteSession = useV2Chat((s) => s.deleteSession);
  const renameSession = useV2Chat((s) => s.renameSession);
  const stopGeneration = useV2Chat((s) => s.stopGeneration);

  const selectedModelId = useModelStore((s) => s.selectedModelId);
  const selectedSlotId = useModelStore((s) => s.selectedSlotId);
  const providers = useModelStore((s) => s.providers);
  const openOverlay = useModelStore((s) => s.openOverlay);

  const [input, setInput] = useState("");
  // SP11.1: Use store-backed per-chat state
  const effort = useV2Chat((s) => s.currentEffort);
  const webSearch = useV2Chat((s) => s.currentWebSearch);
  const deepResearch = useV2Chat((s) => s.currentDeepResearch);
  const setChatState = useV2Chat((s) => s.setChatState);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [headerExpanded, setHeaderExpanded] = useState(false);
  const tokenCount = useV2Chat((s) => s.tokenCount);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const client = useMemo(() => {
    return new V2ChatClient(settings.baseUrl || "", async () => {
      if (settings.rotationSecret) {
        const { deriveToken } = await import("../api/token");
        return deriveToken(settings.rotationSecret);
      }
      return settings.token;
    });
  }, [settings.baseUrl, settings.token, settings.rotationSecret]);

  useEffect(() => { init(client); }, [client]);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const effectiveModelId = selectedSlotId || selectedModelId;

  const currentModelFromStore = useV2Chat((s) => s.currentModel);
  
  const modelInfo = useMemo(() => {
    // Try store model first, then selectedModelId
    const modelId = currentModelFromStore || selectedModelId;
    if (!modelId) return null;
    for (const p of providers) {
      const m = p.models.find((mm: any) => mm.id === modelId || mm.slotId === modelId || mm.id === selectedModelId || mm.slotId === selectedSlotId);
      if (m) return { label: m.displayName || m.id, provider: p.displayName, color: p.color, ctx: m.contextLength || 131072 };
    }
    return { label: selectedModelId?.split("/").pop() || "Select model", provider: "", color: "#a855f7", ctx: 131072 };
  }, [providers, selectedModelId, selectedSlotId]);

  const effortLevels = useMemo(() => {
    if (!selectedSlotId && !selectedModelId) return [];
    for (const p of providers) {
      const m = p.models.find((m: any) => m.slotId === selectedSlotId || m.id === selectedModelId);
      if (m?.attributes?.effort_levels) return m.attributes.effort_levels;
    }
    return [];
  }, [providers, selectedModelId, selectedSlotId]);

  const handleSend = useCallback(async () => {
    if (!input.trim() || isBusy) return;
    const text = input.trim();
    setInput("");
    await sendMessage(client, text);  // SP11.1: opts come from store state
  }, [input, isBusy, client, sendMessage, effectiveModelId, effort, webSearch, deepResearch]);

  const handleNewChat = useCallback(async () => {
    await createSession(client, effectiveModelId || undefined);
    inputRef.current?.focus();
  }, [client, createSession, effectiveModelId]);

  if (!settings.baseUrl && !settings.token && !settings.rotationSecret) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center">
        <MessageSquare className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground">Configure settings to start chatting.</p>
      </div>
    );
  }

  const activeSession = sessions.find(s => s.id === activeSessionId);

  return (
    <div className="flex h-full overflow-hidden">
      {/* Session sidebar */}
      <SessionSidebar
        sessions={sessions}
        activeId={activeSessionId}
        onSelect={(id) => { switchSession(client, id); setSidebarOpen(false); }}
        onNew={handleNewChat}
        onDelete={(id) => deleteSession(client, id)}
        onRename={renameSession}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* Chat area — fills remaining space */}
      <div className="flex-1 flex flex-col min-w-0 h-full">
        {/* Compact header (collapsible) */}
        <div className="shrink-0 border-b border-border/30 bg-surface/40 backdrop-blur">
          {/* Row 1: always visible */}
          <div className="flex items-center gap-2 px-3 py-1.5">
            <button onClick={() => setSidebarOpen(!sidebarOpen)} className="lg:hidden touch-target p-1 rounded-lg hover:bg-muted/40">
              <MessageSquare className="size-4" />
            </button>
            <button onClick={() => openOverlay()} className="flex items-center gap-1.5 px-2.5 h-7 rounded-full bg-accent/10 border border-accent/30 text-[11px] font-medium hover:bg-accent/20 transition-colors truncate">
              <span className="size-2 rounded-full shrink-0" style={{ backgroundColor: modelInfo?.color || "#a855f7" }} />
              <span className="truncate max-w-[100px]">{modelInfo?.label || "Select model"}</span>
            </button>
            {effort && (
              <span className="flex items-center gap-1 px-1.5 h-6 rounded-full bg-purple-500/10 text-[9px] text-purple-400">
                <Gauge className="size-2.5" />{effort}
              </span>
            )}
            {webSearch && <span className="flex items-center gap-1 px-1.5 h-6 rounded-full bg-blue-500/10 text-[9px] text-blue-400"><Globe className="size-2.5" />Web</span>}
            {deepResearch && <span className="flex items-center gap-1 px-1.5 h-6 rounded-full bg-teal-500/10 text-[9px] text-teal-400"><Telescope className="size-2.5" />Deep</span>}
            <div className="ml-auto flex items-center gap-2">
              <ContextCircle used={tokenCount} total={modelInfo?.ctx || 131072} />
              <button onClick={() => setHeaderExpanded(!headerExpanded)} className="touch-target p-1 rounded-lg hover:bg-muted/40">
                <ChevronDown className={`size-3.5 transition-transform ${headerExpanded ? "rotate-180" : ""}`} />
              </button>
            </div>
          </div>
          {/* Row 2: expandable */}
          {headerExpanded && (
            <div className="px-3 pb-2 flex items-center gap-3 text-[10px] text-muted-foreground border-t border-border/20 pt-1.5">
              {activeSession && <span>Session: {activeSession.title}</span>}
              {modelInfo?.provider && <span>· Provider: {modelInfo.provider}</span>}
              <span>· Context: {modelInfo?.ctx ? `${Math.round(modelInfo.ctx/1000)}k` : "128k"}</span>
              <button onClick={() => exportChat(messages, activeSession?.title)} className="ml-auto flex items-center gap-1 hover:text-foreground">
                <Download className="size-3" /> Export
              </button>
            </div>
          )}
        </div>

        {/* Error */}
        {error && (
          <div className="px-3 py-1.5 bg-red-500/10 border-b border-red-500/20 text-[11px] text-red-300 flex items-center gap-2 shrink-0">
            <AlertCircle className="size-3 shrink-0" />
            <span className="flex-1 truncate">{error}</span>
            <button onClick={() => useV2Chat.setState({ error: null })} className="text-red-300/60 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Messages — fills remaining space, scrollable */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-4 space-y-3 min-h-0">
          {messages.length === 0 && !isBusy ? (
            <div className="flex flex-col items-center justify-center h-full text-center gap-3">
              <div className="size-12 rounded-full bg-accent/10 flex items-center justify-center">
                <Brain className="size-6 text-accent" />
              </div>
              <p className="text-sm text-muted-foreground">Send a message to start chatting</p>
              {modelInfo && <p className="text-[10px] text-muted-foreground/60">Using {modelInfo.label}</p>}
            </div>
          ) : (
            messages.map((msg) => <MessageBubble key={msg.id} msg={msg} />)
          )}
          {isBusy && messages.length > 0 && !messages[messages.length - 1]?.isStreaming && (
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground px-2">
              <Loader2 className="size-3 animate-spin" /> working...
            </div>
          )}
        </div>

        {/* Queue indicator */}
        {queue.length > 0 && (
          <div className="px-3 py-0.5 text-[9px] text-accent flex items-center gap-1 border-t border-border/20 shrink-0">
            <Loader2 className="size-2.5 animate-spin" /> {queue.length} queued
          </div>
        )}

        {/* Input area — above the footer */}
        <div className="shrink-0 border-t border-border/30 p-2.5 bg-surface/40">
          {/* Tool bar */}
          <div className="flex items-center gap-1 mb-1.5">
            {effortLevels.length > 0 && (
              <button
                onClick={() => {
                  const idx = effortLevels.indexOf(effort || "");
                  const next = idx < effortLevels.length - 1 ? effortLevels[idx + 1] : effortLevels[0];
                  setChatState({ effort: effort === next ? null : next });
                }}
                className={`flex items-center gap-1 px-1.5 h-6 rounded-full text-[9px] ${effort ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-muted/40"}`}
              >
                <Gauge className="size-2.5" />{effort || "effort"}
              </button>
            )}
            <button onClick={() => { setChatState({ webSearch: !webSearch, deepResearch: false }); }}
              className={`flex items-center gap-1 px-1.5 h-6 rounded-full text-[9px] ${webSearch ? "bg-blue-500/15 text-blue-400" : "text-muted-foreground hover:bg-muted/40"}`}>
              <Globe className="size-2.5" />Web
            </button>
            <button onClick={() => { setChatState({ deepResearch: !deepResearch }); if (webSearch) setChatState({ webSearch: false }); }}
              className={`flex items-center gap-1 px-1.5 h-6 rounded-full text-[9px] ${deepResearch ? "bg-teal-500/15 text-teal-400" : "text-muted-foreground hover:bg-muted/40"}`}
              title="Deep research: agent searches multiple sources and synthesizes a comprehensive answer">
              <Telescope className="size-2.5" />Deep
            </button>
            {(effort || webSearch || deepResearch) && (
              <button onClick={() => { setChatState({ effort: null, webSearch: false, deepResearch: false }); }}
                className="px-1.5 h-6 rounded-full text-[9px] text-muted-foreground hover:text-foreground">Reset</button>
            )}
          </div>
          {/* Input + send */}
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setInput(e.target.value)}
              onKeyDown={(e: React.KeyboardEvent<HTMLTextAreaElement>) => {
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); handleSend(); }
              }}
              placeholder={`Message ${modelInfo?.label || "AI"}...`}
              rows={1}
              className="flex-1 min-h-[36px] max-h-32 resize-none text-sm rounded-2xl bg-background border border-border/40 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-accent/30"
              disabled={isBusy && queue.length >= 3}
            />
            <button
              onClick={isBusy ? stopGeneration : handleSend}
              disabled={!isBusy && !input.trim()}
              className="shrink-0 rounded-full size-9 flex items-center justify-center bg-accent text-white hover:bg-accent/90 transition-colors disabled:opacity-40"
            >
              {isBusy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
            </button>
          </div>
          <div className="text-[8px] text-muted-foreground mt-0.5 px-0.5">Enter for newline · Ctrl+Enter to send</div>
        </div>
      </div>
    </div>
  );
}

// === Session Sidebar ===
function SessionSidebar({ sessions, activeId, onSelect, onNew, onDelete, onRename, open, onToggle }: {
  sessions: any[]; activeId: string | null;
  onSelect: (id: string) => void; onNew: () => void;
  onDelete: (id: string) => void; onRename: (id: string, title: string) => void;
  open: boolean; onToggle: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={onToggle} />}
      <aside className={`${open ? "translate-x-0" : "-translate-x-full lg:translate-x-0"} fixed lg:sticky top-0 z-40 lg:z-0 h-full w-56 shrink-0 border-r border-border/30 bg-surface/80 backdrop-blur transition-transform duration-200 flex flex-col`}>
        <div className="p-2 border-b border-border/20">
          <button onClick={onNew} className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg border border-border/40 text-[11px] hover:bg-accent/10 transition-colors">
            <Plus className="size-3.5" /> New Chat
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1 space-y-0.5">
          {sessions.map((s) => (
            <div key={s.id} className={`group rounded-lg ${s.id === activeId ? "bg-accent/15" : "hover:bg-muted/30"}`}>
              {editingId === s.id ? (
                <div className="flex items-center gap-1 px-1.5 py-1">
                  <input
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") { onRename(s.id, editTitle); setEditingId(null); } }}
                    className="flex-1 h-6 text-[11px] bg-background border border-border/40 rounded px-1.5"
                    autoFocus
                  />
                  <button onClick={() => { onRename(s.id, editTitle); setEditingId(null); }} className="text-emerald-500 hover:text-emerald-400">
                    <Check className="size-3" />
                  </button>
                  <button onClick={() => setEditingId(null)} className="text-muted-foreground hover:text-foreground">
                    <X className="size-3" />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => onSelect(s.id)}
                  className="w-full text-left px-2 py-1.5"
                >
                  <div className="flex items-center gap-1">
                    <span className={`text-[11px] truncate flex-1 ${s.id === activeId ? "text-accent font-medium" : "text-foreground/80"}`}>
                      {s.title || "Untitled"}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 mt-0.5">
                    {s.model && <span className="text-[8px] text-muted-foreground/60 truncate max-w-[80px]">{s.model.split("/").pop()}</span>}
                    <span className="text-[8px] text-muted-foreground/40">{new Date(s.updated_at || s.createdAt || Date.now()).toLocaleDateString()}</span>
                  </div>
                </button>
              )}
              {editingId !== s.id && (
                <div className="flex items-center gap-0.5 px-2 pb-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button onClick={(e) => { e.stopPropagation(); setEditingId(s.id); setEditTitle(s.title || ""); }}
                    className="p-0.5 rounded text-muted-foreground hover:text-foreground">
                    <Pencil className="size-2.5" />
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); if (confirm("Delete this chat?")) onDelete(s.id); }}
                    className="p-0.5 rounded text-muted-foreground hover:text-destructive">
                    <Trash2 className="size-2.5" />
                  </button>
                </div>
              )}
            </div>
          ))}
          {sessions.length === 0 && (
            <div className="text-[10px] text-muted-foreground/40 px-2 py-4 text-center">No chats yet</div>
          )}
        </div>
      </aside>
    </>
  );
}

// === Message Bubble ===
function MessageBubble({ msg }: { msg: V2Message }) {
  const isUser = msg.role === "user";
  const isThinking = msg.role === "thinking";
  const isTool = msg.role === "tool" || msg.role === "tool_result";

  if (isThinking) return <ThinkingBubble msg={msg} />;

  if (isTool) {
    return <ToolBubble msg={msg} />;
  }

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${
        isUser
          ? "bg-gradient-to-br from-purple-600 to-purple-700 text-white rounded-br-md"
          : "bg-muted/40 border border-border/20 rounded-bl-md"
      }`}>
        {isUser ? (
          <div className="whitespace-pre-wrap">{msg.content}</div>
        ) : (
          <Markdown text={msg.content || (msg.isStreaming ? "..." : "")} />
        )}
        {msg.isStreaming && <span className="inline-block w-1 h-3 bg-accent animate-pulse ml-0.5" />}
      </div>
    </div>
  );
}

// === Thinking Bubble (expandable) ===
function ThinkingBubble({ msg }: { msg: V2Message }) {
  const [expanded, setExpanded] = useState(true);
  const hasContent = msg.content && msg.content.trim().length > 0;
  return (
    <div className="px-3 py-0.5">
      <button onClick={() => setExpanded(!expanded)} className="flex items-center gap-1 text-[9px] text-muted-foreground hover:text-foreground">
        <Brain className="size-2.5" />
        {msg.isStreaming ? "thinking..." : "thought process"}
        {hasContent && msg.content.length > 200 && <span className="text-[8px]">({expanded ? "collapse" : "expand"})</span>}
      </button>
      {hasContent && (
        <div className={`mt-0.5 pl-3 border-l border-accent/20 ${expanded ? "max-h-48 overflow-y-auto" : "max-h-6 overflow-hidden"}`}>
          <div className="text-[10px] text-muted-foreground/60 italic whitespace-pre-wrap">{msg.content}</div>
        </div>
      )}
    </div>
  );
}

// === Context Circle ===
function ContextCircle({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(used / total, 1) : 0;
  const color = pct > 0.8 ? "#ef4444" : pct > 0.5 ? "#f59e0b" : "#10b981";
  const r = 7;
  const c = 2 * Math.PI * r;
  return (
    <div className="flex items-center gap-1" title={`${used.toLocaleString()} / ${total.toLocaleString()} tokens`}>
      <svg width="18" height="18" viewBox="0 0 18 18" style={{ transform: "rotate(-90deg)" }}>
        <circle cx="9" cy="9" r={r} fill="none" stroke="currentColor" strokeWidth="1.5" className="text-muted/30" />
        <circle cx="9" cy="9" r={r} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round"
          strokeDasharray={`${c * pct} ${c}`} style={{ transition: "stroke-dasharray 0.3s" }} />
      </svg>
      <span className="text-[8px] font-mono" style={{ color }}>{Math.round(pct * 100)}%</span>
    </div>
  );
}

// === Export Chat ===
function exportChat(messages: V2Message[], title: string | undefined) {
  const lines: string[] = [];
  lines.push(`# ${title || "Chat Export"}`);
  lines.push(`Date: ${new Date().toISOString()}`);
  lines.push("");
  for (const msg of messages) {
    if (msg.role === "user") { lines.push(`## User\n${msg.content}\n`); }
    else if (msg.role === "assistant") { lines.push(`## Assistant\n${msg.content}\n`); }
    else if (msg.role === "thinking") { lines.push(`### Thinking\n${msg.content}\n`); }
    else if (msg.role === "tool") { lines.push(`### Tool: ${msg.toolName}\n${msg.content}\n`); }
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${title || "chat"}.md`;
  a.click();
  URL.revokeObjectURL(url);
}


// === Expandable Tool Bubble ===
function ToolBubble({ msg }: { msg: V2Message }) {
  const [expanded, setExpanded] = useState(false);
  const isResult = msg.role === "tool_result";
  const hasContent = msg.content && msg.content.trim().length > 0;
  const preview = hasContent ? msg.content.slice(0, 60) + (msg.content.length > 60 ? "..." : "") : "";
  
  return (
    <div className="px-3 py-0.5">
      <button
        onClick={() => hasContent && setExpanded(!expanded)}
        className={`flex items-center gap-1.5 text-[9px] w-full text-left ${hasContent ? "hover:text-foreground cursor-pointer" : ""}`}
      >
        <span className={`px-1.5 py-0.5 rounded font-mono ${isResult ? (msg.isError ? "bg-red-500/20 text-red-400" : "bg-emerald-500/20 text-emerald-400") : "bg-blue-500/20 text-blue-400"}`}>
          {isResult ? "result" : (msg.toolName || "tool")}
        </span>
        <span className="truncate flex-1 text-muted-foreground">{preview || "(empty)"}</span>
        {hasContent && <span className="text-[8px] text-muted-foreground/50 shrink-0">{expanded ? "▼" : "▶"}</span>}
      </button>
      {hasContent && expanded && (
        <div className="mt-0.5 ml-4 p-1.5 rounded bg-muted/20 border border-border/20 max-h-40 overflow-y-auto">
          <pre className="text-[9px] font-mono whitespace-pre-wrap break-words text-muted-foreground/80">{msg.content}</pre>
        </div>
      )}
    </div>
  );
}
