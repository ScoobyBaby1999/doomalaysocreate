// GitHub + HF connection status + OAuth trigger. Shows connection status or
// a "Connect" button. Handles 401 (session expired) by prompting reconnect.

import { useEffect, useMemo, useState } from "react";
import { GitHubClient, type GithubStatus } from "../api/github";
import type { Settings } from "../api/panel";

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
  const connected = !!settings.githubSessionId;

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
          if (e instanceof Error && e.message.includes("401")) {
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
    const MAIN_SPACE = "https://scoobybaby1999-loom.hf.space";
    const thisSpace = window.location.origin;
    const loginUrl = `${MAIN_SPACE}/api/auth/github/login?redirect_to=${encodeURIComponent(thisSpace)}`;
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

  if (connected && status?.authenticated) {
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

  return (
    <button
      onClick={handleConnect}
      className="text-[11px] px-2 py-1 rounded-lg border border-border hover:border-accent text-accent"
    >
      🔗 Connect Accounts
    </button>
  );
}
