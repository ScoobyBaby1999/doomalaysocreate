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
import { SaveAsTemplateDialog } from "../components/SaveAsTemplateDialog";
import { Popover } from "../components/Popover";

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
          color: p.color || "#a855f7",
        };
    }
    return {
      label: selectedModelId.split("/").pop() || selectedModelId,
      color: "#a855f7",
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
  // BATCH-2 Task 5.1 — removed busyMode + setBusyMode subscriptions. The
  // queue/stop toggle was removed from the UI; queue is always the default.
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
  // BATCH-2 Task 5.1 — setBusyMode no longer used (queue/stop toggle removed).
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
  const wsBtnRef = useRef<HTMLButtonElement>(null);
  const ghClient = useMemo(() => new GitHubClient(settings), [settings]);

  // "Save as Template" dialog — opened from the chat input toolbar.
  // Pre-fills the markdown with the most-recent user + assistant messages.
  const [saveAsTemplateOpen, setSaveAsTemplateOpen] = useState(false);
  const handleSaveAsTemplate = useCallback(
    (template: import("../api/templates").Template) => {
      // Route the freshly-saved template to the right tool slot (same logic
      // as applyTemplate in chatStore), then close the dialog + toast.
      applyTemplate(template);
      setSaveAsTemplateOpen(false);
    },
    [applyTemplate],
  );

  // Suggested name for the Save-as-Template dialog: the most-recent user
  // message, truncated to 60 chars. Falls back to "Chat Template".
  const suggestedTemplateName = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "user" && !m.isError && m.content?.trim()) {
        const t = m.content.trim().replace(/\s+/g, " ");
        return t.length > 60 ? t.slice(0, 57) + "…" : t;
      }
    }
    return "";
  }, [messages]);
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

  // BATCH-2 Task 5.8 — Enter = newline (phone-first). Removed the
  // Enter-to-send handler. The user sends via the explicit Send button
  // (already 48×48 next to the textarea). This makes long messages on
  // mobile much easier to compose without accidental sends.

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

  // Mobile header: secondary tools (files, panel, queue, export, workspace,
  // verification, token usage, status) collapse into a tappable bar that
  // defaults to closed on phones but is always visible on `sm:`+.
  const [toolsBarOpen, setToolsBarOpen] = useState(false);

  return (
    <div className="flex flex-col h-full relative bg-bg min-h-0 overflow-hidden">
      {/* ── Compact mobile-first header ──────────────────────────────
       *  Row 1 (always visible): menu · model badge · context circle ·
       *    price gauge · stop/new. Everything else lives in the
       *    collapsible Row 2 below.
       *  Row 2 (collapsible on mobile, always open on desktop): files,
       *    panel, queue, export, workspace, verification, token usage,
       *    status indicator.
       *  All icon buttons are ≥44×44 (touch-target friendly).
       *
       *  `relative z-30` is CRITICAL — the header has backdrop-blur which
       *  creates a stacking context. Without an explicit z-index, the
       *  workspace dropdown inside it gets painted UNDER the messages area
       *  below. z-30 puts the header (and all its descendants, including
       *  the dropdown at internal z-50) above the messages area at z-0. */}
      <header className="flex flex-col shrink-0 relative z-30 border-b border-white/5 bg-surface/60 backdrop-blur safe-top">
        {/* Row 1 — essentials */}
        <div className="flex items-center gap-1.5 px-2 sm:px-3 h-12 sm:h-11">
          {/* Session menu toggle */}
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            aria-label="Chat sessions"
            title="Chat sessions"
            className={`touch-target shrink-0 w-10 h-10 sm:w-9 sm:h-9 rounded-xl transition-colors ${
              sidebarOpen
                ? "text-accent bg-accent/15"
                : "text-muted-foreground hover:text-foreground hover:bg-surface2"
            }`}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>

          {/* Active session title (truncated) — hidden on the smallest
              screens so the model badge has breathing room. */}
          <div className="hidden xs:block text-[12px] font-medium text-foreground truncate max-w-[120px] sm:max-w-[140px]">
            {activeSessionId
              ? sessions.find((s) => s.id === activeSessionId)?.title || "New Chat"
              : "New Chat"}
          </div>

          {/* Spacer pushes the right-side cluster (context circle, price,
           *  stop, more) toward the right edge. The model badge lives in
           *  the chat INPUT toolbar now (BATCH-2 Task 5.2 / FE-COMPLETION
           *  Task 1) so the header stays uncluttered. */}
          <div className="flex-1" />

          {/* Context usage circle — uses the selected model's REAL
              contextLength. Hidden on the very smallest screens so the
              header doesn't crowd. */}
          {lastUsage && (
            <div className="hidden xs:block shrink-0">
              <ContextCircle
                used={lastUsage.total_tokens}
                max={modelContextLength}
              />
            </div>
          )}

          {/* PriceGauge — current message cost / session total.
              Only shown when there's actually a cost OR the model isn't free,
              so the header isn't cluttered on a fresh session. */}
          {(typeof lastMsgCost === "number" || (sessionCost ?? 0) > 0 || !modelIsFree) && (
            <div className="shrink-0">
              <PriceGauge
                currentCost={lastMsgCost}
                totalCost={sessionCost}
                isFree={modelIsFree}
              />
            </div>
          )}

          {/* FE-COMPLETION Task 2 — the always-visible "+" New button has
           *  been removed entirely (it sat between price and the 3-dots More
           *  toggle, overlapping on small screens and wasn't useful — new
           *  chat is reachable from the SessionSidebar). Only the Stop
           *  button remains here while a turn is generating. */}
          {running && (
            <button
              onClick={handleStop}
              aria-label="Stop"
              title="Stop"
              className="touch-target shrink-0 flex items-center gap-1.5 px-3 h-9 rounded-full bg-red-500/15 text-red-300 hover:bg-red-500/25 border border-red-500/20 transition-colors font-medium text-[12px]"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="1.5" />
              </svg>
              <span className="hidden sm:inline">Stop</span>
            </button>
          )}

          {/* "More" toggle — only visible on mobile, opens the secondary
              tools row. */}
          <button
            onClick={() => setToolsBarOpen((v) => !v)}
            aria-label="More tools"
            aria-expanded={toolsBarOpen}
            title="More tools"
            className={`touch-target sm:hidden shrink-0 w-10 h-10 rounded-xl transition-colors ${
              toolsBarOpen
                ? "text-accent bg-accent/15"
                : "text-muted-foreground hover:text-foreground hover:bg-surface2"
            }`}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="5" r="1.5" fill="currentColor" />
              <circle cx="12" cy="12" r="1.5" fill="currentColor" />
              <circle cx="12" cy="19" r="1.5" fill="currentColor" />
            </svg>
          </button>
        </div>

        {/* Row 2 — secondary tools. Collapsed by default on mobile,
            always visible on sm:+. Horizontally scrollable if needed.
            Thin top border on mobile (border-white/5) — removed on sm:+
            so the two rows feel like one continuous bar. */}
        <div
          className={`${
            toolsBarOpen ? "flex" : "hidden"
          } sm:flex items-center gap-1.5 px-2 sm:px-3 pb-2 sm:pb-1.5 pt-1 sm:pt-0 sm:h-10 overflow-x-auto no-scrollbar border-t border-white/5 sm:border-t-0`}
        >
          {/* Workspace selector — relative+shrink-0 keeps the dropdown
              anchored to the button and out of the horizontal scroll.
              The dropdown itself uses a portal (see Popover component) so
              it escapes the header's backdrop-filter containing block +
              Row 2's overflow-x-auto clipping. */}
          <div className="relative shrink-0">
            <button
              ref={wsBtnRef}
              onClick={() => setWsOpen((v) => !v)}
              disabled={running}
              title={workspaceId || "No workspace (ephemeral)"}
              className="touch-target shrink-0 flex items-center gap-1.5 px-2.5 h-8 rounded-full border border-white/5 bg-surface2/40 hover:border-accent/40 hover:bg-surface2/70 transition-all text-[11.5px] disabled:opacity-50 disabled:cursor-not-allowed max-w-[160px]"
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
            <Popover
              open={wsOpen}
              onClose={() => setWsOpen(false)}
              anchorRef={wsBtnRef}
              align="right"
              direction="down"
              width={240}
              title="Workspace"
            >
              <button
                onClick={() => { setWorkspaceId(null); setWsOpen(false); }}
                className={`w-full text-left px-3 py-2.5 text-[12px] hover:bg-accent/10 transition-colors rounded-xl ${!workspaceId ? "text-accent font-medium" : ""}`}
              >
                No workspace (ephemeral)
              </button>
              {workspaces.map((ws) => (
                <button
                  key={ws.id}
                  onClick={() => { setWorkspaceId(ws.id); setWsOpen(false); }}
                  className={`w-full text-left px-3 py-2.5 text-[12px] hover:bg-accent/10 transition-colors truncate rounded-xl ${workspaceId === ws.id ? "text-accent font-medium" : ""}`}
                >
                  <div className="truncate">{ws.title}</div>
                  {ws.source_repo && <div className="text-[10px] text-muted-foreground truncate">{ws.source_repo}</div>}
                </button>
              ))}
              {workspaces.length === 0 && (
                <div className="px-3 py-2.5 text-[11px] text-muted-foreground">
                  No workspaces. Connect GitHub in the Workspaces tab.
                </div>
              )}
            </Popover>
          </div>

          {/* Model verification badge — shows what the backend ACTUALLY resolved. */}
          {resolvedModel && (
            <div className="flex flex-col items-end gap-0.5 shrink-0" title={`Verified: ${resolvedProvider || "?"} → ${resolvedModel}`}>
              <span className="text-[10px] text-emerald-400/80 font-mono tabular-nums flex items-center gap-0.5">
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                {resolvedProvider || "unknown"}
              </span>
              {requestedModel && resolvedModel &&
               requestedModel.split("/").pop()?.toLowerCase() !== resolvedModel.split("/").pop()?.toLowerCase() && (
                <span className="text-[9px] text-amber-400/90 font-mono" title={`Requested ${requestedModel} but backend resolved to ${resolvedModel}`}>
                  ⚠ redirected
                </span>
              )}
            </div>
          )}

          {/* Token usage indicator */}
          {lastUsage && (
            <div
              className="flex items-center gap-1 text-[11px] text-muted-foreground tabular-nums cursor-help shrink-0"
              title={`Token breakdown:\nInput: ${lastUsage.input_tokens.toLocaleString()}\nOutput: ${lastUsage.output_tokens.toLocaleString()}\nTotal: ${lastUsage.total_tokens.toLocaleString()}${lastUsage.reasoning_tokens ? `\nReasoning: ${lastUsage.reasoning_tokens.toLocaleString()}` : ""}`}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="opacity-60">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
              </svg>
              {lastUsage.total_tokens.toLocaleString()}
            </div>
          )}

          {/* Status indicator — shows what the agent is doing */}
          {running ? (
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground shrink-0">
              <span className="flex gap-0.5">
                <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "0ms" }} />
                <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "120ms" }} />
                <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "240ms" }} />
              </span>
              <span className="capitalize status-pulse">{status === "starting" ? "starting…" : "working…"}</span>
            </div>
          ) : status === "error" ? (
            <div className="flex items-center gap-1 text-[11px] text-rose-400 shrink-0">
              <span className="size-1 rounded-full bg-rose-400" />
              <span>error</span>
            </div>
          ) : null}

          {/* Files */}
          <button
            onClick={() => setFileDrawerOpen(!fileDrawerOpen)}
            aria-label="Files"
            title="Files"
            className={`touch-target shrink-0 w-9 h-9 rounded-xl transition-colors ${
              fileDrawerOpen
                ? "text-accent bg-accent/15"
                : "text-muted-foreground hover:text-foreground hover:bg-surface2"
            }`}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <polyline points="13 2 13 9 20 9" />
            </svg>
          </button>

          {/* Panel invocations */}
          <button
            onClick={() => setPanelDrawerOpen(!panelDrawerOpen)}
            aria-label="Panel invocations"
            title="Panel invocations"
            className={`touch-target shrink-0 w-9 h-9 rounded-xl transition-colors ${
              panelDrawerOpen
                ? "text-accent bg-accent/15"
                : "text-muted-foreground hover:text-foreground hover:bg-surface2"
            }`}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
          </button>

          {/* QueueMonitor toggle — badge shows active job count */}
          <button
            onClick={() => setQueueMonitorOpen(!queueMonitorOpen)}
            aria-label="Queue monitor"
            title="Queue monitor"
            className={`touch-target shrink-0 w-9 h-9 rounded-xl transition-colors relative ${
              queueMonitorOpen
                ? "text-accent bg-accent/15"
                : "text-muted-foreground hover:text-foreground hover:bg-surface2"
            }`}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
            </svg>
            {jobs.filter((j) => j.status === "queued" || j.status === "running").length > 0 && (
              <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-white text-[10px] font-mono flex items-center justify-center">
                {jobs.filter((j) => j.status === "queued" || j.status === "running").length}
              </span>
            )}
            {suggestions.length > 0 && !jobs.some((j) => j.status === "queued" || j.status === "running") && (
              <span className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-amber-400 animate-pulse" />
            )}
          </button>

          {/* Export — only when there are messages */}
          {messages.length > 0 && (
            <button
              onClick={handleExport}
              aria-label="Export conversation"
              title="Export conversation as markdown"
              className="touch-target shrink-0 w-9 h-9 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </button>
          )}

          {/* BATCH-2 Task 5.9 — Save-as-Template moved here from the chat
              input toolbar. Lives in the header (under the 3-dots More
              toggle, right of the queue monitor) so the input toolbar is
              less cluttered. Disabled when the chat is empty. */}
          {messages.length > 0 && (
            <button
              onClick={() => setSaveAsTemplateOpen(true)}
              aria-label="Save as Template"
              title="Save this chat as a reusable template"
              className="touch-target shrink-0 w-9 h-9 rounded-xl text-muted-foreground hover:text-accent hover:bg-surface2 transition-colors"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                <polyline points="17 21 17 13 7 13 7 21" />
                <polyline points="7 3 7 8 15 8" />
              </svg>
            </button>
          )}
        </div>
      </header>

      {/* Messages area — flex-1 scrollable region. We use `overscroll-contain`
         * so a fling-scroll inside the message list doesn't yank the body
         * (which would reveal the bottom-nav behind the keyboard).
         *
         * `relative z-0` establishes the bottom of the stacking order so the
         * header (z-30) and input area (z-30) — and their popover descendants
         * — always paint above the message bubbles. Without this, dropdowns
         * extending up/down from the header/input would get covered by the
         * chat bubbles. */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto min-h-0 relative z-0 scroll-smooth overscroll-contain pointer-pass"
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
          <div className="py-3 sm:py-4 px-2 sm:px-4 max-w-3xl mx-auto flex flex-col gap-1.5">
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
                onStop={
                  // BATCH-2 Task 5.1 — Stop link at the bottom of the
                  // streaming reply. Only rendered on the LAST assistant
                  // message while it's actively streaming. Removed
                  // automatically when isStreaming flips to false.
                  i === messages.length - 1 && msg.role === "assistant" && msg.isStreaming && isBusy
                    ? handleStop
                    : undefined
                }
              />
            ))}

            {/* Typing indicator — only when the latest message is a user
                message and we haven't received the first delta yet. */}
            {running &&
              messages.length > 0 &&
              messages[messages.length - 1].role === "user" && (
                <div className="px-3 sm:px-4 py-2.5">
                  <div className="flex items-center gap-2.5">
                    <div className="shrink-0 w-8 h-8 rounded-xl bg-accent/15 flex items-center justify-center">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
        <div className="relative z-20 border-t border-white/5 bg-accent/5 px-3 py-2 shrink-0">
          <div className="flex items-center gap-2 text-[11.5px] text-accent">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-pulse shrink-0">
              <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
            </svg>
            <span className="font-medium shrink-0">
              {queueCount} message{queueCount !== 1 ? "s" : ""} queued
            </span>
            <div className="flex-1 truncate text-muted-foreground/80 min-w-0">
              Next: {queue[0]?.text.slice(0, 80)}
              {queue[0] && queue[0].text.length > 80 ? "…" : ""}
            </div>
            <button
              onClick={() => queue.forEach((q) => dequeueMessage(q.id))}
              className="touch-target shrink-0 text-muted-foreground hover:text-foreground px-2.5 h-7 rounded-full hover:bg-surface2 transition-colors text-[11px]"
              title="Clear queue"
            >
              Clear
            </button>
          </div>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="relative z-20 border-t border-red-500/30 bg-red-500/10 px-3 sm:px-4 py-2.5 text-[13px] text-red-300 flex items-center gap-2 sm:gap-3 shrink-0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
            <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span className="flex-1 min-w-0">{error}</span>
          <button
            onClick={() => useChatStore.setState({ error: null })}
            className="touch-target shrink-0 text-red-300/70 hover:text-red-200 px-2.5 h-7 rounded-full text-[11px]"
          >
            dismiss
          </button>
          {lastUserMsgRef.current && (
            <button
              onClick={() => handleSend(lastUserMsgRef.current)}
              className="touch-target shrink-0 px-3 h-7 rounded-full border border-red-500/40 text-[11px] hover:bg-red-500/20 transition-colors"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {/* ── Input area — fixed at the bottom of the chat column ──────
         *  On mobile this is the most-touched region, so:
         *    • textarea is `rounded-2xl` and ≥48px tall
         *    • send button is 48×48 (min touch target) and uses BOTH
         *      `onClick` + `onPointerDown` so it fires immediately on
         *      touch (removing the 300ms delay even on older browsers).
         *    • tool bar above the input is horizontally scrollable so it
         *      never wraps and pushes the input off-screen on phones.
         *    • safe-area-inset padding keeps the input above the iOS home
         *      indicator.
         *
         *  `relative z-30` is CRITICAL for the same reason as the header:
         *  the ToolIcons popovers extend UP from this area into the
         *  messages area, so without an explicit z-index the popovers
         *  would be painted UNDER the chat bubbles. */}
      <div className="relative z-30 border-t border-white/5 bg-surface/40 backdrop-blur-md pt-2 pb-[max(0.75rem,env(safe-area-inset-bottom))] px-2 sm:px-3 shrink-0">
        {/* Tool bar — horizontally scrollable on mobile. Subtle bg so it
            reads as a distinct toolbar above the input. */}
        <div className="flex items-center gap-1 max-w-3xl mx-auto mb-1.5 px-1 min-h-[36px] overflow-x-auto no-scrollbar">
          {/* FE-COMPLETION Task 1 — Model badge lives here in the chat input
           *  toolbar (moved out of the header so the header is uncluttered).
           *  Pill-shaped with the provider color dot + model name + chevron.
           *  Tapping opens the ModelSelectOverlay. */}
          <button
            onClick={() => !running && openOverlay()}
            disabled={running}
            aria-label="Select model"
            title={selectedProviderName || "Select model"}
            className="touch-target shrink-0 flex items-center gap-1.5 px-2.5 h-8 rounded-full border border-white/5 bg-gradient-to-br from-surface2/80 to-surface/80 hover:from-surface2 hover:to-surface3 hover:border-accent/40 transition-all text-[11.5px] disabled:opacity-50 disabled:cursor-not-allowed max-w-[40vw] sm:max-w-[200px]"
          >
            <span
              className="w-2 h-2 rounded-full shrink-0 ring-1 ring-white/10"
              style={{ background: modelDisplay?.color || "#a855f7" }}
            />
            <span className="truncate text-foreground font-medium">
              {modelDisplay?.label || "Select model"}
            </span>
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-muted-foreground/70 shrink-0">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>

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
              mode, not a tool. */}
          <ModePill mode={mode} setMode={setMode} disabled={running} />

          {/* Reset — only visible when a tool override is active. */}
          {toolsDirty && (
            <button
              onClick={resetTools}
              className="touch-target shrink-0 text-[11px] px-2.5 h-7 rounded-full text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors flex items-center gap-1"
              title="Reset all tool selections"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
              Reset
            </button>
          )}

          {/* BATCH-2 Task 5.1 — removed the Queue/Stop mode toggle and the
           *  "Save as Template" button from here. Queue is now the default
           *  (always); Stop lives at the bottom of the streaming reply.
           *  Save-as-Template moved to the header (under the 3-dots More
           *  toggle, right of the queue monitor) per Task 5.9.
           *
           * Queued-message count chip — still here so the user can see at
           * a glance how many messages are pending. */}
          {queueCount > 0 && (
            <span className="text-[11px] px-2.5 h-6 inline-flex items-center rounded-full bg-accent/15 text-accent font-mono tabular-nums shrink-0">
              {queueCount} queued
            </span>
          )}

          {/* Spacer pushes the queued-count to the left on desktop. */}
          <div className="hidden sm:flex flex-1" />
        </div>

        {/* Input row — textarea + send/stop button. */}
        <div className="flex items-end gap-2 max-w-3xl mx-auto">
          <textarea
            ref={inputRef}
            value={inputText}
            onChange={handleInputChange}
            rows={1}
            placeholder={
              isBusy
                ? "Queue another message… (Enter for newline, Send to queue)"
                : "Ask the agent to build, edit, run, or pack something…"
            }
            className="chat-input-glow flex-1 resize-none bg-surface2/70 border border-white/5 rounded-2xl px-4 py-3 text-[16px] sm:text-[14.5px] leading-relaxed outline-none min-h-[48px] max-h-[160px] transition-colors placeholder:text-muted-foreground/60"
          />

          {/* Send / Stop / Queue — primary action button.
              • 48×48px (≥44px touch target).
              • Uses BOTH `onClick` (mouse) and `onPointerDown` (touch) so
                taps fire instantly with no 300ms delay. The pointer handler
                ignores non-primary buttons + scrolling gestures so it never
                double-fires with onClick. */}
          <button
            onClick={() => (running ? handleStop() : handleSend())}
            onPointerDown={(e) => {
              // Only fire on primary touch / left-click, never on a scroll
              // gesture. Use e.button===0 (primary) and pointerType check
              // so a pen eraser or right-click doesn't trigger.
              if (e.button !== 0) return;
              if (e.pointerType === "mouse") return; // let onClick handle mouse
              e.preventDefault();
              (running ? handleStop() : handleSend());
            }}
            disabled={!running && !inputText.trim()}
            aria-label={running ? "Stop" : isBusy ? "Queue message" : "Send message"}
            className={`send-btn touch-target w-12 h-12 sm:w-12 sm:h-12 rounded-full font-medium text-[13px] disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none shrink-0 transition-all flex items-center justify-center gap-1.5 ${
              running
                ? "bg-red-500/90 text-white hover:bg-red-500"
                : "bg-gradient-to-br from-accent to-accentHover text-white hover:from-accentHover hover:to-accentDeep"
            }`}
            title={running ? "Stop" : "Send (or queue if busy)"}
          >
            {running ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            ) : isBusy ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="10" y2="14" />
                <polygon points="22 2 15 22 10 14 2 9 22 2" />
              </svg>
            )}
          </button>
        </div>

        {/* BATCH-2 Task 5.8 — Enter = newline hint. */}
        <div className="hidden sm:flex items-center justify-center gap-3 mt-1 text-[9px] text-muted-foreground/40">
          <span><kbd className="px-1 py-0.5 rounded bg-surface2 border border-white/5 font-mono">Enter</kbd> for newline</span>
          <span><kbd className="px-1 py-0.5 rounded bg-surface2 border border-white/5 font-mono">Send</kbd> button to send</span>
          {isBusy && (
            <span className="text-amber-400/60">
              Messages will queue while the agent works
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

      <SaveAsTemplateDialog
        open={saveAsTemplateOpen}
        settings={settings}
        messages={messages}
        suggestedName={suggestedTemplateName}
        defaultKind="chat"
        onClose={() => setSaveAsTemplateOpen(false)}
        onSaved={handleSaveAsTemplate}
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
  const btnRef = useRef<HTMLButtonElement>(null);
  return (
    <div className="relative shrink-0">
      <button
        ref={btnRef}
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        aria-label={`Execution mode: ${mode}`}
        className={`touch-target flex items-center gap-1 px-2.5 h-7 rounded-full text-[11px] transition-colors ${
          open
            ? "text-accent bg-accent/15 border border-accent/30"
            : "text-muted-foreground hover:text-foreground hover:bg-surface2 border border-white/5"
        } disabled:opacity-50 disabled:cursor-not-allowed capitalize`}
        title={`Execution mode: ${mode}`}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 9h6v6H9z" />
        </svg>
        <span className="hidden sm:inline">{mode}</span>
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={btnRef}
        align="left"
        direction="up"
        width={224}
        title="Execution Mode"
      >
        {(["auto", "build", "plan"] as const).map((m) => (
          <button
            key={m}
            onClick={() => { setMode(m); setOpen(false); }}
            className={`w-full text-left px-3 py-2 rounded-xl text-[12px] transition-colors ${
              mode === m ? "bg-accent/15 text-accent font-medium" : "text-muted-foreground hover:bg-surface2"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="capitalize">{m}</span>
              {mode === m && <span className="text-[10px] text-accent">●</span>}
            </div>
            <div className="text-[10px] opacity-60 mt-0.5">
              {m === "auto" ? "Execute autonomously" : m === "build" ? "Step-by-step with confirmation" : "Plan first, wait for approval"}
            </div>
          </button>
        ))}
      </Popover>
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
    <div className="flex flex-col items-center justify-center h-full px-4 sm:px-6 py-6 sm:py-8 overflow-y-auto">
      <div className="max-w-lg w-full text-center">
        {/* Icon */}
        <div className="w-16 h-16 rounded-3xl bg-accent/10 flex items-center justify-center mx-auto mb-5 empty-state-icon">
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

        {/* Suggestions — tap targets ≥44px, rounded-2xl cards with thin
            subtle borders (border-white/5 instead of border-border so they
            don't read as "bricky"). */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-left">
          {suggestions.map((s, i) => (
            <button
              key={i}
              onClick={() => onSend(s.text)}
              className="flex items-start gap-2.5 text-left text-[12.5px] px-3.5 py-3 min-h-[44px] rounded-2xl border border-white/5 bg-surface/30 hover:border-accent/40 hover:bg-accent/5 transition-all text-muted-foreground hover:text-foreground leading-relaxed card-hover"
            >
              <span className="text-base shrink-0">{s.icon}</span>
              <span>{s.text}</span>
            </button>
          ))}
        </div>

        {/* Tips — hidden on mobile (no keyboard). FE-COMPLETION Task 8:
            Enter = newline (phone-first); send via the explicit Send
            button. The old "Enter to send / Shift+Enter newline" hint
            was wrong after the Enter-to-send handler was removed. */}
        <div className="hidden sm:flex mt-6 items-center justify-center gap-4 text-[10px] text-muted-foreground/50">
          <span className="flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded bg-surface2 border border-white/5 font-mono">Enter</kbd>
            for newline
          </span>
          <span className="flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded bg-surface2 border border-white/5 font-mono">Send</kbd>
            button to send
          </span>
          <span>·</span>
          <span>Queue messages while agent works</span>
        </div>
      </div>
    </div>
  );
}

