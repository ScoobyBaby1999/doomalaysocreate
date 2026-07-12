/**
 * Chat Store — Centralized state management for agent chat.
 *
 * Key features (the "1-up" philosophy):
 *  - **Message queue**: users can queue multiple messages while the LLM is
 *    working. They fire sequentially the moment the previous turn ends.
 *  - **SSE streaming**: real-time deltas (no 300ms polling pop-in).
 *  - **Crash-safe persistence**: activeSessionId and workspaceId survive
 *    refresh. The session list is rehydrated from the backend on mount.
 *  - **Optimistic UI**: every user action reflects instantly in the UI;
 *    backend errors are surfaced as inline retries, never as disappearing
 *    state.
 *  - **Immutable updates**: no in-place mutation of messages (which caused
 *    the previous "stack weirdly" bug).
 *  - **Single source of truth for the agent session**: we track
 *    `currentAgentSessionId` so Stop actually works.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  AgentClient,
  type AgentEvent,
  type AgentFile,
  type AgentStatus,
  type ChatSession,
  type AgentStart,
} from "../api/agent";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ChatRole =
  | "user"
  | "assistant"
  | "thinking"
  | "tool"
  | "status"
  | "panel";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  isError?: boolean;
  isStreaming?: boolean;
  toolName?: string;
  toolSummary?: string;
  panelStatus?: string;
  costUsd?: number | null;
  timestamp: number;
  /** Backend seq number — used for dedupe. Local-only messages use -1. */
  seq: number;
  /** True for messages that haven't been ack'd by the backend yet. */
  pending?: boolean;
}

export interface QueuedMessage {
  id: string;
  text: string;
  enqueuedAt: number;
}

export interface PanelInvocation {
  invoke_id: string;
  task_name: string;
  prompt: string;
  panel: string[];
  status: string;
  snapshot?: Record<string, unknown>;
  error?: string;
}

export interface ChatState {
  // Sessions
  sessions: ChatSession[];
  activeSessionId: string | null;
  isLoadingSessions: boolean;
  sessionError: string | null;
  /** True while we're restoring the active session's events on mount/switch. */
  isLoadingMessages: boolean;

  // Messages
  messages: ChatMessage[];
  isStreaming: boolean;
  status: AgentStatus;

  // Input
  inputText: string;
  /** True when an agent turn is in flight (UI shows Stop). */
  isBusy: boolean;
  error: string | null;

  // Message queue (1-up: send-while-busy)
  queue: QueuedMessage[];

  // Per-session toggles
  effort: "low" | "med" | "high" | "max";
  webSearch: boolean;
  deepResearch: boolean;

  // Files
  files: AgentFile[];
  fileDrawerOpen: boolean;

  // Panel
  panelInvocations: PanelInvocation[];
  panelDrawerOpen: boolean;

  // Session sidebar
  sidebarOpen: boolean;

  // Derived
  cost: number | null;
  currentModel: string | null;
  workspaceId: string | null;

  // Internal: current agent session ID (for Stop and SSE)
  _agentSessionId: string | null;
  _streamController: AbortController | null;

  // Actions
  setInputText: (text: string) => void;
  setEffort: (effort: "low" | "med" | "high" | "max") => void;
  toggleWebSearch: () => void;
  toggleDeepResearch: () => void;
  setSidebarOpen: (open: boolean) => void;
  setFileDrawerOpen: (open: boolean) => void;
  setPanelDrawerOpen: (open: boolean) => void;
  setWorkspaceId: (id: string | null) => void;

  // Core operations
  loadSessions: (client: AgentClient) => Promise<void>;
  createSession: (client: AgentClient, model?: string) => Promise<string>;
  switchSession: (client: AgentClient, sessionId: string) => Promise<void>;
  deleteSession: (client: AgentClient, sessionId: string) => Promise<void>;
  renameSession: (client: AgentClient, sessionId: string, title: string) => Promise<void>;
  sendMessage: (
    client: AgentClient,
    message: string,
    model?: string
  ) => Promise<void>;
  /** Remove a queued message (only if not yet sent). */
  dequeueMessage: (id: string) => void;
  stopGeneration: (client: AgentClient) => Promise<void>;
  refreshFiles: (client: AgentClient) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let nextMsgId = 0;
function genMsgId(): string {
  return `m${++nextMsgId}_${Date.now().toString(36)}`;
}

function genQueueId(): string {
  return `q${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/** Convert raw AgentEvents into normalized ChatMessages. Pure function. */
export function eventsToMessages(events: AgentEvent[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  // Track the index of the streaming assistant / thinking message so deltas
  // merge into it without mutation.
  let streamingAssistantIdx = -1;
  let streamingThinkingIdx = -1;

  for (const ev of events) {
    const ts = (ev.ts || Date.now() / 1000) * 1000;
    const seq = ev.i ?? messages.length;

    switch (ev.type) {
      case "user": {
        messages.push({
          id: genMsgId(),
          role: "user",
          content: ev.text || "",
          timestamp: ts,
          seq,
        });
        streamingAssistantIdx = -1;
        streamingThinkingIdx = -1;
        break;
      }
      case "assistant": {
        messages.push({
          id: genMsgId(),
          role: "assistant",
          content: ev.text || "",
          timestamp: ts,
          seq,
        });
        streamingAssistantIdx = -1;
        streamingThinkingIdx = -1;
        break;
      }
      case "assistant_delta": {
        if (streamingAssistantIdx === -1) {
          messages.push({
            id: genMsgId(),
            role: "assistant",
            content: ev.text || "",
            isStreaming: true,
            timestamp: ts,
            seq,
          });
          streamingAssistantIdx = messages.length - 1;
        } else {
          const cur = messages[streamingAssistantIdx];
          messages[streamingAssistantIdx] = {
            ...cur,
            content: cur.content + (ev.text || ""),
            seq,
            isStreaming: true,
          };
        }
        streamingThinkingIdx = -1;
        break;
      }
      case "thinking": {
        messages.push({
          id: genMsgId(),
          role: "thinking",
          content: ev.text || "",
          timestamp: ts,
          seq,
        });
        streamingThinkingIdx = -1;
        streamingAssistantIdx = -1;
        break;
      }
      case "thinking_delta": {
        if (streamingThinkingIdx === -1) {
          messages.push({
            id: genMsgId(),
            role: "thinking",
            content: ev.text || "",
            isStreaming: true,
            timestamp: ts,
            seq,
          });
          streamingThinkingIdx = messages.length - 1;
        } else {
          const cur = messages[streamingThinkingIdx];
          messages[streamingThinkingIdx] = {
            ...cur,
            content: cur.content + (ev.text || ""),
            seq,
            isStreaming: true,
          };
        }
        streamingAssistantIdx = -1;
        break;
      }
      case "tool_use": {
        messages.push({
          id: genMsgId(),
          role: "tool",
          content: "",
          toolName: ev.name || "tool",
          toolSummary: ev.summary || "",
          timestamp: ts,
          seq,
        });
        streamingAssistantIdx = -1;
        streamingThinkingIdx = -1;
        break;
      }
      case "tool_result": {
        messages.push({
          id: genMsgId(),
          role: "tool",
          content: ev.text || "",
          isError: ev.is_error,
          toolName: "result",
          timestamp: ts,
          seq,
        });
        streamingAssistantIdx = -1;
        streamingThinkingIdx = -1;
        break;
      }
      case "status": {
        const st = ev as {
          state: AgentStatus;
          detail?: string;
          cost_usd?: number | null;
        };
        if (st.state === "idle" && st.detail === "interrupted") {
          messages.push({
            id: genMsgId(),
            role: "status",
            content: "— stopped —",
            timestamp: ts,
            seq,
          });
        } else if (st.state === "error") {
          messages.push({
            id: genMsgId(),
            role: "status",
            content: st.detail || "Agent error",
            isError: true,
            costUsd: st.cost_usd,
            timestamp: ts,
            seq,
          });
        }
        // status events don't reset streaming indexes — they wrap a turn.
        break;
      }
      case "panel": {
        messages.push({
          id: genMsgId(),
          role: "panel",
          content: ev.task_name || "Panel",
          panelStatus: ev.status || "starting",
          timestamp: ts,
          seq,
        });
        streamingAssistantIdx = -1;
        streamingThinkingIdx = -1;
        break;
      }
      // steering events are not rendered as messages (they're routing metadata).
    }
  }

  // Mark the last streaming message as no longer streaming (turn ended).
  if (streamingAssistantIdx !== -1) {
    messages[streamingAssistantIdx] = { ...messages[streamingAssistantIdx], isStreaming: false };
  }
  if (streamingThinkingIdx !== -1) {
    messages[streamingThinkingIdx] = { ...messages[streamingThinkingIdx], isStreaming: false };
  }

  return messages;
}

/** Append a single new event to the existing message list (immutable). */
function appendEvent(existing: ChatMessage[], ev: AgentEvent): ChatMessage[] {
  // The simplest correct merge: re-derive messages from existing events + the
  // new one. The existing messages were derived from a contiguous event
  // stream, so we just need their underlying events back... but we don't have
  // those. Instead, do an efficient targeted update.
  const ts = (ev.ts || Date.now() / 1000) * 1000;
  const seq = ev.i ?? existing.length;
  const out = existing.slice();

  switch (ev.type) {
    case "user": {
      // Backend echoes user messages. If we already have a pending user
      // message with the same content (the optimistic one we added locally),
      // mark it ack'd instead of duplicating.
      const idx = out.findIndex(
        (m) => m.role === "user" && m.pending && m.content === (ev.text || "")
      );
      if (idx !== -1) {
        out[idx] = { ...out[idx], pending: false, seq, timestamp: ts };
        return out;
      }
      // Otherwise, only add if we don't already have this exact seq as a user.
      if (!out.some((m) => m.role === "user" && m.seq === seq)) {
        out.push({
          id: genMsgId(),
          role: "user",
          content: ev.text || "",
          timestamp: ts,
          seq,
        });
      }
      return out;
    }
    case "assistant": {
      out.push({
        id: genMsgId(),
        role: "assistant",
        content: ev.text || "",
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "assistant_delta": {
      // Find the last assistant message that is currently streaming.
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "assistant" && out[i].isStreaming) {
          out[i] = { ...out[i], content: out[i].content + (ev.text || ""), seq };
          return out;
        }
      }
      // No streaming assistant — start a new one.
      out.push({
        id: genMsgId(),
        role: "assistant",
        content: ev.text || "",
        isStreaming: true,
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "thinking": {
      out.push({
        id: genMsgId(),
        role: "thinking",
        content: ev.text || "",
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "thinking_delta": {
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "thinking" && out[i].isStreaming) {
          out[i] = { ...out[i], content: out[i].content + (ev.text || ""), seq };
          return out;
        }
      }
      out.push({
        id: genMsgId(),
        role: "thinking",
        content: ev.text || "",
        isStreaming: true,
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "tool_use": {
      out.push({
        id: genMsgId(),
        role: "tool",
        content: "",
        toolName: ev.name || "tool",
        toolSummary: ev.summary || "",
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "tool_result": {
      out.push({
        id: genMsgId(),
        role: "tool",
        content: ev.text || "",
        isError: ev.is_error,
        toolName: "result",
        timestamp: ts,
        seq,
      });
      return out;
    }
    case "status": {
      const st = ev as {
        state: AgentStatus;
        detail?: string;
        cost_usd?: number | null;
      };
      if (st.state === "idle" && st.detail === "interrupted") {
        out.push({
          id: genMsgId(),
          role: "status",
          content: "— stopped —",
          timestamp: ts,
          seq,
        });
      } else if (st.state === "error") {
        out.push({
          id: genMsgId(),
          role: "status",
          content: st.detail || "Agent error",
          isError: true,
          costUsd: st.cost_usd,
          timestamp: ts,
          seq,
        });
      }
      return out;
    }
    case "panel": {
      out.push({
        id: genMsgId(),
        role: "panel",
        content: ev.task_name || "Panel",
        panelStatus: ev.status || "starting",
        timestamp: ts,
        seq,
      });
      return out;
    }
    default:
      return out;
  }
}

/** Mark the last streaming message as done (called when a turn ends). */
function finalizeStreaming(existing: ChatMessage[]): ChatMessage[] {
  const out = existing.slice();
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i].isStreaming) {
      out[i] = { ...out[i], isStreaming: false };
      break;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

const ACTIVE_SESSION_KEY = "doomalaysocreate.chat.active_session";
const WORKSPACE_KEY = "doomalaysocreate.chat.workspace_id";

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      // -- State --
      sessions: [],
      activeSessionId: null,
      isLoadingSessions: false,
      sessionError: null,
      isLoadingMessages: false,

      messages: [],
      isStreaming: false,
      status: "idle",

      inputText: "",
      isBusy: false,
      error: null,

      queue: [],

      effort: "med",
      webSearch: false,
      deepResearch: false,

      files: [],
      fileDrawerOpen: false,

      panelInvocations: [],
      panelDrawerOpen: false,

      sidebarOpen: false,
      cost: null,
      currentModel: null,
      workspaceId: null,

      _agentSessionId: null,
      _streamController: null,

      // -- Simple setters --
      setInputText: (text) => set({ inputText: text }),
      setEffort: (effort) => set({ effort }),
      toggleWebSearch: () => set((s) => ({ webSearch: !s.webSearch })),
      toggleDeepResearch: () => set((s) => ({ deepResearch: !s.deepResearch })),
      setSidebarOpen: (open) => set({ sidebarOpen: open }),
      setFileDrawerOpen: (open) => set({ fileDrawerOpen: open }),
      setPanelDrawerOpen: (open) => set({ panelDrawerOpen: open }),
      setWorkspaceId: (id) => {
        set({ workspaceId: id });
        try {
          if (id) localStorage.setItem(WORKSPACE_KEY, id);
          else localStorage.removeItem(WORKSPACE_KEY);
        } catch {
          /* ignore */
        }
      },

      // -- Load sessions --
      loadSessions: async (client) => {
        set({ isLoadingSessions: true, sessionError: null });
        try {
          const { sessions } = await client.listChatSessions();
          // Restore active session if it still exists.
          let activeId = get().activeSessionId;
          try {
            const stored = localStorage.getItem(ACTIVE_SESSION_KEY);
            if (stored && !activeId) activeId = stored;
          } catch {
            /* ignore */
          }
          if (activeId && !sessions.find((s) => s.id === activeId)) {
            activeId = null;
            try {
              localStorage.removeItem(ACTIVE_SESSION_KEY);
            } catch {
              /* ignore */
            }
          }
          set({
            sessions,
            isLoadingSessions: false,
            activeSessionId: activeId,
          });

          // If we have an active session, load its events.
          if (activeId) {
            await get().switchSession(client, activeId);
          }
        } catch (e) {
          set({
            sessionError: e instanceof Error ? e.message : "Failed to load sessions",
            isLoadingSessions: false,
          });
        }
      },

      // -- Create session --
      createSession: async (client, model) => {
        try {
          const cs = await client.createChatSession(
            undefined,
            model,
            get().workspaceId || undefined,
          );
          set((s) => ({
            sessions: [cs, ...s.sessions],
            activeSessionId: cs.id,
            messages: [],
            status: "idle",
            isBusy: false,
            isStreaming: false,
            error: null,
            cost: null,
            panelInvocations: [],
            files: [],
            queue: [],
            _agentSessionId: null,
            _streamController: null,
          }));
          try {
            localStorage.setItem(ACTIVE_SESSION_KEY, cs.id);
          } catch {
            /* ignore */
          }
          return cs.id;
        } catch (e) {
          set({
            error: e instanceof Error ? e.message : "Failed to create session",
          });
          throw e;
        }
      },

      // -- Switch session --
      switchSession: async (client, sessionId) => {
        // Abort any in-flight stream first.
        const ctrl = get()._streamController;
        if (ctrl) {
          ctrl.abort();
        }
        set({
          isLoadingMessages: true,
          activeSessionId: sessionId,
          messages: [],
          error: null,
          status: "idle",
          isBusy: false,
          isStreaming: false,
          panelInvocations: [],
          queue: [],
          _agentSessionId: null,
          _streamController: null,
        });
        try {
          const evData = await client.getChatEvents(sessionId);
          const messages = eventsToMessages(evData.events);
          set({ messages, isLoadingMessages: false });
          try {
            localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
          } catch {
            /* ignore */
          }
          // If the session has a workspace_id, sync it.
          const cs = get().sessions.find((s) => s.id === sessionId);
          if (cs?.workspace_id && cs.workspace_id !== get().workspaceId) {
            get().setWorkspaceId(cs.workspace_id);
          }
        } catch (e) {
          set({
            error: e instanceof Error ? e.message : "Failed to load session",
            isLoadingMessages: false,
          });
        }
      },

      // -- Delete session --
      deleteSession: async (client, sessionId) => {
        // Optimistic: remove from local state immediately.
        const wasActive = get().activeSessionId === sessionId;
        set((s) => {
          const sessions = s.sessions.filter((ses) => ses.id !== sessionId);
          const updates: Partial<ChatState> = { sessions };
          if (wasActive) {
            updates.activeSessionId = null;
            updates.messages = [];
            updates.status = "idle";
            updates.isBusy = false;
            updates.isStreaming = false;
            updates.queue = [];
            updates._agentSessionId = null;
            updates._streamController = null;
          }
          return updates;
        });
        if (wasActive) {
          try {
            localStorage.removeItem(ACTIVE_SESSION_KEY);
          } catch {
            /* ignore */
          }
        }
        try {
          await client.deleteChatSession(sessionId);
        } catch (e) {
          // Restore on failure.
          set({ error: e instanceof Error ? e.message : "Failed to delete session" });
          try {
            const { sessions } = await client.listChatSessions();
            set({ sessions });
          } catch {
            /* ignore */
          }
        }
      },

      // -- Rename session --
      renameSession: async (client, sessionId, title) => {
        // Optimistic update.
        set((s) => ({
          sessions: s.sessions.map((ses) =>
            ses.id === sessionId ? { ...ses, title } : ses,
          ),
        }));
        try {
          await client.updateChatSession(sessionId, { title });
        } catch {
          // Revert on failure.
          try {
            const { sessions } = await client.listChatSessions();
            set({ sessions });
          } catch {
            /* ignore */
          }
        }
      },

      // -- Dequeue --
      dequeueMessage: (id) => {
        set((s) => ({ queue: s.queue.filter((q) => q.id !== id) }));
      },

      // -- Send message (with queue support) --
      sendMessage: async (client, message, model) => {
        const text = message.trim();
        if (!text) return;
        const state = get();

        // If busy, enqueue instead of rejecting.
        if (state.isBusy) {
          set((s) => ({
            queue: [...s.queue, { id: genQueueId(), text, enqueuedAt: Date.now() }],
            inputText: "",
          }));
          return;
        }

        await _runTurn(client, text, model, set, get);
      },

      // -- Stop generation --
      stopGeneration: async (client) => {
        const agentSid = get()._agentSessionId;
        if (agentSid) {
          try {
            await client.interrupt(agentSid);
          } catch {
            /* ignore */
          }
        }
        const ctrl = get()._streamController;
        if (ctrl) {
          ctrl.abort();
        }
        set({
          status: "idle",
          isBusy: false,
          isStreaming: false,
          messages: finalizeStreaming(get().messages),
          _streamController: null,
        });
      },

      // -- Refresh files --
      refreshFiles: async (client) => {
        const agentSid = get()._agentSessionId;
        if (!agentSid) return;
        try {
          const f = await client.files(agentSid);
          set({ files: f.files });
        } catch {
          /* ignore */
        }
      },
    }),
    {
      name: "doomalaysocreate.chat.store",
      partialize: (state) => ({
        effort: state.effort,
        webSearch: state.webSearch,
        deepResearch: state.deepResearch,
        sidebarOpen: state.sidebarOpen,
      }),
    },
  ),
);

// ---------------------------------------------------------------------------
// Internal: run a single agent turn (called by sendMessage and the queue
// drainer).
// ---------------------------------------------------------------------------

async function _runTurn(
  client: AgentClient,
  text: string,
  model: string | undefined,
  set: (partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => void,
  get: () => ChatState,
): Promise<void> {
  // Ensure we have an active chat session.
  let sessionId = get().activeSessionId;
  if (!sessionId) {
    try {
      sessionId = await get().createSession(client, model);
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : "Failed to create session",
      });
      return;
    }
  }

  // Add user message optimistically (pending until backend echoes it).
  const userMsg: ChatMessage = {
    id: genMsgId(),
    role: "user",
    content: text,
    timestamp: Date.now(),
    seq: -1,
    pending: true,
  };
  set((s) => ({
    messages: [...s.messages, userMsg],
    isBusy: true,
    isStreaming: true,
    error: null,
    status: "running",
    inputText: "",
  }));

  // Buffer events for batch persistence at turn end.
  let bufferedEvents: AgentEvent[] = [];
  let since = 0;
  let agentSid: string | null = null;

  try {
    const start: AgentStart = await client.send(
      text,
      undefined, // no session_id — the backend reuses by chat_session_id
      model || undefined,
      get().workspaceId || undefined,
      sessionId,
    );
    agentSid = start.session_id;
    since = 0;
    set({ _agentSessionId: agentSid });

    // Open SSE stream for live events. If SSE fails, fall back to polling.
    await new Promise<void>((resolve) => {
      let settled = false;
      let pollTimer: ReturnType<typeof setInterval> | null = null;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (pollTimer) clearInterval(pollTimer);
        resolve();
      };

      const handleEvent = (ev: AgentEvent) => {
        bufferedEvents.push(ev);
        since = Math.max(since, (ev.i ?? 0) + 1);
        set((s) => ({ messages: appendEvent(s.messages, ev) }));

        // Update status from status events.
        if (ev.type === "status") {
          const st = ev as { state: AgentStatus; cost_usd?: number | null };
          set({ status: st.state });
          if (typeof st.cost_usd === "number") {
            set({ cost: st.cost_usd });
          }
          if (st.state === "idle" || st.state === "error") {
            set((s) => ({ messages: finalizeStreaming(s.messages) }));
            // Give a brief moment for any trailing events, then finish.
            setTimeout(finish, 200);
          }
        }
      };

      const controller = client.stream(
        agentSid!,
        since,
        handleEvent,
        () => {
          // SSE failed — fall back to polling.
          if (pollTimer) return; // already polling
          pollTimer = setInterval(async () => {
            try {
              const snap = await client.poll(agentSid!, since);
              for (const ev of snap.events) handleEvent(ev);
              if (snap.status !== "running" && snap.status !== "starting") {
                set((s) => ({ messages: finalizeStreaming(s.messages) }));
                finish();
              }
            } catch {
              /* keep trying */
            }
          }, 800);
        },
        () => {
          // Stream closed by server — turn done (or socket dropped).
          // Do one final poll to catch any missed events.
          client
            .poll(agentSid!, since)
            .then((snap) => {
              for (const ev of snap.events) handleEvent(ev);
              set((s) => ({ messages: finalizeStreaming(s.messages) }));
              finish();
            })
            .catch(() => finish());
        },
      );
      set({ _streamController: controller });
    });

    // Persist all buffered events to the chat session (backend dedupes).
    if (bufferedEvents.length > 0) {
      await client.persistEvents(sessionId, bufferedEvents);
    }

    // Refresh files (best effort).
    try {
      const f = await client.files(agentSid);
      set({ files: f.files });
    } catch {
      /* ignore */
    }

    // Refresh session list to pick up auto-title.
    try {
      const { sessions } = await client.listChatSessions();
      set({ sessions });
    } catch {
      /* ignore */
    }

    set({
      isBusy: false,
      isStreaming: false,
      _agentSessionId: null,
      _streamController: null,
    });
  } catch (e) {
    set({
      error: e instanceof Error ? e.message : String(e),
      isBusy: false,
      isStreaming: false,
      status: "error",
      messages: finalizeStreaming(get().messages),
      _agentSessionId: null,
      _streamController: null,
    });
    return;
  }

  // Drain the queue: send the next message if any.
  const next = get().queue[0];
  if (next) {
    set((s) => ({ queue: s.queue.slice(1) }));
    await _runTurn(client, next.text, model, set, get);
  }
}

// On store creation, restore workspaceId from localStorage (best effort).
try {
  const stored = localStorage.getItem(WORKSPACE_KEY);
  if (stored) {
    useChatStore.setState({ workspaceId: stored });
  }
} catch {
  /* ignore */
}
