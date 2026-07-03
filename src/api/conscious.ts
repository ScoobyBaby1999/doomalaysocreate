/**
 * Conscious API client for Vite + React.
 * Talks to the Python critique-service backend directly (same auth pattern
 * as the existing AgentClient / PanelClient in the socreate frontend).
 *
 * Auth: windowed bearer from rotationSecret via deriveToken(), one 401 retry.
 * Base URL: settings.baseUrl (dev → "/backend", prod → "").
 */
import { deriveToken } from "./token";
import type { Settings } from "./panel";
import type { Workspace } from "./github";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

export interface Conscious {
  id: string;
  workspaceId: string;
  ownerUserId: string;
  title: string;
  goal: string;
  orchestratorAgentId: string | null;
  costCeilingUsd: number;
  costSpentUsd: number;
  brainCommitPolicy: string;
  maxAgents: number;
  status: string;
  createdAt: string;
  updatedAt: string;
}

export interface Agent {
  id: string;
  consciousId: string;
  role: string;
  model: string;
  tier: string; // "claude" | "open"
  status: string; // "idle" | "running" | "waiting" | "done" | "failed"
  worktreePath: string | null;
  branch: string | null;
  parentAgentId: string | null;
  subscribedEvents: string;
  isOrchestrator: number;
  createdAt: string;
}

export interface DrawerEntry {
  id: string;
  invokeId: string;
  fromAgentId: string;
  toAgentId: string;
  kind: string; // "invoke" | "delegate"
  task: string;
  result: string;
  status: string;
  error: string | null;
  createdAt: string;
}

export interface Proposal {
  id: string;
  section: string;
  key: string;
  value: string;
  reason: string;
  status: string; // "pending" | "committed" | "rejected"
  proposerAgentId: string;
  createdAt: string;
}

export interface BlackboardEntry {
  id: string;
  section: string;
  key: string;
  value: string;
  version: number;
  authorAgentId: string;
  committedByAgentId: string;
  createdAt: string;
}

export interface ConsciousContext {
  conscious_id: string;
  cursor: number;
  goal: string;
  orchestrator: { id: string; role: string; model: string } | null;
  self: { id: string; role: string; model: string; is_orchestrator: boolean };
  blackboard: Record<string, unknown[]>;
  events: Array<Record<string, unknown>>;
  pings: Array<{ event_id: number; type: string; summary: string }>;
  messages: Array<Record<string, unknown>>;
  proposals: Array<Record<string, unknown>>;
  tasks: Array<Record<string, unknown>>;
  drawer: Array<Record<string, unknown>>;
}

// ---------------------------------------------------------------------------
// client
// ---------------------------------------------------------------------------

export class ConsciousClient {
  constructor(private settings: Settings) {}

  private async bearer(windowsBack = 0): Promise<string> {
    if (this.settings.rotationSecret) return deriveToken(this.settings.rotationSecret, windowsBack);
    if (this.settings.token) return this.settings.token;
    // Fall back to GitHub OAuth JWT (githubSessionId) for conscious API auth
    return this.settings.githubSessionId || "";
  }

  private async fetchWith(token: string, path: string, init?: RequestInit): Promise<Response> {
    const base = this.settings.baseUrl || "";
    return fetch(base + path, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(this.settings.githubSessionId ? { "X-JWT": this.settings.githubSessionId } : {}),
        ...(init?.headers || {}),
      },
    });
  }

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
      try { const j = await r.json(); if (j?.error) msg = j.error; } catch {}
      throw new ApiError(r.status, msg);
    }
    return (await r.json()) as T;
  }

  // --- conscious lifecycle ---

  createConscious(body: {
    workspace_id: string; title: string; goal: string;
    cost_ceiling_usd?: number; brain_commit_policy?: "on" | "off";
  }): Promise<{ conscious: Conscious; agents: Agent[] }> {
    return this.req("/api/conscious", { method: "POST", body: JSON.stringify(body) });
  }

  listConscious(workspaceId: string): Promise<{ conscious: Conscious[] }> {
    return this.req(`/api/conscious?workspace_id=${encodeURIComponent(workspaceId)}`);
  }

  getConscious(cid: string): Promise<{ conscious: Conscious; agents: Agent[] }> {
    return this.req(`/api/conscious/${cid}`);
  }

  patchConscious(cid: string, body: Record<string, unknown>): Promise<{ conscious: Conscious }> {
    return this.req(`/api/conscious/${cid}`, { method: "PATCH", body: JSON.stringify(body) });
  }

  // --- workspaces ---

  listWorkspaces(): Promise<{ workspaces: Workspace[] }> {
    return this.req("/api/workspaces");
  }

  // --- agents ---

  spawnAgent(cid: string, body: {
    role: string; model: string; tier: "claude" | "open"; parent_agent_id?: string;
  }): Promise<{ agent: Agent }> {
    return this.req(`/api/conscious/${cid}/agents`, { method: "POST", body: JSON.stringify(body) });
  }

  listAgents(cid: string): Promise<{ agents: Agent[] }> {
    return this.req(`/api/conscious/${cid}/agents`);
  }

  deleteAgent(cid: string, aid: string): Promise<{ deleted: boolean }> {
    return this.req(`/api/conscious/${cid}/agents/${aid}`, { method: "DELETE" });
  }

  // --- drawer (invoke / delegate) ---

  invokeAgent(cid: string, body: {
    from_agent_id: string; to_agent_id: string;
    kind: "invoke" | "delegate"; task: string; inputs?: Record<string, unknown>;
  }): Promise<{ drawer_entry: DrawerEntry; files?: string[] }> {
    return this.req(`/api/conscious/${cid}/drawer`, { method: "POST", body: JSON.stringify(body) });
  }

  listDrawer(cid: string, opts?: { limit?: number }): Promise<{ entries: DrawerEntry[] }> {
    const q = opts?.limit ? `?limit=${opts.limit}` : "";
    return this.req(`/api/conscious/${cid}/drawer${q}`);
  }

  // --- proposals ---

  listProposals(cid: string, status?: string): Promise<{ proposals: Proposal[] }> {
    const q = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.req(`/api/conscious/${cid}/proposals${q}`);
  }

  commitProposal(cid: string, pid: string, committerAgentId: string): Promise<unknown> {
    return this.req(`/api/conscious/${cid}/proposals/${pid}/commit`, {
      method: "POST", body: JSON.stringify({ committer_agent_id: committerAgentId }),
    });
  }

  rejectProposal(cid: string, pid: string, committerAgentId: string, reason: string): Promise<unknown> {
    return this.req(`/api/conscious/${cid}/proposals/${pid}/reject`, {
      method: "POST", body: JSON.stringify({ committer_agent_id: committerAgentId, reason }),
    });
  }

  // --- blackboard ---

  postBlackboard(cid: string, body: {
    section: string; key: string; value: string; agent_id: string;
  }): Promise<unknown> {
    return this.req(`/api/conscious/${cid}/blackboard`, { method: "POST", body: JSON.stringify(body) });
  }

  // --- merge ---

  mergeAgentBranch(cid: string, aid: string, committerAgentId: string,
                   resolutions?: Record<string, string>): Promise<unknown> {
    return this.req(`/api/conscious/${cid}/agents/${aid}/merge`, {
      method: "POST",
      body: JSON.stringify({ committer_agent_id: committerAgentId, ...(resolutions ? { resolutions } : {}) }),
    });
  }

  // --- context ---

  getContext(cid: string, body: {
    agent_id: string; since?: number; include_drawer?: boolean;
  }): Promise<ConsciousContext> {
    return this.req(`/api/conscious/${cid}/context`, { method: "POST", body: JSON.stringify(body) });
  }

  // --- files ---

  listFilesOnMain(cid: string): Promise<{ branch: string; files: string[] }> {
    return this.req(`/api/conscious/${cid}/files`);
  }
}
