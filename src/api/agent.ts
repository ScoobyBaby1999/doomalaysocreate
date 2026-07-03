// Typed client for the agent orchestrator (the "agent" tab). Mirrors the
// backend's polling contract: POST /api/agent starts/continues a session,
// GET /api/agent/<sid>?since=<n> returns an append-only transcript delta.
// Reuses the same windowed-bearer derivation + 401-retry as PanelClient.

import { deriveToken } from "./token";
import type { Settings, PanelSnapshot } from "./panel";
import { ApiError } from "./panel";

export type AgentEvent =
  | { i: number; ts: number; type: "user"; text: string }
  | { i: number; ts: number; type: "assistant"; text: string }
  | { i: number; ts: number; type: "thinking"; text: string }
  | { i: number; ts: number; type: "tool_use"; name: string; summary: string }
  | { i: number; ts: number; type: "tool_result"; text: string; is_error: boolean }
  | {
      i: number;
      ts: number;
      type: "status";
      state: "starting" | "idle" | "running" | "error";
      detail?: string;
      cost_usd?: number | null;
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
    };

export type AgentStatus = "starting" | "idle" | "running" | "error";

export interface AgentModel {
  tier: "claude" | "open";
  provider: string;
  model: string;
  label: string;
  default: boolean;
}

export interface AgentSnapshot {
  session_id: string;
  tier: "claude" | "open" | "mock";
  model: string | null;
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
  created_at: string;
  updated_at: string;
}

export class AgentClient {
  constructor(private settings: Settings) {}

  private async bearer(windowsBack = 0): Promise<string> {
    if (this.settings.rotationSecret) return deriveToken(this.settings.rotationSecret, windowsBack);
    return this.settings.token;
  }

  private fetchWith(token: string, path: string, init?: RequestInit): Promise<Response> {
    return fetch(this.settings.baseUrl + path, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers || {}),
      },
    });
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

  /** List models this Space can actually run (for the picker). */
  models() {
    return this.req<{ tier: string | null; models: AgentModel[] }>("/api/agent/models");
  }

  /** Start a new session, or continue an existing one if sessionId is given.
   *  `model` selects which model/tier drives a NEW session.
   *  `workspaceId` links the agent to a user workspace sandbox.
   *  `chatSessionId` links to a persistent chat session for history. */
  send(message: string, sessionId?: string, model?: string, workspaceId?: string, chatSessionId?: string) {
    const body: Record<string, unknown> = { message };
    if (sessionId) body.session_id = sessionId;
    if (model) body.model = model;
    if (workspaceId) body.workspace_id = workspaceId;
    if (chatSessionId) body.chat_session_id = chatSessionId;
    const headers: Record<string, string> = {};
    if (workspaceId && this.settings.githubSessionId) {
      headers["X-JWT"] = this.settings.githubSessionId;
    }
    return this.req<AgentStart>("/api/agent", {
      method: "POST",
      body: JSON.stringify(body),
      headers,
    });
  }

  /** Stop the in-flight turn for a session. */
  interrupt(sessionId: string) {
    return this.req<{ interrupted: boolean; status: AgentStatus }>(
      `/api/agent/${sessionId}/interrupt`,
      { method: "POST" },
    );
  }

  /** Poll the transcript; `since` is the cursor returned as `next` last time.
   *  NOTE: Session ID is in the URL path (not a header) because GET requests can't
   *  have bodies. Backend logs should redact session IDs to prevent leakage via
   *  proxy/CDN access logs. A future improvement could use short-lived signed tokens. */
  poll(sessionId: string, since: number) {
    return this.req<AgentSnapshot>(`/api/agent/${sessionId}?since=${since}`);
  }

  /**
   * SSE stream for live agent events. Returns an AbortController that the
   * caller can use to disconnect.  onEvent is called for each SSE data frame
   * parsed as AgentEvent.
   *
   * Uses fetch() + ReadableStream so the Authorization header is set normally
   * (no token leakage into URL query params or server access logs).
   *
   * NOTE: The older EventSource-based overload (no args) is NOT compatible.
   * Callers MUST migrate to the new signature:
   *   const ac = client.stream(sid, since, onEvent, onError?);
   *   // later: ac.abort();
   */
  stream(
    sessionId: string,
    since: number,
    onEvent: (ev: AgentEvent) => void,
    onError?: (err: Error) => void,
  ): AbortController {
    const ac = new AbortController();
    const baseUrl = this.settings.baseUrl;
    this.bearer(0).then((token) => {
      const url = `${baseUrl}/api/agent/${sessionId}/stream?since=${since}`;
      fetch(url, {
        signal: ac.signal,
        headers: { Authorization: `Bearer ${token}` },
      })
        .then(async (r) => {
          if (!r.ok) {
            onError?.(new Error(`SSE stream returned HTTP ${r.status}`));
            return;
          }
          const reader = r.body?.getReader();
          if (!reader) { onError?.(new Error("SSE: no response body")); return; }
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
                if (line.startsWith("data: ")) {
                  try {
                    const ev = JSON.parse(line.slice(6)) as AgentEvent;
                    onEvent(ev);
                  } catch { /* skip malformed JSON */ }
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

  files(sessionId: string) {
    return this.req<{ session_id: string; files: AgentFile[] }>(`/api/agent/${sessionId}/files`);
  }

  // -- workspace / git operations -----------------------------------------

  workspaceStatus(wsId: string) {
    return this.req<{ branch: string; files: { status: string; path: string }[]; dirty: boolean }>(
      `/api/workspaces/${wsId}/status`,
    );
  }

  workspaceDiff(wsId: string) {
    return this.req<{ diff: string; has_changes: boolean }>(`/api/workspaces/${wsId}/diff`);
  }

  workspaceCommit(wsId: string, message: string) {
    return this.req<{ commit_sha: string }>(`/api/workspaces/${wsId}/commit`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
  }

  workspacePush(wsId: string, opts?: { branch?: string; force?: boolean; auto_approve?: boolean }) {
    return this.req<{ commit_sha?: string; branch?: string; status?: string }>(
      `/api/workspaces/${wsId}/push`,
      { method: "POST", body: JSON.stringify(opts || {}) },
    );
  }

  // -- chat session persistence -----------------------------------------------

  listChatSessions() {
    return this.req<{ sessions: ChatSession[] }>("/api/chat/sessions");
  }

  createChatSession(title?: string) {
    return this.req<ChatSession>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ title: title || "New Chat" }),
    });
  }

  deleteChatSession(id: string) {
    return this.req<{ deleted: string }>(`/api/chat/sessions/${id}`, {
      method: "DELETE",
    });
  }

  updateChatSession(id: string, fields: { title?: string; model?: string }) {
    return this.req<ChatSession>(`/api/chat/sessions/${id}/update`, {
      method: "POST",
      body: JSON.stringify(fields),
    });
  }

  getChatEvents(id: string) {
    return this.req<{ session_id: string; events: AgentEvent[] }>(
      `/api/chat/sessions/${id}/events`,
    );
  }

  /** Download an artifact. Plain <a href> can't carry the bearer, so we fetch
   *  the bytes ourselves and trigger a download from an object URL. */
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
}
