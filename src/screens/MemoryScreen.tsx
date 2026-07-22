// Memory panel — displays the .pied sanity log state for the active workspace.
// Shows: goal, plan, pending/completed tasks, recent events, blackboard entries.

import { useEffect, useState, useCallback, useMemo } from "react";
import { AgentClient } from "../api/agent";
import { useChatStore } from "../state/chatStore";
import type { Settings } from "../api/panel";

interface MemoryState {
  goal?: string;
  plan?: string;
  status?: string;
  pending_tasks?: { task: string; assigned_to: string; status: string }[];
  completed_tasks?: { task: string; agent: string; completed_at: number }[];
}
interface LogEntry {
  ts: number;
  agent: string;
  kind: string;
  data: Record<string, unknown>;
}
interface BlackboardEntry {
  ts: number;
  agent: string;
  key: string;
  value: string;
}

export function MemoryScreen({ settings }: { settings: Settings }) {
  const workspaceId = useChatStore((s) => s.workspaceId);
  const [state, setState] = useState<MemoryState | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [blackboard, setBlackboard] = useState<BlackboardEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const client = useMemo(() => new AgentClient(settings), [settings]);

  const fetchMemory = useCallback(async () => {
    if (!workspaceId) {
      setError("No workspace selected. Select one in the Chat tab.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const r = await client.getMemory(workspaceId);
      setState(r.state || null);
      setLog(r.recent_log || []);
      setBlackboard(r.blackboard || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load memory");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, settings]);

  useEffect(() => {
    fetchMemory();
  }, [fetchMemory]);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted shrink-0">
        <span className="font-medium text-text">Memory Layer (.pied)</span>
        {workspaceId && (
          <span className="px-1.5 py-0.5 rounded bg-surface2 text-[10px] truncate max-w-[120px]">
            {workspaceId.slice(0, 12)}
          </span>
        )}
        <button
          onClick={fetchMemory}
          disabled={loading || !workspaceId}
          className="ml-auto px-2 py-0.5 rounded bg-accent/10 text-accent text-[10px] disabled:opacity-50"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <div className="px-3 py-2 text-sm text-amber-300 border-b border-amber-500/30 bg-amber-500/10">
          {error}
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {/* Goal + Plan */}
        {state && (
          <div className="space-y-2">
            <div className="rounded-xl border border-border bg-surface/40 p-3">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground/60 mb-1">Goal</div>
              <div className="text-sm text-foreground">{state.goal || "(not set)"}</div>
            </div>
            <div className="rounded-xl border border-border bg-surface/40 p-3">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground/60 mb-1">Plan</div>
              <div className="text-sm text-foreground whitespace-pre-wrap">{state.plan || "(not set)"}</div>
            </div>
          </div>
        )}

        {/* Tasks */}
        {state && (state.pending_tasks?.length || state.completed_tasks?.length) ? (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground/60 mb-1.5">Tasks</div>
            <div className="space-y-1">
              {state.pending_tasks?.map((t, i) => (
                <div key={i} className="flex items-center gap-2 text-xs px-2 py-1 rounded border border-border/50">
                  <span className="size-1.5 rounded-full bg-amber-400" />
                  <span className="flex-1 truncate">{t.task}</span>
                  <span className="text-[9px] text-muted-foreground">{t.assigned_to}</span>
                </div>
              ))}
              {state.completed_tasks?.slice(-5).map((t, i) => (
                <div key={i} className="flex items-center gap-2 text-xs px-2 py-1 rounded border border-border/30 opacity-60">
                  <span className="size-1.5 rounded-full bg-emerald-400" />
                  <span className="flex-1 truncate">{t.task}</span>
                  <span className="text-[9px] text-muted-foreground">{t.agent}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {/* Recent Events */}
        {log.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground/60 mb-1.5">Recent Events</div>
            <div className="space-y-0.5 max-h-48 overflow-y-auto">
              {log.map((ev, i) => (
                <div key={i} className="flex items-center gap-2 text-[11px] px-2 py-0.5 rounded hover:bg-surface/30">
                  <span className="text-[9px] text-muted-foreground tabular-nums">
                    {new Date(ev.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span className="text-accent font-medium">{ev.agent}</span>
                  <span className="text-muted-foreground">{ev.kind}</span>
                  <span className="text-muted-foreground/70 truncate">{JSON.stringify(ev.data).slice(0, 80)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Blackboard */}
        {blackboard.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground/60 mb-1.5">Blackboard</div>
            <div className="space-y-1">
              {blackboard.map((entry, i) => (
                <div key={i} className="rounded border border-border/50 px-2 py-1.5 text-xs">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-[9px] text-muted-foreground tabular-nums">
                      {new Date(entry.ts * 1000).toLocaleTimeString()}
                    </span>
                    <span className="text-accent font-medium">{entry.agent}</span>
                    <span className="text-muted-foreground">{entry.key}</span>
                  </div>
                  <div className="text-foreground/80 whitespace-pre-wrap break-words">{entry.value.slice(0, 500)}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {!loading && !state && !error && (
          <div className="text-center text-sm text-muted-foreground py-12">
            No memory data. The memory layer initializes when an agent runs in a workspace.
          </div>
        )}
      </div>
    </div>
  );
}
