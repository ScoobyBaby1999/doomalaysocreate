// Typed client for the agent orchestrator (the "agent" tab). Mirrors the
// backend's polling/SSE contract: POST /api/agent starts/continues a session,
// GET /api/agent/<sid>?since=<n> returns an append-only transcript delta,
// GET /api/agent/<sid>/stream pushes the same events as SSE for real-time UI.
//
// Chat session persistence (/api/chat/sessions/*) is also exposed here so the
// UI store can manage multi-conversation history.

import { deriveToken } from "./token";
import type { Settings, PanelSnapshot } from "./panel";
import { ApiError } from "./panel";

export type AgentEvent =
  | { i: number; ts: number; type: "user"; text: string }
  | { i: number; ts: number; type: "assistant"; text: string }
  | { i: number; ts: number; type: "assistant_delta"; text: string }
  | { i: number; ts: number; type: "thinking"; text: string }
  | { i: number; ts: number; type: "thinking_delta"; text: string }
  | { i: number; ts: number; type: "tool_use"; name: string; summary: string }
  | { i: number; ts: number; type: "tool_result"; text: string; is_error: boolean }
  | {
      i: number;
      ts: number;
      type: "status";
      state: "starting" | "idle" | "running" | "error";
      detail?: string;
      cost_usd?: number | null;
      usage?: {
        input_tokens: number;
        output_tokens: number;
        total_tokens: number;
        reasoning_tokens?: number;
      };
      /** Stage hint — e.g. "searching", "reading:3", "synthesizing".
       *  The frontend maps these to human labels in the bubble status row. */
      stage?: string;
    }
  | {
      i: number;
      ts: number;
      type: "sources";
      sources: { url: string; name?: string; snippet?: string }[];
    }
  | {
      i: number;
      ts: number;
      type: "panel";
      status: "starting" | "running" | "done";
      invoke_id?: string;
      task_name?: string;
      prompt?: string;
      panel?: string[];
      snapshot?: PanelSnapshot;
      error?: string;
    }
  | {
      i: number;
      ts: number;
      type: "steering";
      guidance: string;
      detail: string;
    };

/** Monitor SSE events — emitted by the backend's /api/monitor stream.
 *  These drive the QueueMonitor panel (active jobs, recent jobs, suggestions). */
export type MonitorEvent =
  | { type: "job_started"; job: MonitorJob }
  | {
      type: "job_progress";
      job_id: string;
      stage?: string;
      progress?: number;
      detail?: string;
      cost_usd?: number;
    }
  | { type: "job_delta"; job_id: string; text: string }
  | {
      type: "job_complete";
      job_id: string;
      ok: boolean;
      merged?: string;
      suggestions?: MonitorSuggestion[];
      total_cost_usd?: number;
    }
  | { type: "job_error"; job_id: string; error: string };

export interface MonitorJob {
  id: string;
  space_id?: string;
  kind: "chat" | "panel" | "template";
  status: "queued" | "running" | "complete" | "error";
  prompt: string;
  stage?: string;
  progress?: number;
  started_at?: number;
  ended_at?: number;
  cost_usd?: number;
  model?: string;
}

export interface MonitorSuggestion {
  id: string;
  text: string;
  /** Higher = more important. Used to sort. */
  importance: number;
  /** Higher = more creative. Used to sort + label. */
  creativity: number;
  kind?: "followup" | "action" | "question" | "template";
}

export type AgentStatus = "starting" | "idle" | "running" | "error";

export interface AgentSnapshot {
  session_id: string;
  tier: "claude" | "open" | "mock";
  model: string | null;
  resolved_model?: string | null;
  resolved_provider?: string | null;
  resolved_api_base?: string | null;
  status: AgentStatus;
  events: AgentEvent[];
  next: number;
}

export interface AgentStart {
  session_id: string;
  tier: "claude" | "open" | "mock";
  model: string | null;
  status: AgentStatus;
  workspace_id?: string;
  chat_session_id?: string;
  /** The model the user requested (before backend resolution). */
  requested_model?: string;
  /** The canonical litellm model string the backend actually runs. */
  resolved_model?: string;
  /** The provider serving this agent turn (e.g. "nvidia", "privatemodeai"). */
  resolved_provider?: string;
}

export interface AgentFile {
  path: string;
  size: number;
  mtime: number;
}

export interface ChatSession {
  id: string;
  title: string;
  model: string | null;
  workspace_id: string | null;
  user_id?: string | null;
  created_at: string;
  updated_at: string;
  /** Optional message count (populated when the backend returns `_count`). */
  message_count?: number;
}

export class AgentClient {
  private settings: Settings;

  constructor(settings: Settings) {
    this.settings = settings;
  }

  private async bearer(windowsBack = 0): Promise<string> {
    if (this.settings.rotationSecret) {
      return deriveToken(this.settings.rotationSecret, windowsBack);
    }
    return this.settings.token;
  }

  private fetchWith(token: string, path: string, init?: RequestInit): Promise<Response> {
    const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) };
    if (init?.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (token) headers["Authorization"] = `Bearer ${token}`;
    return fetch(this.settings.baseUrl + path, { ...init, headers });
  }

  /** Authenticated fetch with a single 401 retry on the previous token window. */
  private async raw(path: string, init?: RequestInit): Promise<Response> {
    let r = await this.fetchWith(await this.bearer(), path, init);
    if (r.status === 401 && this.settings.rotationSecret) {
      r = await this.fetchWith(await this.bearer(1), path, init);
    }
    return r;
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    const r = await this.raw(path, init);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        if (j?.error) msg = j.error;
      } catch {
        /* non-json body */
      }
      throw new ApiError(r.status, msg);
    }
    return (await r.json()) as T;
  }

  /** Optional X-JWT header for GitHub-identity routes (workspace ownership). */
  private jwtHeaders(): Record<string, string> {
    return this.settings.githubSessionId ? { "X-JWT": this.settings.githubSessionId } : {};
  }

  /**
   * Start a new agent session, or continue an existing one if sessionId is given.
   * `model` selects which model/tier drives a NEW session.
   * `workspaceId` links the agent to a user workspace sandbox.
   * `chatSessionId` links to a persistent chat session for history.
   * `opts` carries effort/web_search/deep_research toggles.
   */
  send(
    message: string,
    sessionId?: string,
    model?: string,
    workspaceId?: string,
    chatSessionId?: string,
    opts?: {
      effort?: string;
      web_search?: boolean;
      deep_research?: boolean;
      mode?: string;
      /** Web-search template override ("breadth" | "deepdive" | "compare" | "factcheck"). */
      web_template?: string;
      /** Deep-research template override ("react" | "extended"). */
      deep_template?: string;
      /** Judge-panel config: { count, template }. */
      judge?: { count: number; template: string };
    },
  ) {
    const body: Record<string, unknown> = { message };
    if (sessionId) body.session_id = sessionId;
    if (model) body.model = model;
    if (workspaceId) body.workspace_id = workspaceId;
    if (chatSessionId) body.chat_session_id = chatSessionId;
    if (opts?.effort) body.effort = opts.effort;
    if (opts?.web_search) body.web_search = true;
    if (opts?.deep_research) body.deep_research = true;
    if (opts?.mode) body.mode = opts.mode;
    if (opts?.web_template) body.web_template = opts.web_template;
    if (opts?.deep_template) body.deep_template = opts.deep_template;
    if (opts?.judge) body.judge = opts.judge;
    return this.req<AgentStart>("/api/agent", {
      method: "POST",
      body: JSON.stringify(body),
      headers: this.jwtHeaders(),
    });
  }

  /** Stop the in-flight turn for a session. */
  interrupt(sessionId: string) {
    return this.req<{ interrupted: boolean; status: AgentStatus }>(
      `/api/agent/${sessionId}/interrupt`,
      { method: "POST" },
    );
  }

  /** Poll the transcript; `since` is the cursor returned as `next` last time. */
  poll(sessionId: string, since: number) {
    return this.req<AgentSnapshot>(`/api/agent/${sessionId}?since=${since}`);
  }

  /**
   * SSE stream for live agent events. Returns an AbortController. The
   * `onEvent` callback fires once per parsed event; `onError` fires on
   * network errors (abort excluded). `onClose` fires when the stream ends
   * (server closed the connection, including when the agent finishes).
   */
  stream(
    sessionId: string,
    since: number,
    onEvent: (ev: AgentEvent) => void,
    onError?: (err: Error) => void,
    onClose?: () => void,
  ): AbortController {
    const ac = new AbortController();
    const baseUrl = this.settings.baseUrl;
    this.bearer(0).then((token) => {
      const url = `${baseUrl}/api/agent/${sessionId}/stream?since=${since}`;
      fetch(url, {
        signal: ac.signal,
        headers: { Authorization: `Bearer ${token}`, ...this.jwtHeaders() },
      })
        .then(async (r) => {
          if (!r.ok) {
            onError?.(new Error(`SSE stream returned HTTP ${r.status}`));
            return;
          }
          const reader = r.body?.getReader();
          if (!reader) {
            onError?.(new Error("SSE: no response body"));
            return;
          }
          const decoder = new TextDecoder();
          let buf = "";
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buf += decoder.decode(value, { stream: true });
              // SSE events are separated by a blank line (\n\n). We split on
              // \n, keep the last partial line as buf, and parse complete
              // `data: ...` lines.
              const lines = buf.split("\n");
              buf = lines.pop() || "";
              for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed.startsWith("data: ")) {
                  try {
                    const ev = JSON.parse(trimmed.slice(6)) as AgentEvent;
                    onEvent(ev);
                  } catch {
                    /* skip malformed JSON */
                  }
                }
              }
            }
            onClose?.();
          } catch (e) {
            if ((e as Error)?.name !== "AbortError") {
              onError?.(e instanceof Error ? e : new Error(String(e)));
            }
          }
        })
        .catch((e) => {
          if ((e as Error)?.name !== "AbortError") {
            onError?.(e instanceof Error ? e : new Error(String(e)));
          }
        });
    });
    return ac;
  }

  files(sessionId: string) {
    return this.req<{ session_id: string; files: AgentFile[] }>(`/api/agent/${sessionId}/files`);
  }

  // -- memory layer (.pied sanity log) ------------------------------------

  /** Get the memory state for a workspace (goal, plan, tasks, log, blackboard). */
  getMemory(workspaceId: string) {
    return this.req<{
      state: { goal?: string; plan?: string; status?: string; pending_tasks?: any[]; completed_tasks?: any[] };
      recent_log: { ts: number; agent: string; kind: string; data: any }[];
      blackboard: { ts: number; agent: string; key: string; value: string }[];
    }>(`/api/memory?workspace_id=${workspaceId}`);
  }

  /** Get live model benchmarks from OpenRouter (pricing, context, capabilities). */
  getBenchmarks() {
    return this.req<{
      models: {
        id: string; name: string; context_length: number;
        prompt_price: string; completion_price: string; cost_per_1m: number;
        is_free: boolean; description: string; modality: string;
        capabilities: string[]; input_modalities: string[];
        output_modalities: string[]; tokenizer: string;
        knowledge_cutoff: string; supported_params: string[];
      }[];
      count: number;
      free_count: number;
      vision_count: number;
      coding_count: number;
      agentic_count: number;
    }>("/api/benchmarks");
  }

  // -- chat session persistence -------------------------------------------

  listChatSessions() {
    return this.req<{ sessions: ChatSession[] }>("/api/chat/sessions");
  }

  createChatSession(title?: string, model?: string, workspaceId?: string) {
    const body: Record<string, unknown> = { title: title || "New Chat" };
    if (model) body.model = model;
    if (workspaceId) body.workspace_id = workspaceId;
    return this.req<ChatSession>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify(body),
      headers: this.jwtHeaders(),
    });
  }

  deleteChatSession(id: string) {
    return this.req<{ deleted: string }>(`/api/chat/sessions/${id}`, {
      method: "DELETE",
    });
  }

  updateChatSession(id: string, fields: { title?: string; model?: string; workspace_id?: string }) {
    return this.req<ChatSession>(`/api/chat/sessions/${id}/update`, {
      method: "POST",
      body: JSON.stringify(fields),
      headers: this.jwtHeaders(),
    });
  }

  getChatEvents(id: string) {
    return this.req<{ session_id: string; events: AgentEvent[] }>(
      `/api/chat/sessions/${id}/events`,
    );
  }

  /**
   * Persist events to a chat session (batch append for delta-sync). The
   * backend dedupes by seq, so re-persisting the same events is safe.
   * Failures are non-fatal — events will be re-fetched from DB on next load.
   */
  async persistEvents(sessionId: string, events: AgentEvent[]): Promise<void> {
    if (!events.length) return;
    try {
      await this.raw(`/api/chat/sessions/${sessionId}/persist`, {
        method: "POST",
        body: JSON.stringify({ events }),
        headers: this.jwtHeaders(),
      });
    } catch {
      /* non-fatal */
    }
  }

  /** Download an artifact. */
  async download(sessionId: string, path: string): Promise<void> {
    const r = await this.raw(`/api/agent/${sessionId}/file?path=${encodeURIComponent(path)}`);
    if (!r.ok) throw new ApiError(r.status, `download failed (HTTP ${r.status})`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop() || "download";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // -- judge panel (chat-integrated) -------------------------------------

  /** Run the judge panel directly from chat. Returns the merged result + per-judge info. */
  runJudge(body: { input: string; template?: string; count?: number; model?: string }) {
    return this.req<{
      merged: string;
      judges: { model: string; status: string; output?: string; error?: string }[];
      ok: boolean;
      total: number;
    }>("/api/chat/judge", {
      method: "POST",
      body: JSON.stringify(body),
      headers: this.jwtHeaders(),
    });
  }

  // -- async job queue + monitor SSE ------------------------------------

  /** Enqueue a job (chat turn, panel run, template run). Returns the job id. */
  enqueueJob(body: {
    kind: "chat" | "panel" | "template";
    prompt: string;
    model?: string;
    spaceId?: string;
    template?: string;
    effort?: string;
    web_search?: boolean;
    deep_research?: boolean;
  }) {
    return this.req<{ job_id: string; status: string }>("/api/chat/queue", {
      method: "POST",
      body: JSON.stringify(body),
      headers: this.jwtHeaders(),
    });
  }

  /** List queued/running/recent jobs for a space. */
  listJobs(spaceId?: string) {
    const qs = spaceId ? `?spaceId=${encodeURIComponent(spaceId)}` : "";
    return this.req<{ jobs: MonitorJob[] }>(`/api/chat/queue${qs}`);
  }

  /** Cancel a queued/running job. */
  cancelJob(jobId: string) {
    return this.req<{ ok: boolean }>(`/api/chat/queue`, {
      method: "PATCH",
      body: JSON.stringify({ job_id: jobId, action: "cancel" }),
      headers: this.jwtHeaders(),
    });
  }

  /** Get live pricing (per-model $/1M tokens, free flags). */
  getPricing() {
    return this.req<{
      models: {
        id: string;
        prompt_price_per_1m?: number;
        completion_price_per_1m?: number;
        is_free?: boolean;
      }[];
    }>("/api/pricing");
  }

  /**
   * Open the monitor SSE stream. Returns an AbortController. The onEvent
   * callback fires for every job_started/job_progress/job_delta/job_complete/
   * job_error event. We use fetch + ReadableStream (not native EventSource)
   * so we can pass the Authorization header (EventSource can't).
   */
  monitorStream(
    spaceId: string | null,
    onEvent: (ev: MonitorEvent) => void,
    onError?: (err: Error) => void,
  ): AbortController {
    const ac = new AbortController();
    const baseUrl = this.settings.baseUrl;
    this.bearer(0).then((token) => {
      const qs = spaceId ? `?spaceId=${encodeURIComponent(spaceId)}` : "";
      const url = `${baseUrl}/api/monitor${qs}`;
      fetch(url, {
        signal: ac.signal,
        headers: { Authorization: `Bearer ${token}`, ...this.jwtHeaders() },
      })
        .then(async (r) => {
          if (!r.ok) {
            onError?.(new Error(`monitor stream HTTP ${r.status}`));
            return;
          }
          const reader = r.body?.getReader();
          if (!reader) {
            onError?.(new Error("monitor: no response body"));
            return;
          }
          const decoder = new TextDecoder();
          let buf = "";
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buf += decoder.decode(value, { stream: true });
              const lines = buf.split("\n");
              buf = lines.pop() || "";
              for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed.startsWith("data: ")) {
                  try {
                    const ev = JSON.parse(trimmed.slice(6)) as MonitorEvent;
                    onEvent(ev);
                  } catch {
                    /* skip malformed */
                  }
                }
              }
            }
          } catch (e) {
            if ((e as Error)?.name !== "AbortError") {
              onError?.(e instanceof Error ? e : new Error(String(e)));
            }
          }
        })
        .catch((e) => {
          if ((e as Error)?.name !== "AbortError") {
            onError?.(e instanceof Error ? e : new Error(String(e)));
          }
        });
    });
    return ac;
  }
}
