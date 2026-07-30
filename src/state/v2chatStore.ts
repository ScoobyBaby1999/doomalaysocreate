/**
 * V2 Chat Store — with per-chat state isolation + OPFS persistence.
 * Fixed: per-chat restore from backend, token tracking, title updates.
 */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { V2ChatClient, type V2AgentEvent, type V2ChatSession } from "../api/v2chat";
import { saveSession, loadSession, deleteSession as deleteSecureSession } from "../lib/secureStorage";

export interface V2Message {
  id: string;
  role: "user" | "assistant" | "thinking" | "tool" | "tool_result";
  content: string;
  isStreaming?: boolean;
  pending?: boolean;
  toolName?: string;
  toolInput?: string;
  isError?: boolean;
  timestamp: number;
}

interface SessionState {
  model: string | null;
  effort: string | null;
  webSearch: boolean;
  deepResearch: boolean;
}

interface V2ChatState {
  sessions: V2ChatSession[];
  activeSessionId: string | null;
  messages: V2Message[];
  sessionMessages: Record<string, V2Message[]>;
  sessionStates: Record<string, SessionState>;
  currentModel: string | null;
  currentEffort: string | null;
  currentWebSearch: boolean;
  currentDeepResearch: boolean;
  tokenCount: number;
  isBusy: boolean;
  queue: string[];
  error: string | null;
  _streamController: AbortController | null;
  init: (client: V2ChatClient) => Promise<void>;
  sendMessage: (client: V2ChatClient, text: string, opts?: {
    model?: string; effort?: string; web_search?: boolean; deep_research?: boolean;
  }) => Promise<void>;
  createSession: (client: V2ChatClient, model?: string) => Promise<string>;
  switchSession: (client: V2ChatClient, sessionId: string) => Promise<void>;
  deleteSession: (client: V2ChatClient, sessionId: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => void;
  setChatState: (state: Partial<SessionState>) => void;
  stopGeneration: () => void;
}

let msgIdCounter = 0;
const genMsgId = () => `msg-${++msgIdCounter}`;

export const useV2Chat = create<V2ChatState>()(
  persist(
    (set, get) => ({
      sessions: [],
      activeSessionId: null,
      messages: [],
      sessionMessages: {},
      sessionStates: {},
      currentModel: null,
      currentEffort: null,
      currentWebSearch: false,
      currentDeepResearch: false,
      tokenCount: 0,
      isBusy: false,
      queue: [],
      error: null,
      _streamController: null,

      init: async (client) => {
        try {
          const { sessions } = await client.listSessions();
          const activeId = get().activeSessionId || sessions[0]?.id || null;
          set({ sessions, activeSessionId: activeId });
          if (activeId) {
            await get().switchSession(client, activeId);
          }
        } catch (e) {
          console.log("Failed to load sessions from backend, using localStorage:", e);
        }
      },

      sendMessage: async (client, text, opts) => {
        if (!text.trim()) return;
        if (get().isBusy) {
          set(s => ({ queue: [...s.queue, text] }));
          return;
        }
        let sessionId = get().activeSessionId;
        if (!sessionId) {
          sessionId = await get().createSession(client, opts?.model);
        }
        // Use per-chat state
        const state = get();
        const chatState = state.sessionStates[sessionId] || {};
        const sendOpts = opts || {};
        if (!sendOpts.model) sendOpts.model = chatState.model || state.currentModel || undefined;
        if (!sendOpts.effort) sendOpts.effort = chatState.effort || state.currentEffort || undefined;
        if (sendOpts.web_search === undefined) sendOpts.web_search = chatState.webSearch ?? state.currentWebSearch;
        if (sendOpts.deep_research === undefined) sendOpts.deep_research = chatState.deepResearch ?? state.currentDeepResearch;

        const userMsg: V2Message = {
          id: genMsgId(), role: "user", content: text, pending: true, timestamp: Date.now(),
        };
        set(s => ({
          messages: [...s.messages, userMsg],
          sessionMessages: { ...s.sessionMessages, [sessionId]: [...(s.sessionMessages[sessionId] || []), userMsg] },
          isBusy: true, error: null,
        }));

        try {
          const { session_id } = await client.send(sessionId, text, sendOpts);
          const ac = client.stream(
            session_id,
            (ev: V2AgentEvent) => {
              set(state => {
                const newMsgs = handleEvent(state.messages, ev);
                // Save to OPFS
                saveSession(sessionId!, newMsgs).catch(() => {});
                return {
                  messages: newMsgs,
                  sessionMessages: { ...state.sessionMessages, [sessionId!]: newMsgs },
                };
              });
              // Handle title updates
              if (ev.type === "title") {
                set(s => ({
                  sessions: s.sessions.map(ses =>
                    ses.id === sessionId ? { ...ses, title: ev.title || ses.title } : ses
                  ),
                }));
              }
              // SP: Track token count from usage
              if (ev.type === "status" && ev.usage) {
                set({ tokenCount: ev.usage.total_tokens || 0 });
              }
              // Handle status changes
              if (ev.type === "status" && (ev.state === "idle" || ev.state === "error")) {
                set(s => ({ messages: finalizeStreaming(s.messages), isBusy: false }));
              }
              if (ev.type === "error") {
                set({ error: ev.error || "Unknown error" });
              }
            },
            (err: Error) => { set({ error: err.message, isBusy: false }); },
            () => {
              set(s => ({ messages: finalizeStreaming(s.messages), isBusy: false, _streamController: null }));
              const next = get().queue[0];
              if (next) {
                set(s => ({ queue: s.queue.slice(1) }));
                get().sendMessage(client, next, opts);
              }
            },
          );
          set({ _streamController: ac });
        } catch (e) {
          set({ error: e instanceof Error ? e.message : "Failed to send", isBusy: false, messages: finalizeStreaming(get().messages) });
        }
      },

      createSession: async (client, model) => {
        const title = `Chat ${Math.random().toString(16).slice(2, 6)}`;
        try {
          const session = await client.createSession({ model, title });
          set(s => ({
            sessions: [session, ...s.sessions],
            activeSessionId: session.id,
            messages: [],
            sessionMessages: { ...s.sessionMessages, [session.id]: [] },
            sessionStates: { ...s.sessionStates, [session.id]: { model: model || null, effort: null, webSearch: false, deepResearch: false } },
            error: null, isBusy: false, queue: [],
          }));
          return session.id;
        } catch {
          const localId = `local-${Date.now()}`;
          const localSession: V2ChatSession = {
            id: localId, title, model: model || null, workspace_id: null,
            created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
          };
          set(s => ({
            sessions: [localSession, ...s.sessions],
            activeSessionId: localId,
            messages: [],
            sessionMessages: { ...s.sessionMessages, [localId]: [] },
            error: null, isBusy: false, queue: [],
          }));
          return localId;
        }
      },

      switchSession: async (client, sessionId) => {
        const ctrl = get()._streamController;
        if (ctrl) { try { ctrl.abort(); } catch {} }

        // Save current chat state before switching
        const oldId = get().activeSessionId;
        if (oldId) {
          set(s => ({
            sessionStates: {
              ...s.sessionStates,
              [oldId]: {
                model: s.currentModel,
                effort: s.currentEffort,
                webSearch: s.currentWebSearch,
                deepResearch: s.currentDeepResearch,
              },
            },
          }));
        }

        // Load messages from OPFS/memory
        let cachedMsgs = get().sessionMessages[sessionId] || [];
        if (cachedMsgs.length === 0) {
          try {
            const opfsData = await loadSession(sessionId);
            if (opfsData && Array.isArray(opfsData) && opfsData.length > 0) {
              cachedMsgs = opfsData;
              set(s => ({ sessionMessages: { ...s.sessionMessages, [sessionId]: opfsData } }));
            }
          } catch {}
        }

        // Load per-chat state from sessionStates OR from backend session metadata
        let newState = get().sessionStates[sessionId] || { model: null, effort: null, webSearch: false, deepResearch: false };
        
        // Also try to restore from backend session metadata (like V1 did)
        const sessionMeta = get().sessions.find(s => s.id === sessionId);
        if (sessionMeta) {
          if (sessionMeta.model && !newState.model) newState.model = sessionMeta.model;
        }

        // Try to load from backend events endpoint
        try {
          const token = await client.getToken();
          const res = await fetch(`${client.baseUrl}/api/v2/chat/sessions/${sessionId}/events`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (res.ok) {
            const data = await res.json();
            const msgs: V2Message[] = (data.events || []).reduce((acc: V2Message[], ev: any) => handleEvent(acc, ev), []);
            if (msgs.length >= cachedMsgs.length) {
              cachedMsgs = msgs;
              set(s => ({ sessionMessages: { ...s.sessionMessages, [sessionId]: msgs } }));
            }
          }
        } catch {}

        set({
          activeSessionId: sessionId,
          messages: cachedMsgs,
          currentModel: newState.model,
          currentEffort: newState.effort,
          currentWebSearch: newState.webSearch,
          currentDeepResearch: newState.deepResearch,
          tokenCount: 0,
          isBusy: false, error: null, queue: [], _streamController: null,
        });
      },

      deleteSession: async (client, sessionId) => {
        try { await deleteSecureSession(sessionId); } catch {}
        set(s => {
          const sessions = s.sessions.filter(ses => ses.id !== sessionId);
          const sessionMessages = { ...s.sessionMessages };
          delete sessionMessages[sessionId];
          const sessionStates = { ...s.sessionStates };
          delete sessionStates[sessionId];
          const activeSessionId = s.activeSessionId === sessionId
            ? (sessions[0]?.id || null)
            : s.activeSessionId;
          return {
            sessions,
            sessionMessages,
            sessionStates,
            activeSessionId,
            messages: activeSessionId ? (sessionMessages[activeSessionId] || []) : [],
          };
        });
        try {
          const token = await client.getToken();
          await fetch(`${client.baseUrl}/api/v2/chat/sessions/delete`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify({ session_id: sessionId }),
          });
        } catch {}
      },

      renameSession: (sessionId, title) => {
        set(s => ({
          sessions: s.sessions.map(ses =>
            ses.id === sessionId ? { ...ses, title } : ses
          ),
        }));
      },

      setChatState: (state) => {
        set(() => ({
          ...("model" in state ? { currentModel: state.model } : {}),
          ...("effort" in state ? { currentEffort: state.effort } : {}),
          ...("webSearch" in state ? { currentWebSearch: state.webSearch } : {}),
          ...("deepResearch" in state ? { currentDeepResearch: state.deepResearch } : {}),
        }));
        const activeId = get().activeSessionId;
        if (activeId) {
          set(s => ({
            sessionStates: {
              ...s.sessionStates,
              [activeId]: {
                model: get().currentModel,
                effort: get().currentEffort,
                webSearch: get().currentWebSearch,
                deepResearch: get().currentDeepResearch,
              },
            },
          }));
        }
      },

      stopGeneration: () => {
        const ctrl = get()._streamController;
        if (ctrl) { try { ctrl.abort(); } catch {} }
        set(s => ({ isBusy: false, messages: finalizeStreaming(s.messages), _streamController: null }));
      },
    }),
    {
      name: "doomalaysocreate.v2chat",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        sessions: state.sessions,
        activeSessionId: state.activeSessionId,
        sessionStates: state.sessionStates,
      }),
    },
  ),
);

function handleEvent(messages: V2Message[], ev: V2AgentEvent): V2Message[] {
  switch (ev.type) {
    case "user": {
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "user" && !!m.pending);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], pending: false };
        return out;
      }
      return [...messages, { id: genMsgId(), role: "user", content: ev.text || "", timestamp: (ev.ts || 0) * 1000 }];
    }
    case "thinking_delta": {
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "thinking" && !!m.isStreaming);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], content: out[idx].content + (ev.text || "") };
        return out;
      }
      return [...messages, { id: genMsgId(), role: "thinking", content: ev.text || "", isStreaming: true, timestamp: (ev.ts || 0) * 1000 }];
    }
    case "thinking": {
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "thinking" && !!m.isStreaming);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], content: ev.text || "" };
        return out;
      }
      return [...messages, { id: genMsgId(), role: "thinking", content: ev.text || "", isStreaming: true, timestamp: (ev.ts || 0) * 1000 }];
    }
    case "assistant_delta": {
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "assistant" && !!m.isStreaming);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], content: out[idx].content + (ev.text || "") };
        return out;
      }
      return [...messages, { id: genMsgId(), role: "assistant", content: ev.text || "", isStreaming: true, timestamp: (ev.ts || 0) * 1000 }];
    }
    case "assistant_complete":
      return messages.map(m => m.role === "assistant" && m.isStreaming ? { ...m, isStreaming: false } : m);
    case "assistant": {
      const hasStreaming = messages.some(m => m.role === "assistant" && m.isStreaming && m.content);
      if (hasStreaming || ev.text === "(no response from the model)") {
        return messages.map(m => m.role === "assistant" && m.isStreaming ? { ...m, isStreaming: false } : m);
      }
      return [...messages, { id: genMsgId(), role: "assistant", content: ev.text || "", timestamp: (ev.ts || 0) * 1000 }];
    }
    case "tool_use":
      return [...messages, { 
        id: genMsgId(), role: "tool", content: ev.summary || ev.text || "", 
        toolName: ev.name, toolInput: ev.summary, timestamp: (ev.ts || 0) * 1000 
      }];
    case "tool_result":
      return [...messages, { 
        id: genMsgId(), role: "tool_result", content: ev.text || "", 
        isError: (ev as any).is_error, timestamp: (ev.ts || 0) * 1000 
      }];
    default:
      return messages;
  }
}

function finalizeStreaming(messages: V2Message[]): V2Message[] {
  return messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m);
}

function findLastIdx<T>(arr: T[], predicate: (item: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (predicate(arr[i])) return i;
  }
  return -1;
}
