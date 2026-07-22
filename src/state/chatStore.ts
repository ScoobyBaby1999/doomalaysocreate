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
  type MonitorEvent,
  type MonitorJob,
  type MonitorSuggestion,
} from "../api/agent";
import type { Template, TemplateKind } from "../api/templates";
// BATCH-3 Task 2 — import useModelStore at the top level so we can read the
// current model synchronously in _persistChatMeta. This creates a one-way
// dependency (chatStore → model-store); model-store does NOT import chatStore,
// so there's no circular dep. The previous dynamic import was a workaround
// for a non-existent circular dep that triggered a Vite warning.
import { useModelStore } from "../lib/model-store";

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

export interface ChatSource {
  url: string;
  name?: string;
  snippet?: string;
}

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
  /** Sources cited by this assistant turn (web search results). */
  sources?: ChatSource[];
  /** Token usage for this turn (input/output/reasoning). */
  tokensIn?: number;
  tokensOut?: number;
  tokensReasoning?: number;
  /** Stage hint from the backend ("searching", "reading:3", "synthesizing").
   *  Rendered as a one-line status indicator above the bubble. */
  stage?: string;
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

/** Pinned session IDs (survive refresh; rendered in their own group). */
const PINNED_KEY = "doomalaysocreate.chat.pinned";

function loadPinned(): string[] {
  try {
    const raw = localStorage.getItem(PINNED_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function savePinned(ids: string[]): void {
  try {
    localStorage.setItem(PINNED_KEY, JSON.stringify(ids));
  } catch {
    /* ignore */
  }
}

/** Judge-panel configuration exposed by the ToolIcons "Judge" popover. */
export interface JudgeConfig {
  count: number; // 1-6
  /** Accepts a legacy template id ("critique" | "verify" | "improve" |
   *  "debate") OR a template library template id. */
  template: string;
}

export type BusyMode = "queue" | "stop";

export interface ChatState {
  // Sessions
  sessions: ChatSession[];
  activeSessionId: string | null;
  isLoadingSessions: boolean;
  sessionError: string | null;
  /** True while we're restoring the active session's events on mount/switch. */
  isLoadingMessages: boolean;
  /** Pinned session IDs — rendered first in the WorkspaceBrowser. */
  pinnedSessionIds: string[];

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
  // BATCH-3 Task 5 — effort is now `string` (not the hardcoded union) so
  // models with non-standard variants (e.g. "low|mid|ultra|max") can be
  // represented. The legacy "low|med|high|max" defaults still work — the
  // store + backend both accept any string.
  effort: string;
  webSearch: boolean;
  deepResearch: boolean;
  mode: "auto" | "build" | "plan";
  /** Web-search template override (empty = regular). Accepts legacy IDs
   *  ("breadth" | "deepdive" | "compare" | "factcheck") OR a template
   *  library template id (so library templates flow through to the backend). */
  webTemplate: string;
  /** Deep-research template override (empty = default). Accepts legacy IDs
   *  ("react" | "extended") OR a template library template id. */
  deepTemplate: string;
  /** Judge-panel configuration for the chat-integrated popover. */
  judge: JudgeConfig;
  /** What happens when the user presses Enter while busy:
   *  - "queue": enqueue the message (default — 1-up philosophy)
   *  - "stop": interrupt the running turn and send the new one */
  busyMode: BusyMode;

  // Files
  files: AgentFile[];
  fileDrawerOpen: boolean;

  // Panel
  panelInvocations: PanelInvocation[];
  panelDrawerOpen: boolean;

  // Session sidebar
  sidebarOpen: boolean;

  // QueueMonitor panel (jobs + suggestions)
  queueMonitorOpen: boolean;
  jobs: MonitorJob[];
  suggestions: MonitorSuggestion[];

  // Template Library overlay (browse/create/heart/download templates)
  templateLibraryOpen: boolean;
  templateLibraryKind: string; // 'websearch' | 'deepresearch' | 'judge' | 'chat' | 'custom' | ''
  templateLibraryTab: "mine" | "explore";

  // Derived
  cost: number | null;
  /** Running total cost across the active session. */
  sessionCost: number;
  /** Token usage from the last agent turn (cost transparency). */
  lastUsage: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    reasoning_tokens?: number;
  } | null;
  currentModel: string | null;
  /** The model the backend actually resolved + is running (for verification). */
  resolvedModel: string | null;
  /** The provider serving the current/last agent turn (for verification). */
  resolvedProvider: string | null;
  /** The model the user requested (may differ from resolvedModel if the
   *  backend fell back). Shown in the UI so the user can spot mismatches. */
  requestedModel: string | null;
  workspaceId: string | null;

  // Internal: current agent session ID (for Stop and SSE)
  _agentSessionId: string | null;
  _streamController: AbortController | null;
  /** Highest event seq we've seen for the current agent session. Used as the
   *  SSE cursor for the next turn so we don't replay the whole transcript. */
  _lastEventSeq: number;
  /** Monitor SSE controller (single per store instance). */
  _monitorController: AbortController | null;

  // Actions
  setInputText: (text: string) => void;
  // BATCH-3 Task 5 — setEffort accepts any string (model-specific variants).
  setEffort: (effort: string) => void;
  toggleWebSearch: () => void;
  toggleDeepResearch: () => void;
  setMode: (mode: "auto" | "build" | "plan") => void;
  setWebTemplate: (t: string) => void;
  setDeepTemplate: (t: string) => void;
  setJudge: (cfg: Partial<JudgeConfig>) => void;
  setBusyMode: (m: BusyMode) => void;
  /** Reset all tool selections (effort=med, web=off, deep=off, templates cleared). */
  resetTools: () => void;
  setSidebarOpen: (open: boolean) => void;
  setFileDrawerOpen: (open: boolean) => void;
  setPanelDrawerOpen: (open: boolean) => void;
  setQueueMonitorOpen: (open: boolean) => void;
  setWorkspaceId: (id: string | null) => void;
  togglePin: (sessionId: string) => void;

  // Template Library actions
  openTemplateLibrary: (kind: string) => void;
  closeTemplateLibrary: () => void;
  setTemplateLibraryTab: (tab: "mine" | "explore") => void;
  /** Apply a template to the current chat tool (sets the appropriate
   *  template slot based on the template's kind). */
  applyTemplate: (template: Template) => void;

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

  // Judge panel — fired from the ToolIcons popover. Posts to /api/chat/judge
  // and inserts the merged result as an assistant message.
  runJudge: (client: AgentClient, input?: string, model?: string) => Promise<void>;

  // Monitor SSE — opens /api/monitor and routes events into jobs/suggestions.
  connectMonitor: (client: AgentClient) => void;
  disconnectMonitor: () => void;
  cancelJob: (client: AgentClient, jobId: string) => Promise<void>;
  applySuggestion: (id: string) => void;

  // BATCH-2 Task 5.6 — debounced per-chat metadata persist. Called by
  // every tool setter (setEffort, toggleWebSearch, setMode, etc.) so the
  // backend stores the current configuration on the active chat session.
  // Switching chats restores the saved config (see switchSession).
  _persistChatMeta: () => void;
  _persistChatMetaTimer: ReturnType<typeof setTimeout> | null;
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

// BATCH-2 Task 5.6 — module-level ref to the most-recently-used AgentClient.
// The chatStore actions receive `client` as a parameter (no ref held), but
// the debounced _persistChatMeta needs to fire AFTER the setter returns,
// when no client is in scope. We stash the last-seen client here so the
// debounced persist can use it. Updated by loadSessions / sendMessage /
// switchSession / etc. whenever they're called with a fresh client.
const _lastClientRef: { current: AgentClient | null } = { current: null };

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
        // Merge consecutive thinking events into one bubble.
        // The backend accumulates thinking text in one event (same seq),
        // but the post-turn walk may emit a separate one. Merge by checking
        // if the last message is a thinking bubble.
        if (messages.length > 0 && messages[messages.length - 1].role === "thinking") {
          const last = messages[messages.length - 1];
          const newText = ev.text || "";
          const oldText = last.content || "";
          if (newText.length >= oldText.length && newText.startsWith(oldText)) {
            messages[messages.length - 1] = { ...last, content: newText, seq };
          } else if (!oldText.startsWith(newText)) {
            messages[messages.length - 1] = { ...last, content: oldText + newText, seq };
          }
        } else {
          messages.push({
            id: genMsgId(),
            role: "thinking",
            content: ev.text || "",
            timestamp: ts,
            seq,
          });
        }
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
          stage?: string;
          usage?: { input_tokens: number; output_tokens: number; total_tokens: number; reasoning_tokens?: number };
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
        } else if (st.stage) {
          // Stage hint while running — attach to the most recent streaming
          // assistant bubble so the status indicator updates above it.
          if (streamingAssistantIdx !== -1) {
            const cur = messages[streamingAssistantIdx];
            messages[streamingAssistantIdx] = { ...cur, stage: st.stage };
          }
        }
        // status events don't reset streaming indexes — they wrap a turn.
        break;
      }
      case "sources": {
        // Attach sources to the most recent assistant message (the one that
        // produced them via web_search). If there isn't one yet, stash on a
        // new assistant placeholder so the UI can still render the citation.
        const srcs = ev.sources || [];
        if (streamingAssistantIdx !== -1) {
          const cur = messages[streamingAssistantIdx];
          const merged = [...(cur.sources || []), ...srcs];
          // Dedupe by URL so a re-emit doesn't double-list.
          const seen = new Set<string>();
          const dedup = merged.filter((s) => {
            if (seen.has(s.url)) return false;
            seen.add(s.url);
            return true;
          });
          messages[streamingAssistantIdx] = { ...cur, sources: dedup };
        } else {
          // Find the last assistant message in the list.
          for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].role === "assistant") {
              const cur = messages[i];
              const merged = [...(cur.sources || []), ...srcs];
              const seen = new Set<string>();
              const dedup = merged.filter((s) => {
                if (seen.has(s.url)) return false;
                seen.add(s.url);
                return true;
              });
              messages[i] = { ...cur, sources: dedup };
              break;
            }
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

/** Append a single new event to the existing message list (immutable).
 *  DEDUP: every event type checks by seq before appending, so a replayed
 *  event (e.g. from a since=0 re-poll) never creates a duplicate message.
 *  Delta events (assistant_delta, thinking_delta) merge into the last streaming
 *  message of the matching role; they don't use seq dedup because multiple
 *  deltas share the growing message. */
function appendEvent(existing: ChatMessage[], ev: AgentEvent): ChatMessage[] {
  const ts = (ev.ts || Date.now() / 1000) * 1000;
  const seq = ev.i ?? existing.length;
  const out = existing.slice();

  // Universal seq dedup for non-delta events. Prevents duplicated
  // assistant/thinking/tool/status/panel messages when the SSE cursor resets
  // or the backend replays events. EXCEPTION: thinking events use the SAME
  // seq for merged updates (the backend replaces the text of the last
  // thinking event and re-emits it with the same i). So thinking events
  // bypass dedup and always go through the merge logic in the switch.
  if (ev.type !== "assistant_delta" && ev.type !== "thinking_delta" && ev.type !== "thinking") {
    if (out.some((m) => m.seq === seq && m.role !== "user")) {
      return out; // already have this event
    }
  }

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
      // A non-delta assistant event is a complete message. If there's a
      // streaming assistant message at the end, finalize it with this content.
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "assistant" && out[i].isStreaming) {
          out[i] = { ...out[i], content: ev.text || out[i].content, isStreaming: false, seq };
          return out;
        }
      }
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
      // The backend emits multiple "thinking" events as reasoning streams in.
      // emit() ACCUMULATES the text (appends fragments) and sends the
      // accumulated text with the SAME seq (i) to SSE. So consecutive thinking
      // events with the same seq should MERGE into one growing bubble.
      // We also merge by position: if the last thinking bubble is streaming,
      // merge into it. This handles both same-seq and positional merging.
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "thinking") {
          // Found a thinking bubble — merge into it.
          const newText = ev.text || "";
          const oldText = out[i].content || "";
          // The backend sends accumulated text (growing). If the new text
          // starts with the old text, it's the accumulated version — replace.
          // Otherwise append (fragment mode).
          if (newText.length >= oldText.length && newText.startsWith(oldText)) {
            out[i] = { ...out[i], content: newText, seq, timestamp: ts, isStreaming: true };
          } else if (oldText.startsWith(newText)) {
            // New text is a prefix of old — ignore (stale/duplicate)
            return out;
          } else {
            // Fragment — append
            out[i] = { ...out[i], content: oldText + newText, seq, timestamp: ts, isStreaming: true };
          }
          return out;
        }
        // If we hit a non-thinking message, stop scanning — start a new bubble.
        if (out[i].role === "assistant" || out[i].role === "user" || out[i].role === "tool") {
          break;
        }
      }
      // No existing thinking bubble — start a new one.
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
        stage?: string;
        usage?: { input_tokens: number; output_tokens: number; total_tokens: number; reasoning_tokens?: number };
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
      } else if (st.usage) {
        // End-of-turn status: attach token usage to the last assistant
        // message so the bubble can show tokensIn/tokensOut/tokensReasoning.
        for (let i = out.length - 1; i >= 0; i--) {
          if (out[i].role === "assistant") {
            out[i] = {
              ...out[i],
              tokensIn: st.usage.input_tokens,
              tokensOut: st.usage.output_tokens,
              tokensReasoning: st.usage.reasoning_tokens,
              costUsd: typeof st.cost_usd === "number" ? st.cost_usd : out[i].costUsd,
            };
            break;
          }
        }
      } else if (st.stage) {
        // Mid-turn stage hint — attach to the most recent streaming assistant
        // so the bubble shows "Searching the web…" / "Reading 3 sources…" / etc.
        for (let i = out.length - 1; i >= 0; i--) {
          if (out[i].role === "assistant") {
            out[i] = { ...out[i], stage: st.stage };
            break;
          }
        }
      }
      return out;
    }
    case "sources": {
      const srcs = ev.sources || [];
      // Attach to the most recent assistant bubble (streaming or not).
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "assistant") {
          const merged = [...(out[i].sources || []), ...srcs];
          const seen = new Set<string>();
          const dedup = merged.filter((s) => {
            if (seen.has(s.url)) return false;
            seen.add(s.url);
            return true;
          });
          out[i] = { ...out[i], sources: dedup };
          break;
        }
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

/** Mark ALL streaming messages as done (called when a turn ends).
 *  Finalizes every isStreaming message, not just the last — fixes the bug
 *  where a thinking + assistant both streaming left one with a perpetual cursor. */
function finalizeStreaming(existing: ChatMessage[]): ChatMessage[] {
  let changed = false;
  const out = existing.map((m) => {
    if (m.isStreaming) {
      changed = true;
      return { ...m, isStreaming: false };
    }
    return m;
  });
  return changed ? out : existing;
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
      pinnedSessionIds: loadPinned(),

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
      mode: "auto",
      webTemplate: "",
      deepTemplate: "",
      judge: { count: 3, template: "critique" },
      busyMode: "queue",

      files: [],
      fileDrawerOpen: false,

      panelInvocations: [],
      panelDrawerOpen: false,

      sidebarOpen: false,
      queueMonitorOpen: false,
      jobs: [],
      suggestions: [],
      cost: null,
      sessionCost: 0,
      lastUsage: null,
      currentModel: null,
      resolvedModel: null,
      resolvedProvider: null,
      requestedModel: null,
      workspaceId: null,

      // Template Library overlay — closed by default, no kind filter, on the
      // "My Templates" tab.
      templateLibraryOpen: false,
      templateLibraryKind: "",
      templateLibraryTab: "mine",

      _agentSessionId: null,
      _streamController: null,
      _lastEventSeq: 0,
      _monitorController: null,

      // -- Simple setters --
      // BATCH-2 Task 5.6 — every setter that changes a per-chat setting
      // also fires a debounced persist to the backend so the chat session
      // "remembers" its configuration when the user switches away + back.
      setInputText: (text) => set({ inputText: text }),
      setEffort: (effort) => {
        set({ effort });
        get()._persistChatMeta();
      },
      toggleWebSearch: () => {
        set((s) => ({ webSearch: !s.webSearch }));
        get()._persistChatMeta();
      },
      toggleDeepResearch: () => {
        set((s) => ({ deepResearch: !s.deepResearch }));
        get()._persistChatMeta();
      },
      setMode: (mode) => {
        set({ mode });
        get()._persistChatMeta();
      },
      setWebTemplate: (t) => {
        set({ webTemplate: t });
        get()._persistChatMeta();
      },
      setDeepTemplate: (t) => {
        set({ deepTemplate: t });
        get()._persistChatMeta();
      },
      setJudge: (cfg) => {
        set((s) => ({ judge: { ...s.judge, ...cfg } }));
        get()._persistChatMeta();
      },
      setBusyMode: (m) => set({ busyMode: m }),
      resetTools: () => {
        set({
          effort: "med",
          webSearch: false,
          deepResearch: false,
          webTemplate: "",
          deepTemplate: "",
          mode: "auto",
          judge: { count: 3, template: "critique" },
        });
        get()._persistChatMeta();
      },
      setSidebarOpen: (open) => set({ sidebarOpen: open }),
      setFileDrawerOpen: (open) => set({ fileDrawerOpen: open }),
      setPanelDrawerOpen: (open) => set({ panelDrawerOpen: open }),
      setQueueMonitorOpen: (open) => set({ queueMonitorOpen: open }),
      // Template Library actions
      openTemplateLibrary: (kind) =>
        set({ templateLibraryOpen: true, templateLibraryKind: kind || "" }),
      closeTemplateLibrary: () => set({ templateLibraryOpen: false }),
      setTemplateLibraryTab: (tab) => set({ templateLibraryTab: tab }),
      applyTemplate: (template) => {
        // Route the template to the correct tool slot based on its kind.
        // The frontend stores a single "current" template per kind; the
        // backend's chat endpoint accepts web_template / deep_template /
        // judge.template per turn (see AgentClient.send). The backend treats
        // a non-legacy string as a template library template id and looks
        // it up; legacy ids ("breadth", "react", "critique", etc.) still
        // work as before.
        const kind = template.kind as TemplateKind;
        switch (kind) {
          case "websearch":
            set({
              webSearch: true,
              webTemplate: template.id,
              templateLibraryOpen: false,
            });
            break;
          case "deepresearch":
            set({
              deepResearch: true,
              deepTemplate: template.id,
              templateLibraryOpen: false,
            });
            break;
          case "judge":
            set({
              judge: { count: get().judge.count, template: template.id },
              templateLibraryOpen: false,
            });
            break;
          case "chat":
          case "custom":
          default:
            // For chat/custom templates we just close the library. A future
            // iteration could prepend the template markdown to the input.
            set({ templateLibraryOpen: false });
            break;
        }
      },
      togglePin: (sessionId) => {
        const cur = get().pinnedSessionIds;
        const next = cur.includes(sessionId)
          ? cur.filter((id) => id !== sessionId)
          : [sessionId, ...cur];
        set({ pinnedSessionIds: next });
        savePinned(next);
      },
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
        _lastClientRef.current = client; // BATCH-2 Task 5.6
        set({ isLoadingSessions: true, sessionError: null });
        // Always restore activeSessionId from localStorage first — even if the
        // network call fails, the user should keep their active session so
        // they can retry or send a new message without losing context.
        let storedActiveId: string | null = null;
        try {
          storedActiveId = localStorage.getItem(ACTIVE_SESSION_KEY);
        } catch {
          /* ignore */
        }
        try {
          const { sessions } = await client.listChatSessions();
          // Restore active session if it still exists.
          let activeId = get().activeSessionId || storedActiveId;
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
          // Network failure — still restore the active session id from
          // localStorage so the user isn't dropped to an empty chat. They
          // can retry; the next successful loadSessions will reconcile.
          set({
            sessionError: e instanceof Error ? e.message : "Failed to load sessions",
            isLoadingSessions: false,
            activeSessionId: storedActiveId,
            sessions: [],
          });
        }
      },

      // -- Create session --
      createSession: async (client, model) => {
        _lastClientRef.current = client; // BATCH-2 Task 5.6
        try {
          // BATCH-3 Task 6 — generate a random 4-char hex name instead of
          // "New Chat" so each new chat is uniquely identifiable in the
          // sidebar before the backend auto-derives a title from the first
          // message. The backend may overwrite this title later via the
          // "title" event (re-derived from context) — that's expected.
          const randomHex = Math.random().toString(16).slice(2, 6).padEnd(4, "0");
          const randomTitle = `Chat ${randomHex}`;
          const cs = await client.createChatSession(
            randomTitle,
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
            sessionCost: 0,
            lastUsage: null,
            currentModel: model || null,
            resolvedModel: null,
            resolvedProvider: null,
            requestedModel: null,
            panelInvocations: [],
            files: [],
            queue: [],
            suggestions: [],
            _agentSessionId: null,
            _streamController: null,
            _lastEventSeq: 0,
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
        _lastClientRef.current = client; // BATCH-2 Task 5.6
        // Abort any in-flight stream first.
        const ctrl = get()._streamController;
        if (ctrl) {
          ctrl.abort();
        }
        // BATCH-3 Task 2 — IMPORTANT: reset ALL per-chat tool state to
        // defaults BEFORE restoring from the session's saved metadata.
        // Without this reset, switching from chat A (webSearch=true) to
        // chat B (webSearch=false) leaves webSearch=true because the
        // initial set() below only clears messages/status — not the tool
        // toggles. The previous code only restored metadata AFTER
        // getChatEvents, leaving a window where the old chat's tools were
        // still active. Now we reset to defaults here, then overwrite
        // with the session's saved values once we have them.
        set({
          isLoadingMessages: true,
          activeSessionId: sessionId,
          messages: [],
          error: null,
          status: "idle",
          isBusy: false,
          isStreaming: false,
          cost: null,
          sessionCost: 0,
          lastUsage: null,
          files: [],
          currentModel: null,
          resolvedModel: null,
          resolvedProvider: null,
          requestedModel: null,
          panelInvocations: [],
          queue: [],
          suggestions: [],
          _agentSessionId: null,
          _streamController: null,
          _lastEventSeq: 0,
          // Reset tool toggles to defaults — restored from cs below.
          effort: "med",
          webSearch: false,
          deepResearch: false,
          mode: "auto",
          webTemplate: "",
          deepTemplate: "",
          judge: { count: 3, template: "critique" },
        });
        try {
          const evData = await client.getChatEvents(sessionId);
          const messages = eventsToMessages(evData.events);
          // Set the cursor to the end of the loaded events so the next turn
          // only requests new events (no full transcript replay).
          const maxSeq = evData.events.reduce((mx, ev) => Math.max(mx, (ev.i ?? 0) + 1), 0);
          set({ messages, isLoadingMessages: false, _lastEventSeq: maxSeq });
          try {
            localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
          } catch {
            /* ignore */
          }
          // BATCH-3 Task 2 — restore per-chat metadata. The backend stores
          // effort/webSearch/deepResearch/mode/templates/judge/model per
          // session. When switching chats, restore those settings so each
          // chat "remembers" its own configuration. Fall back to defaults
          // for old sessions that don't have the metadata yet (the reset
          // above already set defaults, so this only fires if cs has data).
          const cs = get().sessions.find((s) => s.id === sessionId);
          if (cs?.workspace_id && cs.workspace_id !== get().workspaceId) {
            get().setWorkspaceId(cs.workspace_id);
          }
          if (cs) {
            // Only overwrite the defaults if the session has actual data.
            const restored: Partial<ChatState> = {};
            if (cs.effort) restored.effort = cs.effort as string;
            if (typeof cs.web_search === "boolean") restored.webSearch = cs.web_search;
            if (typeof cs.deep_research === "boolean") restored.deepResearch = cs.deep_research;
            if (cs.mode) restored.mode = cs.mode as ChatState["mode"];
            if (cs.web_template) restored.webTemplate = cs.web_template;
            if (cs.deep_template) restored.deepTemplate = cs.deep_template;
            if (typeof cs.judge_count === "number" || cs.judge_template) {
              restored.judge = {
                count: cs.judge_count ?? 3,
                template: cs.judge_template || "critique",
              };
            }
            if (Object.keys(restored).length > 0) {
              set(restored);
            }
            // BATCH-3 Task 2 — restore the saved model. We update the
            // model-store so the chat input's model badge + effectiveModelId
            // reflect the session's model. The backend will resolve the
            // provider on the next send.
            if (cs.model) {
              try {
                const ms = useModelStore.getState();
                // Only update if the model differs (avoid loops).
                if (ms.selectedModelId !== cs.model && ms.selectedSlotId !== cs.model) {
                  // Try to find the model in the providers list to get the
                  // slot id + provider name. Fall back to setting just the
                  // logical id.
                  let providerName = ms.selectedProviderName;
                  let slotId = cs.model;
                  for (const p of ms.providers) {
                    const m = p.models.find(
                      (mm) => mm.id === cs.model || mm.slotId === cs.model,
                    );
                    if (m) {
                      providerName = p.name;
                      slotId = m.slotId || m.id;
                      break;
                    }
                  }
                  useModelStore.setState({
                    selectedModelId: cs.model,
                    selectedProviderName: providerName,
                    selectedSlotId: slotId,
                    focusedMode: true,
                  });
                  try {
                    localStorage.setItem("doomalaysocreate.model-store.selectedModelId", JSON.stringify(cs.model));
                    localStorage.setItem("doomalaysocreate.model-store.selectedSlotId", JSON.stringify(slotId));
                    if (providerName) {
                      localStorage.setItem("doomalaysocreate.model-store.selectedProviderName", JSON.stringify(providerName));
                    }
                  } catch {
                    /* ignore */
                  }
                }
              } catch {
                /* model-store not yet loaded — non-fatal, will pick up on next render */
              }
            }
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
        // BATCH-3 Task 6 — track that the user manually renamed this
        // session. The "title" event handler in _runTurn checks this flag
        // and SKIPS the auto-rename so the user's choice is preserved.
        // The flag is stored in localStorage (not on the ChatSession —
        // the backend doesn't need to know) keyed by session id.
        try {
          const raw = localStorage.getItem("doomalaysocreate.chat.manually_renamed");
          const map: Record<string, boolean> = raw ? JSON.parse(raw) : {};
          map[sessionId] = true;
          localStorage.setItem("doomalaysocreate.chat.manually_renamed", JSON.stringify(map));
        } catch {
          /* ignore */
        }
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

      // -- Send message (with queue + stop mode support) --
      sendMessage: async (client, message, model) => {
        _lastClientRef.current = client; // BATCH-2 Task 5.6
        const text = message.trim();
        if (!text) return;
        const state = get();

        // If busy:
        //  - busyMode "queue" (default): enqueue so it fires after the current turn.
        //  - busyMode "stop": interrupt the running turn, then send this one immediately.
        if (state.isBusy) {
          if (state.busyMode === "stop") {
            await get().stopGeneration(client);
            // stopGeneration is async — give the backend a beat to settle the
            // interrupt before we kick off a new turn.
            await new Promise((r) => setTimeout(r, 50));
            await _runTurn(client, text, model, set, get);
            return;
          }
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

      // -- Judge panel (chat-integrated) ---------------------------------
      // Fires a POST /api/chat/judge with the current input (or the supplied
      // override) and the user-configured count + template. The merged result
      // is inserted as an assistant message tagged with [Judge Panel].
      runJudge: async (client, input, model) => {
        const text = (input ?? get().inputText).trim();
        if (!text) return;
        const cfg = get().judge;
        // Insert a placeholder streaming assistant bubble so the user sees
        // immediate feedback that the judge panel is running.
        const placeholderId = genMsgId();
        const placeholder: ChatMessage = {
          id: placeholderId,
          role: "assistant",
          content: "",
          isStreaming: true,
          stage: "judge:running",
          timestamp: Date.now(),
          seq: -1,
          pending: true,
        };
        set((s) => ({
          messages: [...s.messages, placeholder],
          isBusy: true,
          status: "running",
          inputText: input ? get().inputText : "",
          error: null,
        }));
        try {
          const res = await client.runJudge({
            input: text,
            template: cfg.template,
            count: cfg.count,
            model: model || undefined,
          });
          // Replace the placeholder with the merged output. Keep the
          // assistant role so the bubble renders normally with Markdown.
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === placeholderId
                ? {
                    ...m,
                    content: res.merged || "(no output)",
                    isStreaming: false,
                    stage: undefined,
                    pending: false,
                    costUsd: null,
                  }
                : m,
            ),
            isBusy: false,
            status: "idle",
          }));
        } catch (e) {
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === placeholderId
                ? {
                    ...m,
                    content: `Judge panel failed: ${e instanceof Error ? e.message : String(e)}`,
                    isStreaming: false,
                    isError: true,
                    stage: undefined,
                    pending: false,
                  }
                : m,
            ),
            isBusy: false,
            status: "idle",
            error: e instanceof Error ? e.message : "Judge panel failed",
          }));
        }
      },

      // -- Monitor SSE (job queue + suggestions) -------------------------
      connectMonitor: (client) => {
        // Don't double-open.
        if (get()._monitorController) return;
        const ctrl = client.monitorStream(
          get().workspaceId,
          (ev: MonitorEvent) => {
            switch (ev.type) {
              case "job_started": {
                set((s) => ({
                  jobs: [...s.jobs.filter((j) => j.id !== ev.job.id), ev.job],
                }));
                break;
              }
              case "job_progress": {
                set((s) => ({
                  jobs: s.jobs.map((j) =>
                    j.id === ev.job_id
                      ? {
                          ...j,
                          stage: ev.stage ?? j.stage,
                          progress: ev.progress ?? j.progress,
                          cost_usd: ev.cost_usd ?? j.cost_usd,
                          status: "running",
                        }
                      : j,
                  ),
                }));
                break;
              }
              case "job_delta": {
                // We don't surface delta text in the QueueMonitor (the chat
                // bubble already streams it). Just bump the job's last-touched
                // timestamp so the active list re-orders.
                set((s) => ({
                  jobs: s.jobs.map((j) =>
                    j.id === ev.job_id ? { ...j, status: "running" } : j,
                  ),
                }));
                break;
              }
              case "job_complete": {
                set((s) => ({
                  jobs: s.jobs.map((j) =>
                    j.id === ev.job_id
                      ? {
                          ...j,
                          status: ev.ok ? "complete" : "error",
                          stage: undefined,
                          progress: 1,
                          ended_at: Date.now(),
                          cost_usd: ev.total_cost_usd ?? j.cost_usd,
                        }
                      : j,
                  ),
                  // Append any new suggestions (deduped by id, sorted by importance×creativity desc).
                  suggestions: ev.suggestions
                    ? mergeSuggestions(s.suggestions, ev.suggestions)
                    : s.suggestions,
                }));
                break;
              }
              case "job_error": {
                set((s) => ({
                  jobs: s.jobs.map((j) =>
                    j.id === ev.job_id
                      ? {
                          ...j,
                          status: "error",
                          stage: ev.error,
                          ended_at: Date.now(),
                        }
                      : j,
                  ),
                }));
                break;
              }
            }
          },
          () => {
            // On error: clear the controller so connectMonitor can retry
            // next time. We don't surface the error to the user — the
            // chat itself still works without the monitor.
            set({ _monitorController: null });
          },
        );
        set({ _monitorController: ctrl });
      },

      disconnectMonitor: () => {
        const ctrl = get()._monitorController;
        if (ctrl) {
          try { ctrl.abort(); } catch { /* ignore */ }
        }
        set({ _monitorController: null });
      },

      cancelJob: async (client, jobId) => {
        // Optimistic: mark cancelled locally so the UI reflects it instantly.
        set((s) => ({
          jobs: s.jobs.map((j) =>
            j.id === jobId
              ? { ...j, status: "error", stage: "cancelled", ended_at: Date.now() }
              : j,
          ),
        }));
        try {
          await client.cancelJob(jobId);
        } catch {
          /* keep optimistic state */
        }
      },

      // Drop a suggestion into the input box so the user can edit/send it.
      applySuggestion: (id) => {
        const sug = get().suggestions.find((s) => s.id === id);
        if (!sug) return;
        set({ inputText: sug.text });
      },

      // BATCH-2 Task 5.6 — debounced per-chat metadata persist. Pushes
      // the current effort / webSearch / deepResearch / mode / templates /
      // judge config + the current model to the backend's
      // /api/chat/sessions/:id/update so the session "remembers" its
      // settings. Debounced 800ms so rapid toggles don't spam the backend.
      // BATCH-3 Task 2 — also persists the current model (read from
      // useModelStore) so switching chats restores the per-chat model.
      _persistChatMeta: () => {
        // Clear any in-flight timer.
        if (get()._persistChatMetaTimer) {
          clearTimeout(get()._persistChatMetaTimer!);
        }
        const timer = setTimeout(() => {
          const sid = get().activeSessionId;
          if (!sid) return;
          const s = get();
          // BATCH-3 Task 2 — read the current model from useModelStore.
          // chatStore → model-store is a one-way dep (model-store doesn't
          // import chatStore), so a top-level import is safe.
          let currentModel: string | undefined;
          try {
            const state = useModelStore.getState();
            currentModel = state.selectedSlotId || state.selectedModelId || undefined;
          } catch {
            /* ignore — model store not initialized yet */
          }
          // Fire-and-forget — failures are non-fatal (the backend may not
          // have rolled out the metadata fields yet; the call still
          // succeeds, the backend just ignores unknown fields).
          try {
            if (_lastClientRef.current) {
              const updates: Record<string, unknown> = {
                effort: s.effort,
                web_search: s.webSearch,
                deep_research: s.deepResearch,
                mode: s.mode,
                web_template: s.webTemplate || undefined,
                deep_template: s.deepTemplate || undefined,
                judge_count: s.judge.count,
                judge_template: s.judge.template,
              };
              if (currentModel) updates.model = currentModel;
              _lastClientRef.current.updateChatSession(sid, updates).catch(() => { /* non-fatal */ });
              // BATCH-3 Task 6 — also optimistically update the local
              // sessions list so the sidebar shows the new model + title
              // without waiting for the next listChatSessions refresh.
              set((st) => ({
                sessions: st.sessions.map((ses) =>
                  ses.id === sid
                    ? {
                        ...ses,
                        effort: s.effort,
                        web_search: s.webSearch,
                        deep_research: s.deepResearch,
                        mode: s.mode,
                        web_template: s.webTemplate || null,
                        deep_template: s.deepTemplate || null,
                        judge_count: s.judge.count,
                        judge_template: s.judge.template,
                        model: currentModel ?? ses.model,
                      }
                    : ses,
                ),
              }));
            }
          } catch {
            /* non-fatal */
          }
          set({ _persistChatMetaTimer: null });
        }, 800);
        set({ _persistChatMetaTimer: timer });
      },
      _persistChatMetaTimer: null,
    }),
    {
      name: "doomalaysocreate.chat.store",
      partialize: (state) => ({
        effort: state.effort,
        webSearch: state.webSearch,
        deepResearch: state.deepResearch,
        mode: state.mode,
        sidebarOpen: state.sidebarOpen,
        busyMode: state.busyMode,
        webTemplate: state.webTemplate,
        deepTemplate: state.deepTemplate,
        judge: state.judge,
      }),
    },
  ),
);

/** Merge new suggestions into existing, dedupe by id, and sort by
 *  (importance desc, creativity desc). Cap at 12 entries so the panel
 *  doesn't grow unbounded. */
function mergeSuggestions(
  existing: MonitorSuggestion[],
  incoming: MonitorSuggestion[],
): MonitorSuggestion[] {
  const map = new Map<string, MonitorSuggestion>();
  for (const s of existing) map.set(s.id, s);
  for (const s of incoming) map.set(s.id, s);
  return Array.from(map.values())
    .sort((a, b) => b.importance - a.importance || b.creativity - a.creativity)
    .slice(0, 12);
}

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
    requestedModel: model || null,
  }));

  // Buffer events for batch persistence at turn end.
  let bufferedEvents: AgentEvent[] = [];
  // The SSE cursor — bumped as events arrive. We read the initial value from
  // the store, but RESET it to 0 below when we detect a NEW agent session
  // (see "new session" guard after client.send).
  let since = get()._lastEventSeq || 0;
  // Capture the PREVIOUS agent session id so we can detect a session change
  // (e.g. when the user switches models mid-conversation and the backend
  // spins up a fresh agent session). On change: abort the old stream, reset
  // the cursor to 0 so we replay every event from the new session.
  const prevAgentSid = get()._agentSessionId;

  // Bug 2 fix: abort any lingering stream controller from the previous turn
  // before we start a new one. The `finally` block of the previous turn also
  // aborts, but a slow/racing stream could still be emitting events when we
  // kick off this turn — killing it here guarantees a clean slate.
  const existingController = get()._streamController;
  if (existingController) {
    try { existingController.abort(); } catch { /* already aborted */ }
    set({ _streamController: null });
  }

  let agentSid: string | null = null;
  let controller: AbortController | null = null;
  // Keep a separate ref the finally-block can read (TS narrows the let to
  // never inside the Promise closure otherwise).
  const controllerRef: { current: AbortController | null } = { current: null };

  try {
    const start: AgentStart = await client.send(
      text,
      undefined, // no session_id — the backend reuses by chat_session_id
      model || undefined,
      get().workspaceId || undefined,
      sessionId,
      {
        effort: get().effort,
        web_search: get().webSearch,
        deep_research: get().deepResearch,
        mode: get().mode,
        web_template: get().webTemplate || undefined,
        deep_template: get().deepTemplate || undefined,
        judge: { count: get().judge.count, template: get().judge.template },
      },
    );
    agentSid = start.session_id;

    // MODEL VERIFICATION: store what the backend actually resolved + is running.
    // The backend returns: model (requested), resolved_model, resolved_provider.
    // If resolved_model differs from what we requested, the UI shows a warning.
    const startAny = start as AgentStart & { resolved_model?: string; resolved_provider?: string; requested_model?: string };
    set({
      _agentSessionId: agentSid,
      currentModel: start.model,
      requestedModel: startAny.requested_model || model || null,
      resolvedModel: startAny.resolved_model || null,
      resolvedProvider: startAny.resolved_provider || null,
    });

    // Bug 1 + Bug 2 fix: if the backend created a NEW agent session (i.e.
    // start.session_id differs from the previous _agentSessionId), reset the
    // SSE cursor to 0 so we replay every event from the start. This catches
    // both the first-message case (prevAgentSid was null) and the model-switch
    // case (prevAgentSid was a different session). Without this, the backend's
    // early-emitted events (which fire before our SSE stream connects) would
    // be lost — manifesting as "first message doesn't respond".
    if (agentSid !== prevAgentSid) {
      since = 0;
      set({ _lastEventSeq: 0 });
    }

    // Bug 1 fix: small delay to give the backend a beat to buffer the early
    // events before we open the SSE stream. Without this, on a brand-new
    // session the backend's "user echo + first assistant_delta" may already
    // be in flight by the time our stream connects, and the queue-based SSE
    // could miss them. 300ms is the same backoff the polling fallback uses.
    await new Promise((r) => setTimeout(r, 300));

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
        // Bug 1 dedupe guard: if the SSE stream replays an event we've
        // already seen (ev.i < since), drop it — appendEvent() also dedupes
        // by seq but we skip the work entirely here for clarity.
        if (typeof ev.i === "number" && ev.i < since - 1 && ev.type !== "assistant_delta" && ev.type !== "thinking_delta" && ev.type !== "thinking") {
          return;
        }
        bufferedEvents.push(ev);
        since = Math.max(since, (ev.i ?? 0) + 1);
        set((s) => ({ messages: appendEvent(s.messages, ev), _lastEventSeq: since }));

        // Bug 3 fix: handle the "title" event — the backend emits this once
        // it has auto-generated a chat title from the first user message.
        // We patch the matching session in the sessions list so the sidebar
        // updates without a full refresh.
        // BATCH-3 Task 6 — SKIP the auto-rename if the user manually renamed
        // the session. The manually_renamed flag is tracked in localStorage
        // by renameSession(). Once set, the backend's auto-derived title is
        // ignored so the user's choice wins.
        if (ev.type === "title") {
          const t = ev as { type: "title"; title: string; session_id: string };
          const targetId = t.session_id || sessionId;
          let isManuallyRenamed = false;
          try {
            const raw = localStorage.getItem("doomalaysocreate.chat.manually_renamed");
            const map: Record<string, boolean> = raw ? JSON.parse(raw) : {};
            isManuallyRenamed = !!map[targetId];
          } catch {
            /* ignore */
          }
          if (!isManuallyRenamed) {
            set((s) => ({
              sessions: s.sessions.map((ses) =>
                ses.id === targetId ? { ...ses, title: t.title } : ses,
              ),
            }));
          }
        }

        // Update status from status events.
        if (ev.type === "status") {
          const st = ev as {
            state: AgentStatus;
            cost_usd?: number | null;
            usage?: { input_tokens: number; output_tokens: number; total_tokens: number; reasoning_tokens?: number };
          };
          set({ status: st.state });
          if (typeof st.cost_usd === "number") {
            // cost_usd is the SESSION TOTAL so far (the backend accumulates),
            // not per-turn. We store it on `cost` for the header gauge and on
            // `sessionCost` for the running PriceGauge total.
            set({ cost: st.cost_usd, sessionCost: st.cost_usd });
          }
          if (st.usage) {
            set({ lastUsage: st.usage });
          }
          if (st.state === "idle" || st.state === "error") {
            set((s) => ({ messages: finalizeStreaming(s.messages) }));
            // Give a brief moment for any trailing events, then finish.
            setTimeout(finish, 200);
          }
        }
      };

      controller = client.stream(
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
      controllerRef.current = controller;
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
  } finally {
    // ALWAYS abort the SSE stream when the turn ends — prevents trailing
    // events from the previous turn leaking into the next turn.
    const ctrl = controllerRef.current;
    if (ctrl) {
      try { ctrl.abort(); } catch { /* already aborted */ }
    }
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
