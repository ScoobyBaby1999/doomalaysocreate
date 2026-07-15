// Typed client for the template library — the user-curated catalog of
// reusable prompts for web search, deep research, judge panels, and chat.
//
// Mirrors the backend's /api/templates* endpoints:
//   GET    /api/templates                 — current user's templates
//   GET    /api/templates/explore         — public templates (sorted/searched)
//   GET    /api/templates/:id             — single template
//   POST   /api/templates                 — create
//   PATCH  /api/templates/:id             — update
//   DELETE /api/templates/:id             — delete
//   POST   /api/templates/:id/heart       — toggle heart
//   POST   /api/templates/:id/download    — download (creates a local copy)
//   POST   /api/templates/:id/publish     — publish (make public)
//   POST   /api/templates/:id/unpublish   — unpublish (back to private)
//
// Auth: same as other endpoints — bearer token in Authorization header,
// X-JWT for GitHub identity (used by the backend to attribute ownership).

import { deriveToken } from "./token";
import type { Settings } from "./panel";
import { ApiError } from "./panel";

export type TemplateKind =
  | "websearch"
  | "deepresearch"
  | "judge"
  | "chat"
  | "custom";

export interface Template {
  id: string;
  author_id: string;
  author_name: string | null;
  name: string;
  description: string | null;
  markdown: string;
  kind: TemplateKind;
  tags: string[];
  is_public: boolean;
  hearts: number;
  downloads: number;
  /** Did the current user heart this template? */
  hearted: boolean;
  /** Did the current user download this template (i.e. has a local copy)? */
  downloaded: boolean;
  created_at: string;
  updated_at: string;
}

export interface ExploreSort {
  sort: "hearts" | "recent" | "relevant";
  query?: string;
  kind?: string;
  limit?: number;
  offset?: number;
}

export class TemplateClient {
  constructor(private settings: Settings) {}

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

  /** Optional X-JWT header for GitHub-identity routes (template ownership). */
  private jwtHeaders(): Record<string, string> {
    return this.settings.githubSessionId ? { "X-JWT": this.settings.githubSessionId } : {};
  }

  // -- GET /api/templates --------------------------------------------------
  listMine(): Promise<{ templates: Template[] }> {
    return this.req("/api/templates", { headers: this.jwtHeaders() });
  }

  // -- GET /api/templates/explore -----------------------------------------
  explore(opts: ExploreSort): Promise<{ templates: Template[]; total: number }> {
    const params = new URLSearchParams();
    params.set("sort", opts.sort);
    if (opts.query) params.set("query", opts.query);
    if (opts.kind) params.set("kind", opts.kind);
    if (typeof opts.limit === "number") params.set("limit", String(opts.limit));
    if (typeof opts.offset === "number") params.set("offset", String(opts.offset));
    return this.req(`/api/templates/explore?${params.toString()}`, {
      headers: this.jwtHeaders(),
    });
  }

  // -- GET /api/templates/:id ---------------------------------------------
  get(id: string): Promise<{ template: Template }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}`, {
      headers: this.jwtHeaders(),
    });
  }

  // -- POST /api/templates -------------------------------------------------
  create(data: {
    name: string;
    description?: string;
    markdown: string;
    kind: string;
    tags?: string[];
    is_public?: boolean;
  }): Promise<{ template: Template }> {
    return this.req("/api/templates", {
      method: "POST",
      body: JSON.stringify(data),
      headers: this.jwtHeaders(),
    });
  }

  // -- PATCH /api/templates/:id -------------------------------------------
  update(
    id: string,
    data: Partial<{
      name: string;
      description: string;
      markdown: string;
      tags: string[];
      is_public: boolean;
    }>,
  ): Promise<{ template: Template }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
      headers: this.jwtHeaders(),
    });
  }

  // -- DELETE /api/templates/:id ------------------------------------------
  delete(id: string): Promise<{ ok: boolean }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: this.jwtHeaders(),
    });
  }

  // -- POST /api/templates/:id/heart --------------------------------------
  heart(id: string): Promise<{ hearted: boolean; hearts: number }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}/heart`, {
      method: "POST",
      headers: this.jwtHeaders(),
    });
  }

  // -- POST /api/templates/:id/download -----------------------------------
  download(id: string): Promise<{ template: Template }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}/download`, {
      method: "POST",
      headers: this.jwtHeaders(),
    });
  }

  // -- POST /api/templates/:id/publish ------------------------------------
  publish(id: string): Promise<{ template: Template }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}/publish`, {
      method: "POST",
      headers: this.jwtHeaders(),
    });
  }

  // -- POST /api/templates/:id/unpublish ----------------------------------
  unpublish(id: string): Promise<{ template: Template }> {
    return this.req(`/api/templates/${encodeURIComponent(id)}/unpublish`, {
      method: "POST",
      headers: this.jwtHeaders(),
    });
  }
}
