import { useEffect, useRef, useCallback, useState, useMemo } from "react";
import type { Settings } from "../api/panel";
import { AgentClient } from "../api/agent";
import { GitHubClient } from "../api/github";
import { useChatStore } from "../state/chatStore";
import { useModelStore } from "../lib/model-store";
import { ChatMessageBubble } from "../components/ChatMessageBubble";
import { SessionSidebar } from "../components/SessionSidebar";
import { FileDrawer } from "../components/FileDrawer";
import { PanelDrawer, type PanelInvocation } from "../components/PanelDrawer";

export function AgentChat({ settings }: { settings: Settings }) {
  // Model store
  const openOverlay = useModelStore((s) => s.openOverlay);
  const selectedModelId = useModelStore((s) => s.selectedModelId);
  const selectedProviderName = useModelStore((s) => s.selectedProviderName);
  const providers = useModelStore((s) => s.providers);
  const selectedSlotId = useModelStore((s) => s.selectedSlotId);

  // Derive display info for selected model
  const modelDisplay = (() => {
    if (!selectedModelId) return null;
    for (const p of providers) {
      const m = p.models.find(
        (m) => m.id === selectedModelId || m.slotId === selectedSlotId,
      );
      if (m)
        return {
          label: m.displayName || m.id,
          color: p.color || "#5b8cff",
        };
    }
    return {
      label: selectedModelId.split("/").pop() || selectedModelId,
      color: "#5b8cff",
    };
  })();

  // The model ID to send to the backend. Prefer the physical slot ID
  // ("provider/model"), fall back to the logical ID. The backend resolves
  // either form (see _handle_agent_post).
  const effectiveModelId = selectedSlotId || selectedModelId;

  // Chat store — use SELECTIVE subscriptions so typing in the input doesn't
  // re-render the whole message list. Each useChatStore((s) => ...) only
  // re-renders when that specific slice changes.
  const sessions = useChatStore((s) => s.sessions);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.messages);
  const isBusy = useChatStore((s) => s.isBusy);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const status = useChatStore((s) => s.status);
  const inputText = useChatStore((s) => s.inputText);
  const error = useChatStore((s) => s.error);
  const effort = useChatStore((s) => s.effort);
  const webSearch = useChatStore((s) => s.webSearch);
  const deepResearch = useChatStore((s) => s.deepResearch);
  const mode = useChatStore((s) => s.mode);
  const files = useChatStore((s) => s.files);
  const fileDrawerOpen = useChatStore((s) => s.fileDrawerOpen);
  const agentSessionId = useChatStore((s) => s._agentSessionId);
  const panelDrawerOpen = useChatStore((s) => s.panelDrawerOpen);
  const panelInvocations = useChatStore((s) => s.panelInvocations);
  const sidebarOpen = useChatStore((s) => s.sidebarOpen);
  const cost = useChatStore((s) => s.cost);
  const lastUsage = useChatStore((s) => s.lastUsage);
  const queue = useChatStore((s) => s.queue);
  const isLoadingMessages = useChatStore((s) => s.isLoadingMessages);
  // Model verification fields
  const resolvedModel = useChatStore((s) => s.resolvedModel);
  const resolvedProvider = useChatStore((s) => s.resolvedProvider);
  const requestedModel = useChatStore((s) => s.requestedModel);
  // Workspace
  const workspaceId = useChatStore((s) => s.workspaceId);
  const setWorkspaceId = useChatStore((s) => s.setWorkspaceId);
  // Actions (stable references from zustand — don't cause re-renders)
  const setInputText = useChatStore((s) => s.setInputText);
  const setEffort = useChatStore((s) => s.setEffort);
  const setMode = useChatStore((s) => s.setMode);
  const toggleWebSearch = useChatStore((s) => s.toggleWebSearch);
  const toggleDeepResearch = useChatStore((s) => s.toggleDeepResearch);
  const setSidebarOpen = useChatStore((s) => s.setSidebarOpen);
  const setFileDrawerOpen = useChatStore((s) => s.setFileDrawerOpen);
  const setPanelDrawerOpen = useChatStore((s) => s.setPanelDrawerOpen);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const createSession = useChatStore((s) => s.createSession);
  const switchSession = useChatStore((s) => s.switchSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const stopGeneration = useChatStore((s) => s.stopGeneration);
  const dequeueMessage = useChatStore((s) => s.dequeueMessage);

  // Refs
  const clientRef = useRef(new AgentClient(settings));
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const lastUserMsgRef = useRef("");
  const stickToBottomRef = useRef(true);

  // Keep client fresh when settings change — useMemo so it only rebuilds
  // when settings actually change, NOT on every render.
  const client = useMemo(() => new AgentClient(settings), [settings]);
  clientRef.current = client;

  // Workspace list — fetched from the backend so the user can select which
  // sandbox the agent works in.
  const [workspaces, setWorkspaces] = useState<{ id: string; title: string; source_repo?: string | null }[]>([]);
  const [wsOpen, setWsOpen] = useState(false);
  const ghClient = useMemo(() => new GitHubClient(settings), [settings]);
  useEffect(() => {
    ghClient.listWorkspaces().then((r) => setWorkspaces(r.workspaces || [])).catch(() => {});
  }, [ghClient]);

  // Auto-scroll behavior: only stick to bottom if the user is already there.
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stickToBottomRef.current = atBottom;
  }, []);

  useEffect(() => {
    if (scrollRef.current && stickToBottomRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  // Load sessions on mount (auto-restores active session if any).
  useEffect(() => {
    loadSessions(clientRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle send — note the store handles queueing when busy.
  const handleSend = useCallback(
    async (text?: string) => {
      const msg = (text ?? inputText).trim();
      if (!msg) return;
      lastUserMsgRef.current = msg;
      await sendMessage(clientRef.current, msg, effectiveModelId || undefined);
    },
    [inputText, effectiveModelId, sendMessage],
  );

  // Handle stop
  const handleStop = useCallback(async () => {
    await stopGeneration(clientRef.current);
  }, [stopGeneration]);

  // Handle new session
  const handleNewSession = useCallback(async () => {
    await createSession(clientRef.current, effectiveModelId || undefined);
    inputRef.current?.focus();
  }, [createSession, effectiveModelId]);

  // Export conversation as markdown
  const handleExport = useCallback(() => {
    const lines: string[] = [];
    lines.push(`# doomalaysocreate Conversation Export`);
    lines.push(`Date: ${new Date().toISOString()}`);
    lines.push(`Model: ${resolvedModel || effectiveModelId || "unknown"}`);
    lines.push(`Provider: ${resolvedProvider || "unknown"}`);
    lines.push("");
    for (const msg of messages) {
      if (msg.role === "user") {
        lines.push(`## User`);
        lines.push(msg.content);
        lines.push("");
      } else if (msg.role === "assistant") {
        lines.push(`## Assistant`);
        lines.push(msg.content);
        lines.push("");
      } else if (msg.role === "thinking") {
        lines.push(`### Thinking`);
        lines.push(msg.content);
        lines.push("");
      } else if (msg.role === "tool") {
        lines.push(`### Tool: ${msg.toolName || "unknown"}`);
        lines.push("```");
        lines.push(msg.content);
        lines.push("```");
        lines.push("");
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `chat-${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [messages, resolvedModel, resolvedProvider, effectiveModelId]);

  // Handle keydown — Enter sends, Shift+Enter newlines. While busy, Enter
  // queues instead of being disabled.
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  // Auto-resize textarea
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInputText(e.target.value);
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    },
    [setInputText],
  );

  // Settings panel
  
  const [activePopover, setActivePopover] = useState<"effort" | "web" | "deep" | "mode" | null>(null);

  const running = isBusy && (status === "running" || status === "starting");
  const queueCount = queue.length;

  return (
    <div className="flex flex-col h-full relative bg-bg">
      {/* Header */}
      <header className="flex items-center gap-2 px-3 h-11 border-b border-border shrink-0 bg-surface/50">
        {/* Session menu toggle */}
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className={`p-1.5 rounded-md transition-colors ${
            sidebarOpen
              ? "text-accent bg-accent/10"
              : "text-muted-foreground hover:text-foreground hover:bg-surface2"
          }`}
          title="Chat sessions"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>

        {/* Active session title (truncated) */}
        <div className="text-[12.5px] font-medium text-foreground truncate max-w-[140px]">
          {activeSessionId
            ? sessions.find((s) => s.id === activeSessionId)?.title || "New Chat"
            : "New Chat"}
        </div>

        {/* Workspace selector — lets the user pick which sandbox the agent
            works in. Shows the current workspace title (or "No workspace")
            and a dropdown to switch. */}
        <div className="relative ml-auto">
          <button
            onClick={() => setWsOpen((v) => !v)}
            disabled={running}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-border hover:border-accent transition-colors text-[11.5px] disabled:opacity-50 disabled:cursor-not-allowed max-w-[160px]"
            title={workspaceId || "No workspace (ephemeral)"}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-muted-foreground">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
            <span className="truncate">
              {workspaceId
                ? (workspaces.find((w) => w.id === workspaceId)?.title || "Workspace")
                : "No workspace"}
            </span>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-muted-foreground shrink-0">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          {wsOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setWsOpen(false)} />
              <div className="absolute right-0 top-full mt-1 z-50 min-w-[200px] max-w-[280px] rounded-lg border border-border bg-surface shadow-lg overflow-hidden">
                <button
                  onClick={() => { setWorkspaceId(null); setWsOpen(false); }}
                  className={`w-full text-left px-3 py-2 text-[11.5px] hover:bg-accent/10 transition-colors ${!workspaceId ? "text-accent font-medium" : ""}`}
                >
                  No workspace (ephemeral)
                </button>
                {workspaces.map((ws) => (
                  <button
                    key={ws.id}
                    onClick={() => { setWorkspaceId(ws.id); setWsOpen(false); }}
                    className={`w-full text-left px-3 py-2 text-[11.5px] hover:bg-accent/10 transition-colors truncate ${workspaceId === ws.id ? "text-accent font-medium" : ""}`}
                  >
                    <div className="truncate">{ws.title}</div>
                    {ws.source_repo && <div className="text-[9px] text-muted-foreground truncate">{ws.source_repo}</div>}
                  </button>
                ))}
                {workspaces.length === 0 && (
                  <div className="px-3 py-2 text-[10px] text-muted-foreground">
                    No workspaces. Connect GitHub in the Workspaces tab.
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Model selector */}
        <button
          onClick={() => !running && openOverlay()}
          disabled={running}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-border hover:border-accent transition-colors text-[11.5px] disabled:opacity-50 disabled:cursor-not-allowed max-w-[200px]"
          title={selectedProviderName || "Select model"}
        >
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ background: modelDisplay?.color || "#5b8cff" }}
          />
          <span className="truncate text-accent">
            {modelDisplay?.label || "Select model"}
          </span>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-muted-foreground shrink-0">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        {/* Model verification badge — shows what the backend ACTUALLY resolved.
            If resolvedModel differs from requestedModel, show a warning so the
            user knows the model they picked isn't the one running. */}
        {resolvedModel && (
          <div className="flex flex-col items-end gap-0.5 ml-1" title={`Verified: ${resolvedProvider || "?"} → ${resolvedModel}`}>
            <span className="text-[9px] text-emerald-400/80 font-mono tabular-nums flex items-center gap-0.5">
              <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              {resolvedProvider || "unknown"}
            </span>
            {requestedModel && resolvedModel &&
             requestedModel.split("/").pop()?.toLowerCase() !== resolvedModel.split("/").pop()?.toLowerCase() && (
              <span className="text-[8px] text-amber-400/90 font-mono" title={`Requested ${requestedModel} but backend resolved to ${resolvedModel}`}>
                ⚠ redirected
              </span>
            )}
          </div>
        )}

        {/* Cost + token usage indicator with hover breakdown */}
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground tabular-nums">
          {cost != null && cost > 0 && (
            <span
              className="cursor-help"
              title={`Cost breakdown:\nTotal: $${cost.toFixed(4)}`}
            >
              <span className="text-amber-400/80">${cost.toFixed(4)}</span>
            </span>
          )}
          {lastUsage && (
            <span
              className="cursor-help flex items-center gap-1"
              title={`Token breakdown:\nInput: ${lastUsage.input_tokens.toLocaleString()}\nOutput: ${lastUsage.output_tokens.toLocaleString()}\nTotal: ${lastUsage.total_tokens.toLocaleString()}`}
            >
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="opacity-60">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
              </svg>
              {lastUsage.total_tokens.toLocaleString()}
            </span>
          )}
        </div>

        {/* Status indicator — shows what the agent is doing */}
        {running ? (
          <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
            <span className="flex gap-0.5">
              <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "0ms" }} />
              <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "120ms" }} />
              <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "240ms" }} />
            </span>
            <span className="capitalize status-pulse">{status === "starting" ? "starting…" : "working…"}</span>
          </div>
        ) : status === "error" ? (
          <div className="flex items-center gap-1 text-[10px] text-rose-400">
            <span className="size-1 rounded-full bg-rose-400" />
            <span>error</span>
          </div>
        ) : null}

        {/* Context usage circle — shows how much of the model's context is used */}
        {lastUsage && (
          <ContextCircle
            used={lastUsage.total_tokens}
            max={128000}
            title={`Context: ${lastUsage.total_tokens.toLocaleString()} / 128,000 tokens (${Math.round(lastUsage.total_tokens / 128000 * 100)}%)`}
          />
        )}

        {/* Files */}
        <button
          onClick={() => setFileDrawerOpen(!fileDrawerOpen)}
          className={`p-1.5 rounded-md transition-colors ${
            fileDrawerOpen
              ? "text-accent bg-accent/10"
              : "text-muted-foreground hover:text-foreground hover:bg-surface2"
          }`}
          title="Files"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
            <polyline points="13 2 13 9 20 9" />
          </svg>
        </button>

        <button
          onClick={() => setPanelDrawerOpen(!panelDrawerOpen)}
          className={`p-1.5 rounded-md transition-colors ${
            panelDrawerOpen
              ? "text-accent bg-accent/10"
              : "text-muted-foreground hover:text-foreground hover:bg-surface2"
          }`}
          title="Panel invocations"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </button>

        {running ? (
          <button
            onClick={handleStop}
            className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 rounded-md bg-red-500/15 text-red-300 hover:bg-red-500/25 transition-colors font-medium shrink-0"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="6" width="12" height="12" rx="1" />
            </svg>
            Stop
          </button>
        ) : (
          <button
            onClick={handleNewSession}
            className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 rounded-md bg-accent/15 text-accent hover:bg-accent/25 transition-colors font-medium shrink-0"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New
          </button>
        )}
        {messages.length > 0 && (
          <button
            onClick={handleExport}
            className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors shrink-0"
            title="Export conversation as markdown"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
          </button>
        )}
      </header>

      {/* Messages area */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto min-h-0 scroll-smooth"
      >
        {isLoadingMessages && messages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="flex items-center gap-2 text-muted-foreground text-[12px]">
              <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
              Loading chat…
            </div>
          </div>
        ) : messages.length === 0 && !isBusy ? (
          <EmptyState onSend={handleSend} />
        ) : (
          <div className="py-3 max-w-3xl mx-auto">
            {messages.map((msg) => (
              <ChatMessageBubble key={msg.id} message={msg} />
            ))}

            {/* Typing indicator — only when the latest message is a user
                message and we haven't received the first delta yet. */}
            {running &&
              messages.length > 0 &&
              messages[messages.length - 1].role === "user" && (
                <div className="px-4 py-2.5">
                  <div className="flex items-center gap-2.5">
                    <div className="shrink-0 w-7 h-7 rounded-md bg-accent/15 flex items-center justify-center">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5b8cff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M12 2a10 10 0 0 1 10 10c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2z" />
                        <path d="M12 16v-4" />
                        <path d="M12 8h.01" />
                      </svg>
                    </div>
                    <span className="flex gap-1 items-center h-4">
                      <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground animate-bounce" style={{ animationDelay: "300ms" }} />
                    </span>
                  </div>
                </div>
              )}
          </div>
        )}
      </div>

      {/* Queue indicator (1-up: shows pending messages while LLM is busy) */}
      {queueCount > 0 && (
        <div className="border-t border-border bg-accent/5 px-3 py-2 shrink-0">
          <div className="flex items-center gap-2 text-[11.5px] text-accent">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-pulse">
              <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
            </svg>
            <span className="font-medium">
              {queueCount} message{queueCount !== 1 ? "s" : ""} queued
            </span>
            <div className="flex-1 truncate text-muted-foreground/80">
              Next: {queue[0]?.text.slice(0, 80)}
              {queue[0] && queue[0].text.length > 80 ? "…" : ""}
            </div>
            <button
              onClick={() => queue.forEach((q) => dequeueMessage(q.id))}
              className="text-muted-foreground hover:text-foreground px-1.5 py-0.5 rounded hover:bg-surface2 transition-colors"
              title="Clear queue"
            >
              Clear
            </button>
          </div>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="border-t border-red-500/30 bg-red-500/10 px-4 py-2.5 text-[13px] text-red-300 flex items-center gap-3 shrink-0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
            <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span className="flex-1">{error}</span>
          <button
            onClick={() => useChatStore.setState({ error: null })}
            className="text-red-300/70 hover:text-red-200 px-2 py-0.5 text-[11px]"
          >
            dismiss
          </button>
          {lastUserMsgRef.current && (
            <button
              onClick={() => handleSend(lastUserMsgRef.current)}
              className="px-3 py-1 rounded-md border border-red-500/40 text-[11px] hover:bg-red-500/20 transition-colors shrink-0"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {/* Input area */}
      <div className="border-t border-border bg-surface/30 pt-2 pb-3 px-3 shrink-0">
        {/* Input row with compact icon settings */}
        <div className="flex items-end gap-1.5 max-w-3xl mx-auto">

          {/* Effort icon + dropdown */}
          <div className="relative shrink-0 mb-0.5">
            <button
              onClick={() => { setActivePopover(activePopover === "effort" ? null : "effort"); }}
              className={`p-2 rounded-lg transition-colors ${
                activePopover === "effort"
                  ? "text-accent bg-accent/10"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface2"
              }`}
              title={`Effort: ${effort}`}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
              </svg>
            </button>
            {activePopover === "effort" && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setActivePopover(null)} />
                <div className="absolute bottom-full left-0 mb-1 z-50 w-44 rounded-lg border border-border bg-surface shadow-xl p-1.5">
                  <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">Effort Level</div>
                  {(["low", "med", "high", "max"] as const).map((e) => (
                    <button
                      key={e}
                      onClick={() => { setEffort(e); setActivePopover(null); }}
                      className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                        effort === e ? "bg-accent/15 text-accent font-medium" : "text-muted-foreground hover:bg-surface2"
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="capitalize">{e}</span>
                        <span className="text-[9px] opacity-60">
                          {e === "low" ? "1 judge" : e === "med" ? "3 judges" : e === "high" ? "5 judges" : "all judges"}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>

          {/* Web search icon + dropdown */}
          <div className="relative shrink-0 mb-0.5">
            <button
              onClick={() => { setActivePopover(activePopover === "web" ? null : "web"); }}
              className={`p-2 rounded-lg transition-colors ${
                webSearch || activePopover === "web"
                  ? "text-accent bg-accent/10"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface2"
              }`}
              title="Web search"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
              </svg>
            </button>
            {activePopover === "web" && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setActivePopover(null)} />
                <div className="absolute bottom-full left-0 mb-1 z-50 w-52 rounded-lg border border-border bg-surface shadow-xl p-1.5">
                  <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">Web Search</div>
                  <button
                    onClick={() => { toggleWebSearch(); setActivePopover(null); }}
                    className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                      webSearch ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span>Enable web search</span>
                      <span className={`size-1.5 rounded-full ${webSearch ? "bg-accent" : "bg-muted-foreground/30"}`} />
                    </div>
                    <div className="text-[9px] opacity-60 mt-0.5">Agent can search the web during its turn</div>
                  </button>
                  <div className="text-[9px] text-muted-foreground/50 px-2 py-1 mt-1 border-t border-border/50">
                    Template: breadth-first topic discovery → batched sub-agent search
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Deep research icon + dropdown */}
          <div className="relative shrink-0 mb-0.5">
            <button
              onClick={() => { setActivePopover(activePopover === "deep" ? null : "deep"); }}
              className={`p-2 rounded-lg transition-colors ${
                deepResearch || activePopover === "deep"
                  ? "text-accent bg-accent/10"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface2"
              }`}
              title="Deep research"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s-8-4.5-8-11.8A8 8 0 0 1 12 2a8 8 0 0 1 8 8.2c0 7.3-8 11.8-8 11.8z" /><circle cx="12" cy="10" r="3" />
              </svg>
            </button>
            {activePopover === "deep" && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setActivePopover(null)} />
                <div className="absolute bottom-full left-0 mb-1 z-50 w-52 rounded-lg border border-border bg-surface shadow-xl p-1.5">
                  <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">Deep Research</div>
                  <button
                    onClick={() => { toggleDeepResearch(); setActivePopover(null); }}
                    className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                      deepResearch ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span>Enable deep research</span>
                      <span className={`size-1.5 rounded-full ${deepResearch ? "bg-accent" : "bg-muted-foreground/30"}`} />
                    </div>
                    <div className="text-[9px] opacity-60 mt-0.5">Extended reasoning + web ReAct loop</div>
                  </button>
                  <div className="text-[9px] text-muted-foreground/50 px-2 py-1 mt-1 border-t border-border/50">
                    Uses more tokens + longer timeouts for thorough analysis
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Mode icon + dropdown */}
          <div className="relative shrink-0 mb-0.5">
            <button
              onClick={() => { setActivePopover(activePopover === "mode" ? null : "mode"); }}
              className={`p-2 rounded-lg transition-colors ${
                activePopover === "mode"
                  ? "text-accent bg-accent/10"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface2"
              }`}
              title={`Mode: ${mode}`}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 9h6v6H9z" />
              </svg>
            </button>
            {activePopover === "mode" && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setActivePopover(null)} />
                <div className="absolute bottom-full left-0 mb-1 z-50 w-48 rounded-lg border border-border bg-surface shadow-xl p-1.5">
                  <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">Execution Mode</div>
                  {(["auto", "build", "plan"] as const).map((m) => (
                    <button
                      key={m}
                      onClick={() => { setMode(m); setActivePopover(null); }}
                      className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                        mode === m ? "bg-accent/15 text-accent font-medium" : "text-muted-foreground hover:bg-surface2"
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="capitalize">{m}</span>
                        {mode === m && <span className="text-[9px]">●</span>}
                      </div>
                      <div className="text-[9px] opacity-60 mt-0.5">
                        {m === "auto" ? "Execute autonomously" : m === "build" ? "Step-by-step with confirmation" : "Plan first, wait for approval"}
                      </div>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>

          <textarea
            ref={inputRef}
            value={inputText}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={
              isBusy
                ? "Queue another message… (Enter to queue, Shift+Enter for newline)"
                : "Ask the agent to build, edit, run, or pack something…"
            }
            className="flex-1 resize-none bg-surface2 border border-border rounded-xl px-3.5 py-2.5 text-[14.5px] leading-relaxed outline-none focus:border-accent min-h-[40px] max-h-[160px] transition-colors placeholder:text-muted-foreground/60"
          />

          <button
            onClick={() => (running ? handleStop() : handleSend())}
            disabled={!running && !inputText.trim()}
            className={`h-10 px-4 rounded-xl font-medium text-[13px] disabled:opacity-40 disabled:cursor-not-allowed shrink-0 transition-all flex items-center gap-1.5 ${
              running
                ? "bg-red-500/90 text-white hover:bg-red-500"
                : "bg-accent text-white hover:bg-accent/90"
            }`}
            title={running ? "Stop" : "Send (or queue if busy)"}
          >
            {running ? (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="6" y="6" width="12" height="12" rx="1" />
                </svg>
                Stop
              </>
            ) : isBusy ? (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="5" x2="12" y2="19" />
                  <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Queue
              </>
            ) : (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="22" y1="2" x2="10" y2="14" />
                  <polygon points="22 2 15 22 10 14 2 9 22 2" />
                </svg>
                Send
              </>
            )}
          </button>
        </div>

        {/* Keyboard shortcut hint */}
        <div className="flex items-center justify-center gap-3 mt-1 text-[9px] text-muted-foreground/40">
          <span><kbd className="px-1 py-0.5 rounded bg-surface2 border border-border/50 font-mono">Enter</kbd> to send</span>
          <span><kbd className="px-1 py-0.5 rounded bg-surface2 border border-border/50 font-mono">Shift+Enter</kbd> for newline</span>
          {isBusy && <span className="text-amber-400/60">Queue mode active</span>}
        </div>
      </div>

      {/* Overlays */}
      <SessionSidebar
        open={sidebarOpen}
        sessions={sessions}
        activeId={activeSessionId}
        isLoading={false}
        onSelect={(id) => {
          switchSession(clientRef.current, id);
          setSidebarOpen(false);
        }}
        onDelete={(id) => deleteSession(clientRef.current, id)}
        onRename={(id, title) => renameSession(clientRef.current, id, title)}
        onNew={handleNewSession}
        onClose={() => setSidebarOpen(false)}
      />

      <FileDrawer
        open={fileDrawerOpen}
        onClose={() => setFileDrawerOpen(false)}
        files={files}
        sessionId={agentSessionId}
        settings={settings}
        onRefresh={async () => {
          if (agentSessionId) {
            try {
              const f = await clientRef.current.files(agentSessionId);
              useChatStore.setState({ files: f.files });
            } catch { /* ignore */ }
          }
        }}
      />

      <PanelDrawer
        open={panelDrawerOpen}
        onClose={() => setPanelDrawerOpen(false)}
        invocations={panelInvocations as PanelInvocation[]}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState({ onSend }: { onSend: (text: string) => void }) {
  const suggestions = [
    { icon: "⚛️", text: "Build a React component that displays a data table with sorting and filtering" },
    { icon: "🐍", text: "Create a Python script that fetches data from an API and saves it to CSV" },
    { icon: "🐳", text: "Write a Dockerfile for a Node.js application with multi-stage build" },
    { icon: "🔄", text: "Set up a CI/CD pipeline configuration for running tests on every push" },
    { icon: "🔍", text: "Clone a GitHub repo and review the code for bugs and improvements" },
    { icon: "📊", text: "Analyze a dataset and create a visualization with matplotlib" },
  ];

  return (
    <div className="flex flex-col items-center justify-center h-full px-6 py-8 overflow-y-auto">
      <div className="max-w-lg w-full text-center">
        {/* Icon */}
        <div className="w-16 h-16 rounded-2xl bg-accent/10 flex items-center justify-center mx-auto mb-5 empty-state-icon">
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-accent"
          >
            <path d="M12 2a10 10 0 0 1 10 10c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2z" />
            <path d="M12 16v-4" />
            <path d="M12 8h.01" />
          </svg>
        </div>

        <h2 className="text-[18px] font-semibold text-foreground mb-2">
          What would you like to build?
        </h2>
        <p className="text-[13px] text-muted-foreground mb-6 leading-relaxed">
          The agent works in a private workspace and can build, edit, run, or
          pack your code. It has a real bash shell, file operations, web access,
          and can invoke the judge panel for critiques.
        </p>

        {/* Suggestions */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-left">
          {suggestions.map((s, i) => (
            <button
              key={i}
              onClick={() => onSend(s.text)}
              className="flex items-start gap-2.5 text-left text-[12px] px-3 py-2.5 rounded-xl border border-border hover:border-accent/50 hover:bg-accent/5 transition-all text-muted-foreground hover:text-foreground leading-relaxed card-hover"
            >
              <span className="text-base shrink-0">{s.icon}</span>
              <span>{s.text}</span>
            </button>
          ))}
        </div>

        {/* Tips */}
        <div className="mt-6 flex items-center justify-center gap-4 text-[10px] text-muted-foreground/50">
          <span className="flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded bg-surface2 border border-border/50 font-mono">Enter</kbd>
            to send
          </span>
          <span className="flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded bg-surface2 border border-border/50 font-mono">Shift+Enter</kbd>
            newline
          </span>
          <span>·</span>
          <span>Queue messages while agent works</span>
        </div>
      </div>
    </div>
  );
}

// Context usage circle — fills clockwise as the model's context window fills up
function ContextCircle({ used, max, title }: { used: number; max: number; title: string }) {
  const pct = Math.min(1, used / max);
  const deg = Math.round(pct * 360);
  const color = pct > 0.85 ? "#f59e0b" : pct > 0.7 ? "#eab308" : "#5b8cff";
  return (
    <div
      className="relative size-4 shrink-0 cursor-help"
      title={title}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" className="-rotate-90">
        <circle cx="8" cy="8" r="6" fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth="2" />
        <circle
          cx="8" cy="8" r="6" fill="none" stroke={color} strokeWidth="2"
          strokeDasharray={`${deg / 360 * 37.7} 37.7`}
          strokeLinecap="round"
          style={{ transition: "stroke-dasharray 0.3s ease" }}
        />
      </svg>
      {pct > 0.85 && (
        <span className="absolute -top-1 -right-1 size-1.5 rounded-full bg-amber-400 animate-pulse" />
      )}
    </div>
  );
}
