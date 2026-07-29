/**
 * V2 Chat Store — simplified, no cursor, no polling, no dedup complexity.
 */
import { create } from "zustand";
import { V2ChatClient, type V2AgentEvent, type V2ChatSession } from "../api/v2chat";

export interface V2Message {
  id: string;
  role: "user" | "assistant" | "thinking" | "tool" | "tool_result";
  content: string;
  isStreaming?: boolean;
  pending?: boolean;
  toolName?: string;
  timestamp: number;
}

interface V2ChatState {
  sessions: V2ChatSession[];
  activeSessionId: string | null;
  messages: V2Message[];
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
  stopGeneration: () => void;
}

let msgIdCounter = 0;
const genMsgId = () => `msg-${++msgIdCounter}`;

export const useV2Chat = create<V2ChatState>((set, get) => ({
  sessions: [],
  activeSessionId: null,
  messages: [],
  isBusy: false,
  queue: [],
  error: null,
  _streamController: null,

  init: async (client) => {
    try {
      const { sessions } = await client.listSessions();
      const activeId = sessions[0]?.id ?? null;
      set({ sessions, activeSessionId: activeId });
      if (activeId) {
        await get().switchSession(client, activeId);
      }
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "Failed to load sessions" });
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
    const userMsg: V2Message = {
      id: genMsgId(), role: "user", content: text, pending: true, timestamp: Date.now(),
    };
    set(s => ({ messages: [...s.messages, userMsg], isBusy: true, error: null }));
    try {
      const { session_id } = await client.send(sessionId, text, opts || {});
      const ac = client.stream(
        session_id,
        (ev: V2AgentEvent) => {
          set(state => ({ messages: handleEvent(state.messages, ev) }));
          if (ev.type === "title") {
            set(s => ({ sessions: s.sessions.map(ses => ses.id === sessionId ? { ...ses, title: ev.title || ses.title } : ses) }));
          }
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
    const session = await client.createSession({ model, title: `Chat ${Math.random().toString(16).slice(2, 6)}` });
    set(s => ({ sessions: [session, ...s.sessions], activeSessionId: session.id, messages: [], error: null, isBusy: false, queue: [] }));
    return session.id;
  },

  switchSession: async (client, sessionId) => {
    const ctrl = get()._streamController;
    if (ctrl) { try { ctrl.abort(); } catch {} }
    set({ activeSessionId: sessionId, messages: [], isBusy: false, error: null, queue: [], _streamController: null });
    try {
      const token = await client.getToken();
      const res = await fetch(`${client.baseUrl}/api/v2/chat/sessions/${sessionId}/events`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        const msgs: V2Message[] = (data.events || []).reduce((acc: V2Message[], ev: any) => handleEvent(acc, ev), []);
        set({ messages: msgs });
      }
    } catch { /* new session */ }
  },

  stopGeneration: () => {
    const ctrl = get()._streamController;
    if (ctrl) { try { ctrl.abort(); } catch {} }
    set(s => ({ isBusy: false, messages: finalizeStreaming(s.messages), _streamController: null }));
  },
}));

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
      return [...messages, { id: genMsgId(), role: "tool", content: ev.summary || ev.text || "", toolName: ev.name, timestamp: (ev.ts || 0) * 1000 }];
    case "tool_result":
      return [...messages, { id: genMsgId(), role: "tool_result", content: ev.text || "", timestamp: (ev.ts || 0) * 1000 }];
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
