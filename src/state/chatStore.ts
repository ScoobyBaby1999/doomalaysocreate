/**
 * Chat Store — Centralized state management for agent chat.
 *
 * Replaces the scattered useState in AgentChat with a single Zustand store
 * that handles: sessions, messages, streaming, model selection, and persistence.
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
import type { Settings } from "../api/panel";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "thinking" | "tool" | "status" | "panel";
  content: string;
  isError?: boolean;
  isStreaming?: boolean;
  toolName?: string;
  toolSummary?: string;
  panelStatus?: string;
  costUsd?: number | null;
  timestamp: number;
  seq: number;
}

export interface ChatState {
  // Sessions
  sessions: ChatSession[];
  activeSessionId: string | null;
  isLoadingSessions: boolean;
  sessionError: string | null;

  // Messages (derived from events)
  messages: ChatMessage[];
  isStreaming: boolean;
  status: AgentStatus;

  // Input
  inputText: string;
  isBusy: boolean;
  error: string | null;

  // Settings per session
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
  sendMessage: (
    client: AgentClient,
    message: string,
    model?: string
  ) => Promise<void>;
  stopGeneration: (client: AgentClient) => Promise<void>;
  refreshFiles: (client: AgentClient) => Promise<void>;
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let nextMsgId = 0;
function genMsgId(): string {
  return `msg-${++nextMsgId}-${Date.now()}`;
}

/** Convert raw AgentEvents into normalized ChatMessages. */
function eventsToMessages(events: AgentEvent[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  const deltaBuffers: Map<string, { content: string; seq: number }> = new Map();

  for (const ev of events) {
    const base = {
      timestamp: (ev.ts || Date.now() / 1000) * 1000,
      seq: ev.i ?? 0,
    };

    switch (ev.type) {
      case "user": {
        messages.push({
          id: genMsgId(),
          role: "user",
          content: ev.text || "",
          ...base,
        });
        break;
      }
      case "assistant": {
        messages.push({
          id: genMsgId(),
          role: "assistant",
          content: ev.text || "",
          ...base,
        });
        break;
      }
      case "assistant_delta": {
        // Merge consecutive deltas
        const last = messages[messages.length - 1];
        if (last && last.role === "assistant" && last.isStreaming) {
          last.content += ev.text || "";
          last.seq = base.seq;
        } else {
          messages.push({
            id: genMsgId(),
            role: "assistant",
            content: ev.text || "",
            isStreaming: true,
            ...base,
          });
        }
        break;
      }
      case "thinking": {
        messages.push({
          id: genMsgId(),
          role: "thinking",
          content: ev.text || "",
          ...base,
        });
        break;
      }
      case "thinking_delta": {
        const last = messages[messages.length - 1];
        if (last && last.role === "thinking" && last.isStreaming) {
          last.content += ev.text || "";
          last.seq = base.seq;
        } else {
          messages.push({
            id: genMsgId(),
            role: "thinking",
            content: ev.text || "",
            isStreaming: true,
            ...base,
          });
        }
        break;
      }
      case "tool_use": {
        messages.push({
          id: genMsgId(),
          role: "tool",
          content: "",
          toolName: ev.name || "tool",
          toolSummary: ev.summary || "",
          ...base,
        });
        break;
      }
      case "tool_result": {
        messages.push({
          id: genMsgId(),
          role: "tool",
          content: ev.text || "",
          isError: ev.is_error,
          toolName: "result",
          ...base,
        });
        break;
      }
      case "status": {
        const st = ev as any;
        if (st.state === "idle" && st.detail === "interrupted") {
          messages.push({
            id: genMsgId(),
            role: "status",
            content: "— stopped —",
            ...base,
          });
        } else if (st.state === "error") {
          messages.push({
            id: genMsgId(),
            role: "status",
            content: st.detail || "Agent error",
            isError: true,
            costUsd: st.cost_usd,
            ...base,
          });
        } else {
          // Track cost from status events
          if (typeof st.cost_usd === "number") {
            // Will be handled by the store
          }
        }
        break;
      }
      case "panel": {
        messages.push({
          id: genMsgId(),
          role: "panel",
          content: ev.task_name || "Panel",
          panelStatus: ev.status || "starting",
          ...base,
        });
        break;
      }
    }
  }

  // Mark the last assistant/thinking message as no longer streaming
  // (this will be set to true again when new deltas arrive)
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant" || messages[i].role === "thinking") {
      messages[i].isStreaming = false;
      break;
    }
  }

  return messages;
}

/** Merge new events into existing messages efficiently. */
function mergeEvents(
  existing: ChatMessage[],
  newEvents: AgentEvent[]
): ChatMessage[] {
  if (newEvents.length === 0) return existing;

  // Find the highest seq we already have
  const maxSeq = existing.reduce(
    (max, m) => Math.max(max, m.seq),
    -1
  );

  // Only process events with seq > maxSeq
  const freshEvents = newEvents.filter((e) => (e.i ?? -1) > maxSeq);
  if (freshEvents.length === 0) {
    // No new events by seq, but we might have delta updates
    // Re-process everything to catch delta merges
    const allEvents = [...existing.map(() => null), ...newEvents].filter(Boolean);
    return eventsToMessages(allEvents as AgentEvent[]);
  }

  // Convert fresh events to messages and append
  const freshMessages = eventsToMessages(freshEvents);

  // If the last existing message is an assistant/thinking and the first fresh
  // message is a delta of the same type, merge them
  const result = [...existing];
  if (
    result.length > 0 &&
    freshMessages.length > 0 &&
    (freshMessages[0].role === "assistant" || freshMessages[0].role === "thinking") &&
    freshMessages[0].isStreaming &&
    result[result.length - 1].role === freshMessages[0].role
  ) {
    result[result.length - 1].content += freshMessages[0].content;
    result[result.length - 1].isStreaming = true;
    result.push(...freshMessages.slice(1));
  } else {
    result.push(...freshMessages);
  }

  return result;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

const ACTIVE_SESSION_KEY = "doomalaysocreate.chat.active_session";

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      // -- State --
      sessions: [],
      activeSessionId: null,
      isLoadingSessions: false,
      sessionError: null,

      messages: [],
      isStreaming: false,
      status: "idle",

      inputText: "",
      isBusy: false,
      error: null,

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

      // -- Simple setters --
      setInputText: (text) => set({ inputText: text }),
      setEffort: (effort) => set({ effort }),
      toggleWebSearch: () => set((s) => ({ webSearch: !s.webSearch })),
      toggleDeepResearch: () => set((s) => ({ deepResearch: !s.deepResearch })),
      setSidebarOpen: (open) => set({ sidebarOpen: open }),
      setFileDrawerOpen: (open) => set({ fileDrawerOpen: open }),
      setPanelDrawerOpen: (open) => set({ panelDrawerOpen: open }),
      setWorkspaceId: (id) => set({ workspaceId: id }),

      // -- Load sessions --
      loadSessions: async (client) => {
        set({ isLoadingSessions: true, sessionError: null });
        try {
          const { sessions } = await client.listChatSessions();
          set({ sessions, isLoadingSessions: false });

          // If we have an active session stored, try to restore it
          const stored = localStorage.getItem(ACTIVE_SESSION_KEY);
          if (stored && sessions.find((s) => s.id === stored)) {
            // Don't auto-switch; just mark it
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
          const cs = await client.createChatSession(undefined, model);
          set((s) => ({
            sessions: [cs, ...s.sessions],
            activeSessionId: cs.id,
            messages: [],
            status: "idle",
            isBusy: false,
            error: null,
            cost: null,
            panelInvocations: [],
            files: [],
          }));
          localStorage.setItem(ACTIVE_SESSION_KEY, cs.id);
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
        set({
          isBusy: true,
          activeSessionId: sessionId,
          messages: [],
          error: null,
          status: "idle",
          panelInvocations: [],
        });
        try {
          const evData = await client.getChatEvents(sessionId);
          const messages = eventsToMessages(evData.events);
          set({ messages, isBusy: false });
          localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
        } catch (e) {
          set({
            error: e instanceof Error ? e.message : "Failed to load session",
            isBusy: false,
          });
        }
      },

      // -- Delete session --
      deleteSession: async (client, sessionId) => {
        try {
          await client.deleteChatSession(sessionId);
          set((s) => {
            const sessions = s.sessions.filter((ses) => ses.id !== sessionId);
            const updates: Partial<ChatState> = { sessions };
            if (s.activeSessionId === sessionId) {
              updates.activeSessionId = null;
              updates.messages = [];
              updates.status = "idle";
            }
            return updates;
          });
          localStorage.removeItem(ACTIVE_SESSION_KEY);
        } catch (e) {
          set({
            error: e instanceof Error ? e.message : "Failed to delete session",
          });
        }
      },

      // -- Send message --
      sendMessage: async (client, message, model) => {
        const state = get();
        if (state.isBusy || !message.trim()) return;

        // Ensure we have an active session
        let sessionId = state.activeSessionId;
        if (!sessionId) {
          sessionId = await get().createSession(client, model);
        }

        // Add user message immediately
        const userMsg: ChatMessage = {
          id: genMsgId(),
          role: "user",
          content: message.trim(),
          timestamp: Date.now(),
          seq: state.messages.length,
        };

        set((s) => ({
          messages: [...s.messages, userMsg],
          isBusy: true,
          isStreaming: true,
          error: null,
          status: "running",
          inputText: "",
        }));

        try {
          // Start the agent
          const start: AgentStart = await client.send(
            message.trim(),
            undefined, // no existing agent session
            model || undefined,
            state.workspaceId || undefined,
            sessionId
          );

          // Poll for events
          let since = 0;
          const pollInterval = 300;
          const maxWait = 1000 * 60 * 30; // 30 min timeout
          const startTime = Date.now();

          while (Date.now() - startTime < maxWait) {
            const snap = await client.poll(start.session_id, since);

            if (snap.events.length > 0) {
              // Merge new events into messages
              set((s) => {
                const merged = mergeEvents(s.messages, snap.events);
                return { messages: merged };
              });
              since = snap.next;

              // Persist events to backend chat session
              if (start.chat_session_id) {
                try {
                  await client.persistEvents(start.chat_session_id, snap.events);
                } catch {
                  // Non-fatal
                }
              }
            }

            set({ status: snap.status });

            if (snap.status !== "running" && snap.status !== "starting") {
              // Done
              if (snap.status !== "error") {
                // Refresh files
                try {
                  const f = await client.files(start.session_id);
                  set({ files: f.files });
                } catch {
                  // ignore
                }
              }
              break;
            }

            await new Promise((r) => setTimeout(r, pollInterval));
          }

          // Final sync: reload full events from DB
          try {
            const evData = await client.getChatEvents(sessionId);
            set({ messages: eventsToMessages(evData.events) });
          } catch {
            // Non-fatal
          }

          // Refresh session list to get updated title
          try {
            const { sessions } = await client.listChatSessions();
            set({ sessions });
          } catch {
            // Non-fatal
          }

          set({ isBusy: false, isStreaming: false });
        } catch (e) {
          set({
            error: e instanceof Error ? e.message : String(e),
            isBusy: false,
            isStreaming: false,
            status: "error",
          });
        }
      },

      // -- Stop generation --
      stopGeneration: async (client) => {
        // We need the current agent session ID to stop it
        // For now, this is a placeholder - the agent session ID
        // would need to be tracked in the store during sendMessage
        set({ status: "idle", isBusy: false, isStreaming: false });
      },

      // -- Refresh files --
      refreshFiles: async (client) => {
        // This requires the agent session ID which isn't stored in the store
        // It's populated after a successful run
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
    }
  )
);
