import { useCallback, useEffect, useState } from "react";
import type { AgentClient } from "../api/agent";

interface StatusData {
  branch: string;
  files: { status: string; path: string }[];
  dirty: boolean;
}

interface PushResult {
  commit_sha?: string;
  branch?: string;
  status?: string;
  request_id?: string;
}

export function GitStatus({
  client,
  wsId,
}: {
  client: AgentClient;
  wsId: string | null;
}) {
  const [status, setStatus] = useState<StatusData | null>(null);
  const [loading, setLoading] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [message, setMessage] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!wsId) return;
    setLoading(true);
    setError("");
    try {
      const s = await client.workspaceStatus(wsId);
      setStatus(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : "status failed");
    } finally {
      setLoading(false);
    }
  }, [client, wsId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCommit() {
    if (!wsId || !message.trim()) return;
    setCommitting(true);
    setResult("");
    setError("");
    try {
      const r = await client.workspaceCommit(wsId, message.trim());
      setResult(`Committed: ${r.commit_sha.slice(0, 8)}`);
      setMessage("");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "commit failed");
    } finally {
      setCommitting(false);
    }
  }

  async function handlePush() {
    if (!wsId) return;
    setPushing(true);
    setResult("");
    setError("");
    try {
      const r: PushResult = await client.workspacePush(wsId, { auto_approve: false });
      if (r.status === "pending_approval") {
        setResult(`Push queued (request: ${r.request_id?.slice(0, 8)}…). Approve in Settings > Pending.`);
      } else {
        setResult(`Pushed to ${r.branch || "remote"}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "push failed");
    } finally {
      setPushing(false);
    }
  }

  if (!wsId) return null;

  return (
    <div className="border-t border-border bg-surface/50">
      {/* branch + status bar */}
      <div className="flex items-center gap-2 px-3 py-1.5 text-[11px]">
        <span className="font-mono text-accent">⎇ {status?.branch || "…"}</span>
        {status?.dirty && (
          <span className="text-amber-400">{status.files.length} changed</span>
        )}
        {!status?.dirty && status && (
          <span className="text-green-400">clean</span>
        )}
        <button
          onClick={refresh}
          className="ml-auto text-muted hover:text-accent"
          disabled={loading}
        >
          {loading ? "…" : "↻"}
        </button>
      </div>

      {/* changed files list */}
      {status?.files && status.files.length > 0 && (
        <div className="max-h-[120px] overflow-y-auto px-3 pb-1">
          {status.files.map((f) => (
            <div key={f.path} className="flex items-center gap-2 text-[11px] font-mono py-0.5">
              <span className={`shrink-0 w-5 text-center ${
                f.status === "M" ? "text-amber-400" :
                f.status === "A" ? "text-green-400" :
                f.status === "D" ? "text-red-400" : "text-muted"
              }`}>
                {f.status === "M" ? "~" : f.status === "A" ? "+" : f.status === "D" ? "-" : "?"}
              </span>
              <span className="truncate text-muted">{f.path}</span>
            </div>
          ))}
        </div>
      )}

      {/* commit form */}
      {status?.dirty && (
        <div className="px-3 pb-2 flex gap-2">
          <input
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="commit message…"
            className="flex-1 bg-surface2 border border-border rounded px-2 py-1 text-[12px] text-text placeholder:text-muted outline-none focus:border-accent"
            onKeyDown={(e) => e.key === "Enter" && handleCommit()}
          />
          <button
            onClick={handleCommit}
            disabled={committing || !message.trim()}
            className="text-[11px] px-2 py-1 rounded bg-accent text-white disabled:opacity-40"
          >
            {committing ? "…" : "✓"}
          </button>
          <button
            onClick={handlePush}
            disabled={pushing}
            className="text-[11px] px-2 py-1 rounded border border-border text-muted hover:text-accent disabled:opacity-40"
            title="Push to remote"
          >
            {pushing ? "…" : "↑"}
          </button>
        </div>
      )}

      {/* result / error */}
      {result && <div className="px-3 pb-2 text-[11px] text-green-400">{result}</div>}
      {error && <div className="px-3 pb-2 text-[11px] text-red-400">{error}</div>}
    </div>
  );
}
