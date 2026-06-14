// Typed client for the GitHub integration backend (critique-service/github_integration.py).
// Mirrors all /api/auth/*, /api/github/*, /api/workspaces/*, /api/registry/* routes.
// Uses githubSessionId as bearer (the user_id from GitHub OAuth — not the rotation secret).

import type { Settings } from "./panel";
import { ApiError } from "./panel";

// --- Types ----------------------------------------------------------------

export interface GithubStatus {
  authenticated: boolean;
  github_username: string | null;
  hf_username: string | null;
}

export interface Repo {
  full_name: string;
  name: string;
  owner: string;
  private: boolean;
  default_branch: string;
  description: string;
  updated_at: string;
  clone_url: string;
}

export interface Branch {
  name: string;
  sha: string;
}

export interface Workspace {
  id: string;
  user_id: string;
  source_repo: string | null;
  source_branch: string | null;
  current_branch: string;
  sandbox_path: string;
  hf_space_id: string | null;
  auto_sync: number;
  visibility: string;
  title: string;
  description: string;
  created_at: string;
  last_modified: string;
}

export interface PushRequest {
  status: string;
  request_id: string;
  action: string;
  created_at?: number;
  branch?: string;
  title?: string;
}

export interface PushResult {
  commit_sha: string;
  branch: string;
}

export interface PrResult {
  number: number;
  html_url: string;
  state: string;
  title: string;
}

export interface PushLog {
  id: string;
  workspace_id: string;
  target_repo: string;
  target_branch: string;
  commit_sha: string;
  commit_message: string;
  push_type: string;
  pr_number: number | null;
  pr_url: string | null;
  approved_by_user: number;
  auto_approved: number;
  files_changed: number;
  created_at: string;
}

export interface RegistryEntry {
  workspace_id: string;
  indexed_at: string;
  owner_username: string;
  owner_avatar: string;
  title: string;
  description: string;
  source_repo: string | null;
  hf_space_url: string | null;
}

export interface RegistryPage {
  items: RegistryEntry[];
  total: number;
  page: number;
  per_page: number;
}

// --- Client --------------------------------------------------------------

export class GitHubClient {
  constructor(private settings: Settings) {}

  private get sessionId(): string {
    return this.settings.githubSessionId || "";
  }

  private async raw(path: string, init?: RequestInit): Promise<Response> {
    const token = this.sessionId;
    return fetch(this.settings.baseUrl + path, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers || {}),
      },
    });
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    let r = await this.raw(path, init);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        if (j?.error) msg = j.error;
      } catch {
        /* non-json error body */
      }
      throw new ApiError(r.status, msg);
    }
    return (await r.json()) as T;
  }

  // -- Auth ----------------------------------------------------------------

  /** URL to redirect the browser to for GitHub OAuth. */
  loginUrl(): string {
    return this.settings.baseUrl + "/api/auth/github/login";
  }

  /** Check if GitHub is connected for this session. */
  status(): Promise<GithubStatus> {
    return this.req<GithubStatus>("/api/auth/status");
  }

  /** Disconnect GitHub (removes stored token). */
  disconnect(): Promise<{ disconnected: boolean }> {
    return this.req<{ disconnected: boolean }>("/api/auth/github/disconnect", {
      method: "POST",
    });
  }

  // -- Repos ---------------------------------------------------------------

  repos(page = 1, perPage = 30): Promise<{ repos: Repo[] }> {
    return this.req<{ repos: Repo[] }>(
      `/api/github/repos?page=${page}&per_page=${perPage}&sort=updated`,
    );
  }

  branches(owner: string, repo: string): Promise<{ branches: Branch[] }> {
    return this.req<{ branches: Branch[] }>(
      `/api/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/branches`,
    );
  }

  contents(
    owner: string,
    repo: string,
    path: string,
    ref = "main",
  ): Promise<Record<string, unknown>> {
    return this.req<Record<string, unknown>>(
      `/api/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/contents?path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`,
    );
  }

  tree(
    owner: string,
    repo: string,
    ref = "main",
  ): Promise<{ tree: Array<Record<string, unknown>> }> {
    return this.req<{ tree: Array<Record<string, unknown>> }>(
      `/api/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/tree?ref=${encodeURIComponent(ref)}`,
    );
  }

  // -- Workspaces ----------------------------------------------------------

  listWorkspaces(): Promise<{ workspaces: Workspace[] }> {
    return this.req<{ workspaces: Workspace[] }>("/api/workspaces");
  }

  createWorkspace(opts: {
    title: string;
    description?: string;
    source_repo?: string;
    source_branch?: string;
    visibility?: string;
    auto_sync?: boolean;
  }): Promise<Workspace> {
    return this.req<Workspace>("/api/workspaces", {
      method: "POST",
      body: JSON.stringify(opts),
    });
  }

  getWorkspace(id: string): Promise<Workspace> {
    return this.req<Workspace>(`/api/workspaces/${encodeURIComponent(id)}`);
  }

  updateWorkspace(
    id: string,
    fields: Partial<Pick<Workspace, "title" | "description" | "visibility" | "current_branch" | "auto_sync">>,
  ): Promise<Workspace> {
    return this.req<Workspace>(`/api/workspaces/${encodeURIComponent(id)}/update`, {
      method: "POST",
      body: JSON.stringify(fields),
    });
  }

  deleteWorkspace(id: string): Promise<{ deleted: string }> {
    return this.req<{ deleted: string }>(
      `/api/workspaces/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  }

  // -- Git Operations ------------------------------------------------------

  commit(
    id: string,
    message: string,
  ): Promise<{ commit_sha: string }> {
    return this.req<{ commit_sha: string }>(
      `/api/workspaces/${encodeURIComponent(id)}/commit`,
      { method: "POST", body: JSON.stringify({ message }) },
    );
  }

  push(
    id: string,
    opts: { branch?: string; force?: boolean; auto_approve?: boolean } = {},
  ): Promise<PushResult | PushRequest> {
    return this.req<PushResult | PushRequest>(
      `/api/workspaces/${encodeURIComponent(id)}/push`,
      { method: "POST", body: JSON.stringify(opts) },
    );
  }

  createPr(
    id: string,
    opts: {
      title: string;
      body?: string;
      head_branch?: string;
      base_branch?: string;
      auto_approve?: boolean;
    },
  ): Promise<PrResult | PushRequest> {
    return this.req<PrResult | PushRequest>(
      `/api/workspaces/${encodeURIComponent(id)}/pr`,
      { method: "POST", body: JSON.stringify(opts) },
    );
  }

  checkout(
    id: string,
    branch: string,
  ): Promise<{ branch: string }> {
    return this.req<{ branch: string }>(
      `/api/workspaces/${encodeURIComponent(id)}/checkout`,
      { method: "POST", body: JSON.stringify({ branch }) },
    );
  }

  // -- Push Approval -------------------------------------------------------

  pushRequestStatus(requestId: string): Promise<PushRequest> {
    return this.req<PushRequest>(
      `/api/auth/push-requests/${encodeURIComponent(requestId)}`,
    );
  }

  resolvePushRequest(
    requestId: string,
    approved: boolean,
  ): Promise<{ status: string; commit_sha?: string; html_url?: string }> {
    return this.req<{ status: string; commit_sha?: string; html_url?: string }>(
      `/api/auth/push-requests/${encodeURIComponent(requestId)}/resolve`,
      { method: "POST", body: JSON.stringify({ approved }) },
    );
  }

  // -- Push Logs -----------------------------------------------------------

  pushLogs(id: string, limit = 50): Promise<{ logs: PushLog[] }> {
    return this.req<{ logs: PushLog[] }>(
      `/api/workspaces/${encodeURIComponent(id)}/logs?limit=${limit}`,
    );
  }

  // -- Registry ------------------------------------------------------------

  publish(id: string): Promise<RegistryEntry> {
    return this.req<RegistryEntry>(
      `/api/workspaces/${encodeURIComponent(id)}/publish`,
      { method: "POST" },
    );
  }

  unpublish(id: string): Promise<{ unpublished: string }> {
    return this.req<{ unpublished: string }>(
      `/api/workspaces/${encodeURIComponent(id)}/unpublish`,
      { method: "POST" },
    );
  }

  listRegistry(
    params: { page?: number; per_page?: number; sort?: string; search?: string } = {},
  ): Promise<RegistryPage> {
    const q = new URLSearchParams();
    if (params.page) q.set("page", String(params.page));
    if (params.per_page) q.set("per_page", String(params.per_page));
    if (params.sort) q.set("sort", params.sort);
    if (params.search) q.set("search", params.search);
    const qs = q.toString();
    return this.req<RegistryPage>(`/api/registry/spaces${qs ? "?" + qs : ""}`);
  }
}
