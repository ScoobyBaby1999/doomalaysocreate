// Typed client for the agent orchestrator (the "agent" tab). Mirrors the
// backend's polling contract: POST /api/agent starts/continues a session,
// GET /api/agent/<sid>?since=<n> returns an append-only transcript delta.
// Reuses the same windowed-bearer derivation + 401-retry as PanelClient.

import { deriveToken } from "./token";
import type { Settings } from "./panel";
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
}

export interface AgentFile {
  path: string;
  size: number;
  mtime: number;
}

export class AgentClient {
  constructor(private settings: Settings) {}

  private async bearer(windowsBack = 0): Promise<string> {
    // Fix 2: rotationSecret ALWAYS takes precedence (Layer 1: service auth).
    // The JWT goes in X-JWT (Layer 2: user identity), NEVER as the bearer.
    if (this.settings.rotationSecret) return deriveToken(this.settings.rotationSecret, windowsBack);
    return this.settings.token;
  }

  private fetchWith(token: string, path: string, init?: RequestInit): Promise<Response> {
    // Fix 2: send X-JWT when githubSessionId is set (Layer 2: user identity).
    // Same pattern as conscious.ts.
    const headers: Record<string, string> = {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(this.settings.githubSessionId ? { "X-JWT": this.settings.githubSessionId } : {}),
      ...((init?.headers as Record<string, string>) || {}),
    };
    return fetch(this.settings.baseUrl + path, { ...init, credentials: "same-origin", headers });
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
   *  Fix 2: X-JWT is now sent globally by fetchWith() — no need to set it here. */
  send(message: string, sessionId?: string, model?: string, workspaceId?: string) {
    const body: Record<string, unknown> = { message };
    if (sessionId) body.session_id = sessionId;
    if (model) body.model = model;
    if (workspaceId) body.workspace_id = workspaceId;
    return this.req<AgentStart>("/api/agent", {
      method: "POST",
      body: JSON.stringify(body),
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

  files(sessionId: string) {
    return this.req<{ session_id: string; files: AgentFile[] }>(`/api/agent/${sessionId}/files`);
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
