// GitHub + HF connection status + OAuth trigger. Shows connection status or
// a "Connect" button. Handles 401 (session expired) by prompting reconnect.

import { useEffect, useMemo, useState } from "react";
import { GitHubClient, type GithubStatus } from "../api/github";
import { ApiError, type Settings } from "../api/panel";
import { getJWTSub } from "../lib/jwt";

interface Props {
  settings: Settings;
  onConnected?: (sessionId: string, username: string) => void;
}

export function GithubConnect({ settings, onConnected }: Props) {
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sessionExpired, setSessionExpired] = useState(false);

  const client = useMemo(() => new GitHubClient(settings), [settings]);
  const hasSession = !!settings.githubSessionId;

  useEffect(() => {
    if (!settings.githubSessionId) {
      setLoading(false);
      return;
    }
    setSessionExpired(false);
    let alive = true;
    client
      .status()
      .then((s) => {
        if (alive) setStatus(s);
      })
      .catch((e) => {
        if (alive) {
          if (e instanceof ApiError && e.status === 401) {
            setSessionExpired(true);
          } else {
            setError("Failed to check connection status");
          }
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.githubSessionId]);

  function handleConnect() {
    // OAuth proxy flow: redirect to the main Space's login endpoint which handles
    // the GitHub OAuth and forwards the callback back to this user's Space.
    // If user already has a session (e.g. from HF OAuth), pass its `sub` so
    // the backend merges the GitHub account instead of creating a new one.
    const MAIN_SPACE = import.meta.env.VITE_MAIN_SPACE || "";
    const thisSpace = window.location.origin;
    if (!MAIN_SPACE) {
      console.error("VITE_MAIN_SPACE not configured - cannot complete GitHub OAuth proxy flow");
      return;
    }
    const existingId = settings.githubSessionId ? getJWTSub(settings.githubSessionId) : null;
    const existingParam = existingId ? `&existing_id=${encodeURIComponent(existingId)}` : "";
    const loginUrl = `${MAIN_SPACE}/api/auth/github/login?redirect_to=${encodeURIComponent(thisSpace)}${existingParam}`;
    window.location.href = loginUrl;
  }

  async function handleDisconnect() {
    try {
      await client.disconnect();
      onConnected?.("", "");
      setStatus(null);
    } catch {
      setError("Failed to disconnect");
    }
  }

  if (loading) {
    return (
      <span className="text-[11px] text-muted animate-pulse">checking connection…</span>
    );
  }

  if (sessionExpired) {
    return (
      <div className="flex items-center gap-2 text-[11px]">
        <span className="text-amber-400">session expired</span>
        <button
          onClick={handleConnect}
          className="px-2 py-0.5 rounded border border-amber-500/40 text-amber-300 hover:bg-amber-500/10"
        >
          reconnect
        </button>
      </div>
    );
  }

  if (error) {
    return (
      <span className="text-[11px] text-rose-300">{error}</span>
    );
  }

  // If they have a GitHub connection, show fully connected
  if (hasSession && status?.github_username) {
    return (
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="text-green-400">✓</span>
        <span className="text-muted">{status.github_username}</span>
        {status.hf_username && (
          <span className="text-muted/60">· HF: {status.hf_username}</span>
        )}
        <button
          onClick={handleDisconnect}
          className="text-muted hover:text-rose-300 underline ml-1"
        >
          disconnect
        </button>
      </div>
    );
  }

  // If they have any session (HF only) they need to connect GitHub
  return (
    <button
      onClick={handleConnect}
      className="text-[11px] px-2 py-1 rounded-xl border border-border hover:border-accent text-accent"
    >
      🔗 Connect GitHub
    </button>
  );
}
