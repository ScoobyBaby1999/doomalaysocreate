import { useEffect, useRef, useCallback, useState } from "react";
import type { Settings } from "../api/panel";
import { AgentClient } from "../api/agent";
import { useChatStore } from "../state/chatStore";
import { useModelStore } from "../lib/model-store";
import { ChatMessageBubble } from "../components/ChatMessageBubble";
import { SessionSidebar } from "../components/SessionSidebar";
import { FileDrawer } from "../components/FileDrawer";
import { PanelDrawer, type PanelInvocation } from "../components/PanelDrawer";
import { GitStatus } from "../components/GitStatus";

const EFFORTS: Array<"low" | "med" | "high" | "max"> = ["low", "med", "high", "max"];

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
      const m = p.models.find((m) => m.id === selectedModelId || m.slotId === selectedSlotId);
      if (m) return { label: m.displayName || m.id, color: p.color || "#5b8cff" };
    }
    return { label: selectedModelId.split("/").pop() || selectedModelId, color: "#5b8cff" };
  })();

  // Get the actual model ID to send to backend (physical slot ID preferred)
  const effectiveModelId = selectedSlotId || selectedModelId;

  // Chat store
  const store = useChatStore();
  const {
    sessions,
    activeSessionId,
    messages,
    isBusy,
    isStreaming,
    status,
    inputText,
    error,
    effort,
    webSearch,
    deepResearch,
    files,
    fileDrawerOpen,
    panelDrawerOpen,
    panelInvocations,
    sidebarOpen,
    cost,
    setInputText,
    setEffort,
    toggleWebSearch,
    toggleDeepResearch,
    setSidebarOpen,
    setFileDrawerOpen,
    setPanelDrawerOpen,
    loadSessions,
    createSession,
    switchSession,
    deleteSession,
    sendMessage,
    stopGeneration,
  } = store;

  // Refs
  const clientRef = useRef(new AgentClient(settings));
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const lastUserMsgRef = useRef("");

  // Keep client fresh
  clientRef.current = new AgentClient(settings);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  // Load sessions on mount
  useEffect(() => {
    loadSessions(clientRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle send
  const handleSend = useCallback(
    async (text?: string) => {
      const msg = (text ?? inputText).trim();
      if (!msg || isBusy) return;
      lastUserMsgRef.current = msg;
      await sendMessage(clientRef.current, msg, effectiveModelId || undefined);
    },
    [inputText, isBusy, effectiveModelId, sendMessage]
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

  // Handle keydown
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  // Auto-resize textarea
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInputText(e.target.value);
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    },
    [setInputText]
  );

  // Settings panel
  const [settingsOpen, setSettingsOpen] = useState(false);

  const running = isBusy && (status === "running" || status === "starting");

  return (
    <div className="flex flex-col h-full relative bg-background">
      {/* Header */}
      <header className="flex items-center gap-2 px-3 h-11 border-b border-border shrink-0 bg-card/50">
        {/* Session menu toggle */}
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className={`p-1.5 rounded-md transition-colors ${
            sidebarOpen ? "text-accent bg-accent/10" : "text-muted-foreground hover:text-foreground hover:bg-muted"
          }`}
          title="Chat sessions"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>

        {/* Model selector */}
        <button
          onClick={() => !running && openOverlay()}
          disabled={running}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-border hover:border-accent transition-colors text-xs disabled:opacity-50 disabled:cursor-not-allowed max-w-[200px]"
        >
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ background: modelDisplay?.color || "#5b8cff" }}
          />
          <span className="truncate text-accent">
            {modelDisplay?.label || "Select model"}
          </span>
        </button>

        {/* Cost indicator */}
        {cost != null && (
          <span className="text-[10px] text-muted-foreground tabular-nums">
            ${cost.toFixed(4)}
          </span>
        )}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Status */}
        {running && (
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
            <span className="capitalize">{status}</span>
          </div>
        )}

        {/* Action buttons */}
        <button
          onClick={() => setFileDrawerOpen(!fileDrawerOpen)}
          className={`p-1.5 rounded-md transition-colors ${
            fileDrawerOpen ? "text-accent bg-accent/10" : "text-muted-foreground hover:text-foreground hover:bg-muted"
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
            panelDrawerOpen ? "text-accent bg-accent/10" : "text-muted-foreground hover:text-foreground hover:bg-muted"
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
            className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 rounded-md bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors font-medium"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="6" width="12" height="12" rx="1" />
            </svg>
            Stop
          </button>
        ) : (
          <button
            onClick={handleNewSession}
            className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 rounded-md bg-accent/10 text-accent hover:bg-accent/20 transition-colors font-medium"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New
          </button>
        )}
      </header>

      {/* Messages area */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto min-h-0 scroll-smooth"
      >
        {messages.length === 0 && !isBusy ? (
          <EmptyState onSend={handleSend} />
        ) : (
          <div className="py-4 space-y-1">
            {messages.map((msg) => (
              <ChatMessageBubble key={msg.id} message={msg} />
            ))}

            {/* Typing indicator */}
            {running && messages.length > 0 && messages[messages.length - 1].role === "user" && (
              <div className="px-4 py-3">
                <div className="flex items-center gap-2 text-muted-foreground">
                  <span className="flex gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "0ms" }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "150ms" }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "300ms" }} />
                  </span>
                  <span className="text-[12px]">Thinking...</span>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div className="border-t border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive flex items-center gap-3 shrink-0">
          <span className="flex-1">{error}</span>
          {lastUserMsgRef.current && (
            <button
              onClick={() => handleSend(lastUserMsgRef.current)}
              className="px-3 py-1 rounded-md border border-destructive/40 text-xs hover:bg-destructive/20 transition-colors shrink-0"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {/* Input area */}
      <div className="border-t border-border bg-card/50 pt-2 pb-3 px-3 shrink-0">
        {/* Quick settings */}
        {settingsOpen && (
          <div className="flex items-center gap-1.5 px-1 pb-2 overflow-x-auto">
            {EFFORTS.map((e) => (
              <button
                key={e}
                onClick={() => setEffort(e)}
                className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors shrink-0 ${
                  effort === e
                    ? "border-accent text-accent bg-accent/10"
                    : "border-border text-muted-foreground hover:border-accent/50"
                }`}
              >
                {e}
              </button>
            ))}
            <div className="w-px h-4 bg-border mx-1" />
            <button
              onClick={toggleWebSearch}
              className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors shrink-0 flex items-center gap-1 ${
                webSearch
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted-foreground hover:border-accent/50"
              }`}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
              </svg>
              Web
            </button>
            <button
              onClick={toggleDeepResearch}
              className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors shrink-0 flex items-center gap-1 ${
                deepResearch
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted-foreground hover:border-accent/50"
              }`}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s-8-4.5-8-11.8A8 8 0 0 1 12 2a8 8 0 0 1 8 8.2c0 7.3-8 11.8-8 11.8z" /><circle cx="12" cy="10" r="3" />
              </svg>
              Deep
            </button>
          </div>
        )}

        {/* Input row */}
        <div className="flex items-end gap-2">
          <button
            onClick={() => setSettingsOpen((p) => !p)}
            className={`p-2 rounded-lg transition-colors shrink-0 mb-0.5 ${
              settingsOpen
                ? "text-accent bg-accent/10"
                : "text-muted-foreground hover:text-foreground hover:bg-muted"
            }`}
            title="Settings"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>

          <textarea
            ref={inputRef}
            value={inputText}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder="Ask the agent to build, edit, run, or pack something..."
            disabled={isBusy}
            className="flex-1 resize-none bg-muted border border-border rounded-xl px-3.5 py-2.5 text-[15px] leading-relaxed outline-none focus:border-accent disabled:opacity-50 min-h-[40px] max-h-[120px] transition-colors"
          />

          <button
            onClick={() => (running ? handleStop() : handleSend())}
            disabled={!running && (isBusy || !inputText.trim())}
            className={`h-10 px-4 rounded-xl font-medium text-sm disabled:opacity-40 disabled:cursor-not-allowed shrink-0 transition-all ${
              running
                ? "bg-destructive/90 text-destructive-foreground hover:bg-destructive"
                : "bg-accent text-accent-foreground hover:bg-accent/90"
            }`}
          >
            {running ? (
              <span className="flex items-center gap-1.5">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="6" y="6" width="12" height="12" rx="1" />
                </svg>
                Stop
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="22" y1="2" x2="10" y2="14" />
                  <polygon points="22 2 15 22 10 14 2 9 22 2" />
                </svg>
                Send
              </span>
            )}
          </button>
        </div>
      </div>

      {/* Overlays */}
      <SessionSidebar
        open={sidebarOpen}
        sessions={sessions}
        activeId={activeSessionId}
        isLoading={false}
        onSelect={(id) => switchSession(clientRef.current, id)}
        onDelete={(id) => deleteSession(clientRef.current, id)}
        onNew={handleNewSession}
        onClose={() => setSidebarOpen(false)}
      />

      <FileDrawer
        open={fileDrawerOpen}
        onClose={() => setFileDrawerOpen(false)}
        files={files}
        sessionId={null}
        settings={settings}
        onRefresh={() => {}}
      />

      <PanelDrawer
        open={panelDrawerOpen}
        onClose={() => setPanelDrawerOpen(false)}
        invocations={panelInvocations}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState({ onSend }: { onSend: (text: string) => void }) {
  const suggestions = [
    "Build a React component that displays a data table with sorting and filtering",
    "Create a Python script that fetches data from an API and saves it to CSV",
    "Write a Dockerfile for a Node.js application with multi-stage build",
    "Set up a CI/CD pipeline configuration for running tests on every push",
  ];

  return (
    <div className="flex flex-col items-center justify-center h-full px-6 py-12">
      <div className="max-w-md w-full text-center">
        {/* Icon */}
        <div className="w-14 h-14 rounded-2xl bg-accent/10 flex items-center justify-center mx-auto mb-5">
          <svg
            width="28"
            height="28"
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

        <h2 className="text-lg font-semibold text-foreground mb-2">
          What would you like to build?
        </h2>
        <p className="text-sm text-muted-foreground mb-8 leading-relaxed">
          The agent works in a private workspace and can build, edit, run, or
          pack your code. Files appear in the file drawer.
        </p>

        {/* Suggestions */}
        <div className="space-y-2 text-left">
          {suggestions.map((s, i) => (
            <button
              key={i}
              onClick={() => onSend(s)}
              className="w-full text-left text-[13px] px-4 py-3 rounded-xl border border-border hover:border-accent/50 hover:bg-accent/5 transition-all text-muted-foreground hover:text-foreground leading-relaxed"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
