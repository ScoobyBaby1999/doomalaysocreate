import { useEffect, useRef, useCallback, useState, useMemo } from "react";
import type { Settings } from "../api/panel";
import { AgentClient } from "../api/agent";
import { GitHubClient } from "../api/github";
import { useChatStore } from "../state/chatStore";
import { useModelStore } from "../lib/model-store";
import { isFreeModel } from "../lib/providers/family";
import { ChatMessageBubble } from "../components/ChatMessageBubble";
import { SessionSidebar } from "../components/SessionSidebar";
import { FileDrawer } from "../components/FileDrawer";
import { PanelDrawer, type PanelInvocation } from "../components/PanelDrawer";
import { ContextCircle } from "../components/ContextCircle";
import { PriceGauge } from "../components/PriceGauge";
import { ToolIcons } from "../components/ToolIcons";
import { QueueMonitor } from "../components/QueueMonitor";
import { TemplateLibrary } from "../components/TemplateLibrary";

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
  const webTemplate = useChatStore((s) => s.webTemplate);
  const deepTemplate = useChatStore((s) => s.deepTemplate);
  const judge = useChatStore((s) => s.judge);
  const busyMode = useChatStore((s) => s.busyMode);
  const files = useChatStore((s) => s.files);
  const fileDrawerOpen = useChatStore((s) => s.fileDrawerOpen);
  const agentSessionId = useChatStore((s) => s._agentSessionId);
  const panelDrawerOpen = useChatStore((s) => s.panelDrawerOpen);
  const panelInvocations = useChatStore((s) => s.panelInvocations);
  const sidebarOpen = useChatStore((s) => s.sidebarOpen);
  const queueMonitorOpen = useChatStore((s) => s.queueMonitorOpen);
  const sessionCost = useChatStore((s) => s.sessionCost);
  const lastUsage = useChatStore((s) => s.lastUsage);
  const queue = useChatStore((s) => s.queue);
  const jobs = useChatStore((s) => s.jobs);
  const suggestions = useChatStore((s) => s.suggestions);
  const pinnedSessionIds = useChatStore((s) => s.pinnedSessionIds);
  const isLoadingMessages = useChatStore((s) => s.isLoadingMessages);
  // Model verification fields
  const resolvedModel = useChatStore((s) => s.resolvedModel);
  const resolvedProvider = useChatStore((s) => s.resolvedProvider);
  const requestedModel = useChatStore((s) => s.requestedModel);
  // Workspace
  const workspaceId = useChatStore((s) => s.workspaceId);
  const setWorkspaceId = useChatStore((s) => s.setWorkspaceId);

  // Template Library overlay
  const templateLibraryOpen = useChatStore((s) => s.templateLibraryOpen);
  const templateLibraryKind = useChatStore((s) => s.templateLibraryKind);
  const templateLibraryTab = useChatStore((s) => s.templateLibraryTab);
  const openTemplateLibrary = useChatStore((s) => s.openTemplateLibrary);
  const closeTemplateLibrary = useChatStore((s) => s.closeTemplateLibrary);
  const applyTemplate = useChatStore((s) => s.applyTemplate);

  // ── Derived model info ───────────────────────────────────────────────
  // Find the selected model in the providers list so we can read its REAL
  // contextLength (was hardcoded 128000) + capabilities (for ToolIcons
  // dimming) + free status (for PriceGauge).
  const selectedModelInfo = useMemo(() => {
    if (!selectedModelId && !selectedSlotId) return null;
    for (const p of providers) {
      const m = p.models.find(
        (m) =>
          m.id === selectedModelId ||
          m.slotId === selectedSlotId ||
          (selectedSlotId && m.slotId === selectedSlotId),
      );
      if (m) {
        const caps = m.attributes?.capabilities || [];
        return {
          contextLength: m.contextLength || 128000,
          capabilities: {
            effort: caps.includes("effort"),
            webSearch: caps.includes("webSearch") || caps.includes("web_search"),
            deepResearch: caps.includes("deepResearch") || caps.includes("deep_research"),
            extendedThinking:
              caps.includes("extendedThinking") || caps.includes("extended_thinking"),
          },
          isFree: isFreeModel(m.id) || isFreeModel(m.slotId || ""),
        };
      }
    }
    return null;
  }, [providers, selectedModelId, selectedSlotId]);

  // Fallbacks when the model isn't in the providers list yet (e.g. logical
  // ID before the roster sync completes). Default to 128k + all-caps-enabled
  // so the UI is permissive — better to show a working tool than a dimmed
  // one when we don't actually know.
  const modelContextLength = selectedModelInfo?.contextLength || 128000;
  const modelCapabilities = selectedModelInfo?.capabilities || {
    effort: true,
    webSearch: true,
    deepResearch: true,
    extendedThinking: true,
  };
  const modelIsFree = selectedModelInfo?.isFree || false;

  // The cost of the most-recent assistant message (for PriceGauge current).
  // Walks the message list backwards to find the last assistant bubble with
  // a non-null costUsd.
  const lastMsgCost = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && typeof m.costUsd === "number") return m.costUsd;
    }
    return null;
  }, [messages]);
  // Actions (stable references from zustand — don't cause re-renders)
  const setInputText = useChatStore((s) => s.setInputText);
  const setEffort = useChatStore((s) => s.setEffort);
  const setMode = useChatStore((s) => s.setMode);
  const toggleWebSearch = useChatStore((s) => s.toggleWebSearch);
  const toggleDeepResearch = useChatStore((s) => s.toggleDeepResearch);
  const setWebTemplate = useChatStore((s) => s.setWebTemplate);
  const setDeepTemplate = useChatStore((s) => s.setDeepTemplate);
  const setJudge = useChatStore((s) => s.setJudge);
  const setBusyMode = useChatStore((s) => s.setBusyMode);
  const resetTools = useChatStore((s) => s.resetTools);
  const setSidebarOpen = useChatStore((s) => s.setSidebarOpen);
  const setFileDrawerOpen = useChatStore((s) => s.setFileDrawerOpen);
  const setPanelDrawerOpen = useChatStore((s) => s.setPanelDrawerOpen);
  const setQueueMonitorOpen = useChatStore((s) => s.setQueueMonitorOpen);
  const togglePin = useChatStore((s) => s.togglePin);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const createSession = useChatStore((s) => s.createSession);
  const switchSession = useChatStore((s) => s.switchSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const stopGeneration = useChatStore((s) => s.stopGeneration);
  const dequeueMessage = useChatStore((s) => s.dequeueMessage);
  const runJudge = useChatStore((s) => s.runJudge);
  const connectMonitor = useChatStore((s) => s.connectMonitor);
  const disconnectMonitor = useChatStore((s) => s.disconnectMonitor);
  const cancelJob = useChatStore((s) => s.cancelJob);
  const applySuggestion = useChatStore((s) => s.applySuggestion);

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

  // Connect the monitor SSE stream on mount + whenever the workspace changes.
  // The backend pushes job_started/job_progress/job_delta/job_complete/
  // job_error events so the QueueMonitor panel stays live without polling.
  useEffect(() => {
    connectMonitor(clientRef.current);
    return () => {
      disconnectMonitor();
    };
    // Reconnect when the workspace changes — different workspace = different
    // job scope. eslint disabled because connectMonitor is a stable zustand
    // action and we explicitly want the workspace dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

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
  const running = isBusy && (status === "running" || status === "starting");
  const queueCount = queue.length;

  // Whether ANY tool override is active (drives the "Reset" button visibility).
  const toolsDirty =
    effort !== "med" ||
    webSearch ||
    deepResearch ||
    !!webTemplate ||
    !!deepTemplate ||
    mode !== "auto";

  // Handle judge run — fired from the ToolIcons popover. The store handles
  // the placeholder + API call + bubble insertion.
  const handleRunJudge = useCallback(() => {
    runJudge(clientRef.current, undefined, effectiveModelId || undefined);
  }, [runJudge, effectiveModelId]);

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

        {/* PriceGauge — current message cost / session total. Free models
            show FREE. Uses REAL cost data from the API. */}
        <PriceGauge
          currentCost={lastMsgCost}
          totalCost={sessionCost}
          isFree={modelIsFree}
        />

        {/* Token usage indicator with hover breakdown */}
        {lastUsage && (
          <div
            className="flex items-center gap-1 text-[10px] text-muted-foreground tabular-nums cursor-help"
            title={`Token breakdown:\nInput: ${lastUsage.input_tokens.toLocaleString()}\nOutput: ${lastUsage.output_tokens.toLocaleString()}\nTotal: ${lastUsage.total_tokens.toLocaleString()}${lastUsage.reasoning_tokens ? `\nReasoning: ${lastUsage.reasoning_tokens.toLocaleString()}` : ""}`}
          >
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="opacity-60">
              <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
            </svg>
            {lastUsage.total_tokens.toLocaleString()}
          </div>
        )}

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

        {/* Context usage circle — uses the selected model's REAL
            contextLength (was hardcoded 128000). 44px, percentage in the
            center, color shift green→amber→red, pulses at ≥100%. */}
        {lastUsage && (
          <ContextCircle
            used={lastUsage.total_tokens}
            max={modelContextLength}
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

        {/* QueueMonitor toggle — badge shows active job count */}
        <button
          onClick={() => setQueueMonitorOpen(!queueMonitorOpen)}
          className={`p-1.5 rounded-md transition-colors relative ${
            queueMonitorOpen
              ? "text-accent bg-accent/10"
              : "text-muted-foreground hover:text-foreground hover:bg-surface2"
          }`}
          title="Queue monitor"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
          </svg>
          {jobs.filter((j) => j.status === "queued" || j.status === "running").length > 0 && (
            <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1 rounded-full bg-accent text-white text-[9px] font-mono flex items-center justify-center">
              {jobs.filter((j) => j.status === "queued" || j.status === "running").length}
            </span>
          )}
          {suggestions.length > 0 && !jobs.some((j) => j.status === "queued" || j.status === "running") && (
            <span className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-amber-400 animate-pulse" />
          )}
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
            {messages.map((msg, i) => (
              <ChatMessageBubble
                key={msg.id}
                message={msg}
                onRetry={
                  // Show "Retry" only on the last assistant message (the one
                  // most likely to need a re-roll). The retry handler resends
                  // the last user message, which kicks off a fresh turn.
                  i === messages.length - 1 && msg.role === "assistant" && !isBusy
                    ? () => {
                        if (lastUserMsgRef.current) {
                          handleSend(lastUserMsgRef.current);
                        }
                      }
                    : undefined
                }
              />
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
        {/* Tool bar — sits ABOVE the input. ToolIcons renders the unified
            effort / web / deep / judge popovers, with capability dimming.
            Reset clears all tool overrides. Queue/Stop toggles what happens
            when the user presses Enter while a turn is in flight. */}
        <div className="flex items-center gap-1 max-w-3xl mx-auto mb-1.5 px-1 min-h-[26px]">
          <ToolIcons
            effort={effort}
            webSearch={webSearch}
            deepResearch={deepResearch}
            webTemplate={webTemplate}
            deepTemplate={deepTemplate}
            judge={judge}
            capabilities={modelCapabilities}
            disabled={running}
            setEffort={setEffort}
            toggleWebSearch={toggleWebSearch}
            toggleDeepResearch={toggleDeepResearch}
            setWebTemplate={setWebTemplate}
            setDeepTemplate={setDeepTemplate}
            setJudge={setJudge}
            onRunJudge={handleRunJudge}
            onOpenTemplateLibrary={openTemplateLibrary}
          />

          {/* Mode toggle (compact pill) — kept here because it's an execution
              mode, not a tool. Hidden on narrow viewports. */}
          <ModePill mode={mode} setMode={setMode} disabled={running} />

          {/* Spacer pushes Reset + Queue/Stop toggle to the right. */}
          <div className="flex-1" />

          {/* Reset — only visible when a tool override is active. */}
          {toolsDirty && (
            <button
              onClick={resetTools}
              className="text-[10px] px-2 py-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors flex items-center gap-1 shrink-0"
              title="Reset all tool selections"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
              Reset
            </button>
          )}

          {/* Queue / Stop mode toggle (default: Queue). */}
          <div className="flex items-center bg-surface2 rounded-md p-0.5 text-[10px] shrink-0" title="What happens when you press Enter while the agent is busy">
            <button
              onClick={() => setBusyMode("queue")}
              className={`px-2 py-0.5 rounded transition-colors ${
                busyMode === "queue" ? "bg-accent text-white" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Queue
            </button>
            <button
              onClick={() => setBusyMode("stop")}
              className={`px-2 py-0.5 rounded transition-colors ${
                busyMode === "stop" ? "bg-accent text-white" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Stop
            </button>
          </div>

          {/* Queued message count — appears when there's anything in the queue. */}
          {queueCount > 0 && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-accent/15 text-accent font-mono tabular-nums shrink-0">
              {queueCount} queued
            </span>
          )}
        </div>

        {/* Input row — textarea + send/stop button */}
        <div className="flex items-end gap-1.5 max-w-3xl mx-auto">
          <textarea
            ref={inputRef}
            value={inputText}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={
              isBusy
                ? busyMode === "queue"
                  ? "Queue another message… (Enter to queue, Shift+Enter for newline)"
                  : "Type to replace the running turn… (Enter to stop + send)"
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
          {isBusy && (
            <span className="text-amber-400/60">
              {busyMode === "queue" ? "Queue mode active" : "Stop mode active"}
            </span>
          )}
        </div>
      </div>

      {/* Overlays */}
      <SessionSidebar
        open={sidebarOpen}
        sessions={sessions}
        activeId={activeSessionId}
        isLoading={false}
        pinnedIds={pinnedSessionIds}
        onTogglePin={togglePin}
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

      <QueueMonitor
        open={queueMonitorOpen}
        onClose={() => setQueueMonitorOpen(false)}
        jobs={jobs}
        suggestions={suggestions}
        onApplySuggestion={applySuggestion}
        onCancelJob={(jobId) => cancelJob(clientRef.current, jobId)}
      />

      <TemplateLibrary
        open={templateLibraryOpen}
        initialKind={templateLibraryKind}
        initialTab={templateLibraryTab}
        settings={settings}
        onClose={closeTemplateLibrary}
        onApply={applyTemplate}
      />
    </div>
  );
}

/** Compact execution-mode pill (auto / build / plan). Sits at the right edge
 *  of the tool bar so it's out of the way but still one click away. */
function ModePill({
  mode,
  setMode,
  disabled,
}: {
  mode: "auto" | "build" | "plan";
  setMode: (m: "auto" | "build" | "plan") => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative shrink-0">
      <button
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        className={`flex items-center gap-1 px-2 py-1 rounded-md text-[10px] transition-colors ${
          open
            ? "text-accent bg-accent/10"
            : "text-muted-foreground hover:text-foreground hover:bg-surface2"
        } disabled:opacity-50 disabled:cursor-not-allowed capitalize`}
        title={`Execution mode: ${mode}`}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 9h6v6H9z" />
        </svg>
        <span className="hidden sm:inline">{mode}</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-0 mb-1 z-50 w-48 rounded-lg border border-border bg-surface shadow-xl p-1.5">
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">Execution Mode</div>
            {(["auto", "build", "plan"] as const).map((m) => (
              <button
                key={m}
                onClick={() => { setMode(m); setOpen(false); }}
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

