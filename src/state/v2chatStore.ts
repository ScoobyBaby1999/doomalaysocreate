/**
 * V2 Chat Store — simplified, no cursor, no polling, no dedup complexity.
 *
 * Key principles:
 * - SSE stream is the ONLY event source (no polling fallback)
 * - Always since=0 (idempotent event handler)
 * - thinking events REPLACE the bubble (reasoningText is accumulated)
 * - assistant_delta events APPEND to the bubble
 * - user events mark the pending message as confirmed
 * - No _lastEventSeq, no _pre_count, no _msg_cursor
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
  // Sessions
  sessions: V2ChatSession[];
  activeSessionId: string | null;
  
  // Messages
  messages: V2Message[];
  isBusy: boolean;
  
  // Queue
  queue: string[];
  
  // Error
  error: string | null;
  
  // Stream controller
  _streamController: AbortController | null;
  
  // Actions
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
    
    // If busy, queue the message
    if (get().isBusy) {
      set(s => ({ queue: [...s.queue, text] }));
      return;
    }

    let sessionId = get().activeSessionId;
    if (!sessionId) {
      sessionId = await get().createSession(client, opts?.model);
    }

    // Add user message optimistically
    const userMsg: V2Message = {
      id: genMsgId(),
      role: "user",
      content: text,
      pending: true,
      timestamp: Date.now(),
    };
    set(s => ({
      messages: [...s.messages, userMsg],
      isBusy: true,
      error: null,
    }));

    try {
      // Send the message
      const { session_id } = await client.send(sessionId, text, opts || {});

      // Open SSE stream — ALWAYS since=0, idempotent handler
      const ac = client.stream(
        session_id,
        // onEvent — the SINGLE event handler
        (ev: V2AgentEvent) => {
          set(s => ({ messages: handleEvent(s.messages, ev) }));

          // Handle status changes
          if (ev.type === "status" && (ev.state === "idle" || ev.state === "error")) {
            set(s => ({
              messages: finalizeStreaming(s.messages),
              isBusy: false,
            }));
          }

          // Handle errors
          if (ev.type === "error") {
            set({ error: ev.error || "Unknown error" });
          }
        },
        // onError
        (err: Error) => {
          set({ error: err.message, isBusy: false });
        },
        // onClose — stream ended
        () => {
          set(s => ({
            messages: finalizeStreaming(s.messages),
            isBusy: false,
            _streamController: null,
          }));
          
          // Drain the queue
          const next = get().queue[0];
          if (next) {
            set(s => ({ queue: s.queue.slice(1) }));
            get().sendMessage(client, next, opts);
          }
        },
      );
      set({ _streamController: ac });
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : "Failed to send message",
        isBusy: false,
        messages: finalizeStreaming(get().messages),
      });
    }
  },

  createSession: async (client, model) => {
    const session = await client.createSession({ model, title: `Chat ${Math.random().toString(16).slice(2, 6)}` });
    set(s => ({
      sessions: [session, ...s.sessions],
      activeSessionId: session.id,
      messages: [],
      error: null,
      isBusy: false,
      queue: [],
    }));
    return session.id;
  },

  switchSession: async (client, sessionId) => {
    // Abort any in-flight stream
    const ctrl = get()._streamController;
    if (ctrl) ctrl.abort();

    set({
      activeSessionId: sessionId,
      messages: [],
      isBusy: false,
      error: null,
      queue: [],
      _streamController: null,
    });

    // Load messages from the backend
    try {
      // Poll to get existing events
      const { events } = await client.poll(sessionId, 0);
      set(() => ({
        messages: events.reduce((msgs: V2Message[], ev) => handleEvent(msgs, ev), []),
      }));
    } catch {
      // Non-fatal — session might be new
    }
  },

  stopGeneration: () => {
    const ctrl = get()._streamController;
    if (ctrl) ctrl.abort();
    set(s => ({
      isBusy: false,
      messages: finalizeStreaming(s.messages),
      _streamController: null,
    }));
  },
}));

/**
 * Handle a single event — idempotent (safe to replay).
 * This is the ONLY place messages are modified.
 */
function handleEvent(messages: V2Message[], ev: V2AgentEvent): V2Message[] {
  switch (ev.type) {
    case "user": {
      // Mark the pending user message as confirmed
      const pendingIdx = findLastIdx(messages, (m: V2Message) => m.role === "user" && !!m.pending);
      if (pendingIdx !== -1) {
        const out = [...messages];
        out[pendingIdx] = { ...out[pendingIdx], pending: false };
        return out;
      }
      // No pending message — add it (e.g. loading from backend)
      return [...messages, {
        id: genMsgId(), role: "user", content: ev.text || "",
        timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "thinking": {
      // reasoningText is ACCUMULATED — REPLACE the thinking bubble
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "thinking" && !!m.isStreaming);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], content: ev.text || "" };
        return out;
      }
      return [...messages, {
        id: genMsgId(), role: "thinking", content: ev.text || "",
        isStreaming: true, timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "assistant_delta": {
      // APPEND to the streaming assistant bubble
      const idx = findLastIdx(messages, (m: V2Message) => m.role === "assistant" && !!m.isStreaming);
      if (idx !== -1) {
        const out = [...messages];
        out[idx] = { ...out[idx], content: out[idx].content + (ev.text || "") };
        return out;
      }
      return [...messages, {
        id: genMsgId(), role: "assistant", content: ev.text || "",
        isStreaming: true, timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "assistant_complete": {
      // Finalize the streaming bubble
      return messages.map(m =>
        m.role === "assistant" && m.isStreaming ? { ...m, isStreaming: false } : m
      );
    }

    case "assistant": {
      // Full assistant message (non-streaming fallback or placeholder)
      // Skip if we already have a streaming bubble with content
      const hasStreaming = messages.some(m => m.role === "assistant" && m.isStreaming && m.content);
      if (hasStreaming) {
        return messages.map(m =>
          m.role === "assistant" && m.isStreaming ? { ...m, isStreaming: false } : m
        );
      }
      // Skip placeholder
      if (ev.text === "(no response from the model)") {
        return messages.map(m =>
          m.role === "assistant" && m.isStreaming ? { ...m, isStreaming: false } : m
        );
      }
      return [...messages, {
        id: genMsgId(), role: "assistant", content: ev.text || "",
        timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "tool_use": {
      return [...messages, {
        id: genMsgId(), role: "tool", content: ev.summary || "",
        toolName: ev.name, timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "tool_result": {
      return [...messages, {
        id: genMsgId(), role: "tool_result", content: ev.text || "",
        timestamp: (ev.ts || 0) * 1000,
      }];
    }

    case "status":
    case "error":
    case "title":
      // These don't add messages — they update state
      return messages;

    default:
      return messages;
  }
}

function finalizeStreaming(messages: V2Message[]): V2Message[] {
  return messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m);
}


/** ES2015-compatible findLastIndex. */
function findLastIdx<T>(arr: T[], predicate: (item: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (predicate(arr[i])) return i;
  }
  return -1;
}
