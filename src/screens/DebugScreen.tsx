import { useCallback, useEffect, useState } from "react";
import { deriveToken } from "../api/token";
import type { Settings } from "../api/panel";

/** Debug log viewer — fetches /api/debug/logs and displays them.
 *  Accessible from the nav bar on Android (no terminal needed).
 *  Shows: startup env detection, agent events, conscious errors.
 *  Categories: startup, agent, conscious, errors, http.
 *  Auto-refreshes every 5s when open. */

interface LogEntry {
  ts: string;
  level: string;
  cat: string;
  fn: string;
  msg: string;
  data?: Record<string, unknown>;
  ms?: number;
}

const CATEGORIES = [
  { id: "", label: "All" },
  { id: "startup", label: "Startup" },
  { id: "agent", label: "Agent" },
  { id: "conscious", label: "Conscious" },
  { id: "errors", label: "Errors" },
  { id: "auth", label: "Auth" },
  { id: "http", label: "HTTP" },
];

const LEVEL_COLORS: Record<string, string> = {
  ERROR: "text-rose-300 bg-rose-500/10",
  WARN: "text-amber-300 bg-amber-500/10",
  INFO: "text-purple-300 bg-purple-500/10",
  DEBUG: "text-muted bg-surface2",
};

export function DebugScreen({ settings }: { settings: Settings }) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [category, setCategory] = useState("");
  const [level, setLevel] = useState("");
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [error, setError] = useState("");

  const fetchLogs = useCallback(async () => {
    if (!settings.token && !settings.rotationSecret) {
      setError("No auth token set — configure in Settings first.");
      return;
    }
    setLoading(true);
    try {
      const token = settings.rotationSecret
        ? await deriveToken(settings.rotationSecret)
        : settings.token;
      const params = new URLSearchParams();
      if (category) params.set("cat", category);
      params.set("tail", "100");
      if (level) params.set("level", level);
      const qs = params.toString();
      const r = await fetch(
        `${settings.baseUrl}/api/debug/logs${qs ? "?" + qs : ""}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j as { error?: string }).error || `HTTP ${r.status}`);
      }
      const data = await r.json();
      setLogs(data.logs || []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [settings, category, level]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  // Auto-refresh every 5s
  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchLogs]);

  async function clearLogs() {
    try {
      const token = settings.rotationSecret
        ? await deriveToken(settings.rotationSecret)
        : settings.token;
      const params = category ? `?cat=${category}` : "";
      await fetch(`${settings.baseUrl}/api/debug/clear${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      fetchLogs();
    } catch {}
  }

  return (
    <div className="flex flex-col h-full">
      {/* header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Debug Logs</span>
          {logs.length > 0 && (
            <span className="text-[10px] text-muted bg-surface2 px-1.5 py-0.5 rounded-full">
              {logs.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setAutoRefresh((v) => !v)}
            className={`text-[11px] px-2 py-1 rounded-xl ${
              autoRefresh
                ? "bg-green-500/20 text-green-400"
                : "bg-surface2 text-muted"
            }`}
          >
            {autoRefresh ? "● live" : "○ paused"}
          </button>
          <button
            onClick={fetchLogs}
            className="text-[11px] px-2 py-1 rounded-xl bg-surface2 text-muted"
          >
            ↻
          </button>
          <button
            onClick={clearLogs}
            className="text-[11px] px-2 py-1 rounded-xl bg-rose-500/20 text-rose-300"
          >
            clear
          </button>
        </div>
      </div>

      {/* filters */}
      <div className="flex gap-1 px-3 py-2 border-b border-border overflow-x-auto">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            onClick={() => setCategory(c.id)}
            className={`text-[11px] px-2 py-1 rounded-xl whitespace-nowrap ${
              category === c.id
                ? "bg-accent text-white"
                : "bg-surface2 text-muted"
            }`}
          >
            {c.label}
          </button>
        ))}
        <div className="w-px bg-border mx-1" />
        {["", "ERROR", "WARN", "INFO"].map((l) => (
          <button
            key={l}
            onClick={() => setLevel(l)}
            className={`text-[11px] px-2 py-1 rounded-xl whitespace-nowrap ${
              level === l
                ? "bg-accent text-white"
                : "bg-surface2 text-muted"
            }`}
          >
            {l || "All levels"}
          </button>
        ))}
      </div>

      {/* error */}
      {error && (
        <div className="px-4 py-2 bg-rose-500/10 border-b border-rose-500/30 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {/* logs */}
      <div className="flex-1 overflow-y-auto p-3 space-y-1.5 max-h-[calc(100vh-180px)]">
        {loading && logs.length === 0 ? (
          <div className="text-center text-muted text-sm mt-8">Loading…</div>
        ) : logs.length === 0 ? (
          <div className="text-center text-muted text-sm mt-8">
            No logs found. Trigger some actions (chat, agent, conscious) and
            logs will appear here.
          </div>
        ) : (
          logs.map((log, i) => <LogRow key={i} log={log} />)
        )}
      </div>
    </div>
  );
}

function LogRow({ log }: { log: LogEntry }) {
  const [expanded, setExpanded] = useState(false);
  // BATCH-3 Task 3 — "Copied!" feedback for the copy-to-clipboard icon.
  const [copied, setCopied] = useState(false);
  const levelColor = LEVEL_COLORS[log.level] || "text-muted bg-surface2";

  // BATCH-3 Task 3 — copy the full log entry (header + message + data) to
  // the clipboard. The header line mimics the on-screen row so the pasted
  // text is easy to scan in a bug report.
  const handleCopy = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    const header = `${log.ts} [${log.level}] [${log.cat}] ${log.fn}${log.ms != null ? ` (${log.ms}ms)` : ""}`;
    const body = log.msg || "";
    const dataStr = log.data ? `\n${JSON.stringify(log.data, null, 2)}` : "";
    const text = `${header}\n${body}${dataStr}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — non-fatal */
    }
  }, [log]);

  return (
    <div
      className={`rounded-xl border border-border p-2 ${levelColor}`}
      onClick={() => setExpanded((v) => !v)}
    >
      <div className="flex items-center gap-2 text-[11px]">
        <span className="font-mono text-muted shrink-0">
          {log.ts.slice(11, 19)}
        </span>
        <span className="font-bold shrink-0">{log.level}</span>
        <span className="text-muted shrink-0">[{log.cat}]</span>
        <span className="font-mono text-muted truncate flex-1 min-w-0">{log.fn}</span>
        {log.ms != null && (
          <span className="text-muted shrink-0">{log.ms}ms</span>
        )}
        {/* BATCH-3 Task 3 — copy icon (clipboard). Stops propagation so the
            row doesn't expand/collapse when the icon is tapped. Shows a
            green check + "Copied!" for 1.5s after a successful copy. */}
        <button
          onClick={handleCopy}
          className={`shrink-0 inline-flex items-center gap-1 px-1.5 h-6 rounded-md transition-colors ${
            copied
              ? "text-emerald-400 bg-emerald-500/15"
              : "text-muted-foreground hover:text-foreground hover:bg-surface2"
          }`}
          title={copied ? "Copied!" : "Copy log to clipboard"}
          aria-label={copied ? "Copied" : "Copy log"}
        >
          {copied ? (
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          ) : (
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          )}
          {copied && <span className="text-[9px]">Copied!</span>}
        </button>
      </div>
      <div className="text-[12px] mt-1 break-words">{log.msg}</div>
      {expanded && log.data && (
        <pre className="mt-1.5 text-[10px] font-mono whitespace-pre-wrap break-words bg-black/20 rounded p-1.5 overflow-x-auto">
          {JSON.stringify(log.data, null, 2)}
        </pre>
      )}
      {expanded && (
        <div className="text-[10px] text-muted mt-1">tap to collapse</div>
      )}
    </div>
  );
}
