// Typed client for the loom panel gateway (the backend we built in critique-service/).
// Mirrors its real endpoints: /health, /api/roster, /api/panel, /api/run, /api/jobs/:id.
// Async-first: submit a job, then poll the snapshot for per-judge streaming progress
// (the gateway exposes content_chars / reasoning_chars / tail per running judge).

import { deriveToken } from "./token";

export interface Settings {
  baseUrl: string; // "" when served by the gateway itself, "/backend" in dev (proxied), or "https://<space>.hf.space"
  token: string; // static CRITIQUE_TOKEN, or a gen_token.py windowed token
  rotationSecret: string; // CRITIQUE_ROTATION_SECRET; preferred - wire token derived per call
}

export interface JudgeProgress {
  content_chars: number;
  reasoning_chars: number;
  tail: string;
  updated_at: number;
}

export interface Judge {
  model: string;
  status?: "pending" | "running" | "done" | "error";
  ok?: boolean;
  routed_to?: string;
  output?: string;
  critique?: string;
  error?: string;
  elapsed_s?: number;
  reasoning?: string;
  progress?: JudgeProgress;
  coverage?: { files_included: number; files_total: number; dropped: string[] };
  cached?: boolean;
}

export interface PanelSnapshot {
  job_id: string;
  status: "running" | "complete";
  kind: string;
  role: string;
  merge: string;
  judges: Judge[];
  merged: string;
  meta: {
    judges_ok: number;
    judges_total: number;
    judges_settled: number;
    complete: boolean;
    age_s: number;
  };
  pack?: { files: number; skipped: number; total_tokens: number };
}

export interface RosterModel {
  logical: string;
  frontier: boolean;
  arena_elo: number | null;
  privacy_safe_routable: boolean;
  routable: boolean;
  hosts: { slot: string; privacy_safe: boolean; cooling: boolean; blacklisted: boolean }[];
}

export interface Roster {
  models: RosterModel[];
  frontier_total: number;
  frontier_privacy_safe_available: number;
  frontier_ok: boolean;
}

export type Privacy = "strict" | "fallback" | "off";
export type Effort = "low" | "med" | "high" | "max";

export interface PanelRequest {
  input?: string;
  system?: string;
  role?: string;
  panel?: string[];
  merge?: string;
  effort?: Effort;
  privacy?: Privacy;
  reasoning?: boolean;
  research?: boolean;
  max_tokens?: number;
  profile?: string;
  files?: Record<string, string>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class PanelClient {
  constructor(private settings: Settings) {}

  /** The bearer for this call: derived fresh from the rotation secret when set
   *  (so it auto-rotates), else the static token. */
  private async bearer(windowsBack = 0): Promise<string> {
    if (this.settings.rotationSecret) return deriveToken(this.settings.rotationSecret, windowsBack);
    return this.settings.token;
  }

  private fetchWith(token: string, path: string, init?: RequestInit): Promise<Response> {
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
    let r = await this.fetchWith(await this.bearer(), path, init);
    if (r.status === 401 && this.settings.rotationSecret) {
      // window-boundary / clock-skew insurance: the server still accepts the
      // previous window's token, so retry once with it before surfacing a 401.
      r = await this.fetchWith(await this.bearer(1), path, init);
    }
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

  health() {
    return this.req<Record<string, unknown>>("/health");
  }

  roster() {
    return this.req<Roster>("/api/roster");
  }

  /** Submit an async panel job; returns the initial (all-pending) snapshot with a job_id. */
  submitPanel(body: PanelRequest) {
    return this.req<PanelSnapshot>("/api/panel", {
      method: "POST",
      body: JSON.stringify({ ...body, async: true }),
    });
  }

  job(jobId: string, trace = false) {
    return this.req<PanelSnapshot>(`/api/jobs/${jobId}${trace ? "?trace=true" : ""}`);
  }

  /**
   * Run a panel job to completion, calling onUpdate with each polled snapshot so the UI
   * can stream per-judge progress. Resolves with the final snapshot.
   */
  async runPanel(
    body: PanelRequest,
    onUpdate: (s: PanelSnapshot) => void,
    opts: { intervalMs?: number; signal?: AbortSignal } = {},
  ): Promise<PanelSnapshot> {
    const interval = opts.intervalMs ?? 1200;
    let snap = await this.submitPanel(body);
    onUpdate(snap);
    while (!snap.meta.complete) {
      if (opts.signal?.aborted) throw new DOMException("aborted", "AbortError");
      await new Promise((res) => setTimeout(res, interval));
      snap = await this.job(snap.job_id);
      onUpdate(snap);
    }
    return snap;
  }
}
