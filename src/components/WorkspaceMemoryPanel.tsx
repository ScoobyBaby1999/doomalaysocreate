/**
 * WorkspaceMemoryPanel — slide-in panel (right on desktop, bottom sheet on
 * mobile) showing the .pied sanity log for ONE workspace.
 *
 * Read + write capability:
 *  - Reads via AgentClient.getMemory(workspaceId) — which now correctly
 *    attaches the X-JWT header (the original bug: missing X-JWT made the
 *    backend return "workspace not found").
 *  - Writes via AgentClient.postMemory(workspaceId, { kind, agent, data }).
 *
 * The global memory layer is just a container/index; per-workspace detail
 * lives here.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { AgentClient } from "../api/agent";
import type { Settings } from "../api/panel";
import type { Workspace } from "../api/github";

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

interface Props {
  open: boolean;
  workspace: Workspace | null;
  settings: Settings;
  onClose: () => void;
}

export function WorkspaceMemoryPanel({ open, workspace, settings, onClose }: Props) {
  const client = useMemo(() => new AgentClient(settings), [settings]);
  const [state, setState] = useState<MemoryState | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [blackboard, setBlackboard] = useState<BlackboardEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [writing, setWriting] = useState(false);

  // Quick "add note" mini-form
  const [noteText, setNoteText] = useState("");

  const fetchMemory = useCallback(async () => {
    if (!workspace) return;
    setLoading(true);
    setError("");
    try {
      const r = await client.getMemory(workspace.id);
      setState(r.state || null);
      setLog(r.recent_log || []);
      setBlackboard(r.blackboard || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load memory");
      setState(null);
      setLog([]);
      setBlackboard([]);
    } finally {
      setLoading(false);
    }
  }, [client, workspace]);

  useEffect(() => {
    if (open && workspace) fetchMemory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspace?.id]);

  async function handleAddNote() {
    if (!workspace || !noteText.trim()) return;
    setWriting(true);
    setError("");
    try {
      await client.postMemory(workspace.id, {
        kind: "note",
        agent: "user",
        data: { note: noteText.trim() },
      });
      setNoteText("");
      await fetchMemory();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to write memory");
    } finally {
      setWriting(false);
    }
  }

  async function handleSetGoal() {
    if (!workspace || !state?.goal) return;
    setWriting(true);
    try {
      await client.postMemory(workspace.id, {
        kind: "goal",
        agent: "user",
        goal: state.goal,
      });
      await fetchMemory();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to set goal");
    } finally {
      setWriting(false);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70]">
      {/* backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* panel — bottom sheet on mobile, right slide-in on desktop */}
      <div
        className="absolute sm:right-0 sm:top-0 sm:bottom-0 sm:w-[420px] sm:max-w-[90vw]
                   left-0 right-0 bottom-0 sm:rounded-none rounded-t-2xl
                   bg-bg border-t sm:border-t-0 sm:border-l border-border
                   flex flex-col max-h-[88vh] sm:max-h-full slide-up
                   sm:slide-up-desktop"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {/* drag handle (mobile) */}
        <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* header */}
        <div className="flex items-center gap-2 px-4 h-12 border-b border-border shrink-0">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
            <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
            <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
          </svg>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-text truncate">Memory · {workspace?.title || "—"}</div>
            <div className="text-[10px] text-muted truncate">
              {workspace ? `id ${workspace.id.slice(0, 12)}…` : ""}
            </div>
          </div>
          <button
            onClick={fetchMemory}
            disabled={loading || !workspace}
            aria-label="Refresh"
            className="touch-target w-9 h-9 rounded-xl bg-surface2 flex items-center justify-center text-muted hover:text-text disabled:opacity-40"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={loading ? "animate-spin" : ""}>
              <polyline points="23 4 23 10 17 10"/>
              <polyline points="1 20 1 14 7 14"/>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
            </svg>
          </button>
          <button
            onClick={onClose}
            aria-label="Close"
            className="touch-target w-9 h-9 rounded-xl bg-surface2 flex items-center justify-center text-muted hover:text-text"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        {error && (
          <div className="px-3 py-2 text-xs text-rose-300 border-b border-rose-500/40 bg-rose-500/10">
            {error}
          </div>
        )}

        {/* body */}
        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {loading && !state ? (
            <div className="text-center text-muted text-sm py-12 flex flex-col items-center gap-2">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
                <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
                <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
                <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
              </svg>
              Loading memory…
            </div>
          ) : null}

          {/* Goal */}
          {state && (
            <div className="rounded-xl border border-border bg-surface/40 p-3">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">Goal</div>
              <textarea
                value={state.goal || ""}
                onChange={(e) => setState({ ...state, goal: e.target.value })}
                placeholder="What is the agent trying to achieve?"
                rows={2}
                className="w-full bg-transparent text-sm text-foreground outline-none resize-none border-0 p-0"
              />
              <div className="flex justify-end mt-1">
                <button
                  onClick={handleSetGoal}
                  disabled={writing || !state.goal}
                  className="px-2 py-0.5 rounded-md bg-accent/15 text-accent text-[10px] disabled:opacity-40"
                >
                  save goal
                </button>
              </div>
            </div>
          )}

          {/* Tasks */}
          {state && (state.pending_tasks?.length || state.completed_tasks?.length) ? (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">Tasks</div>
              <div className="space-y-1">
                {state.pending_tasks?.map((t, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs px-2 py-1.5 rounded-xl border border-border/60 bg-surface/30">
                    <span className="size-1.5 rounded-full bg-amber-400" />
                    <span className="flex-1 truncate">{t.task}</span>
                    <span className="text-[9px] text-muted-foreground">{t.assigned_to}</span>
                  </div>
                ))}
                {state.completed_tasks?.slice(-5).map((t, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs px-2 py-1.5 rounded-xl border border-border/30 opacity-60">
                    <span className="size-1.5 rounded-full bg-emerald-400" />
                    <span className="flex-1 truncate">{t.task}</span>
                    <span className="text-[9px] text-muted-foreground">{t.agent}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {/* Blackboard */}
          {blackboard.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">Blackboard</div>
              <div className="space-y-1">
                {blackboard.map((entry, i) => (
                  <div key={i} className="rounded-xl border border-border/60 bg-surface/30 px-2 py-1.5 text-xs">
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

          {/* Recent events */}
          {log.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">Recent events</div>
              <div className="space-y-0.5 max-h-48 overflow-y-auto rounded-xl border border-border/40 bg-surface/20 p-1">
                {log.map((ev, i) => (
                  <div key={i} className="flex items-center gap-2 text-[11px] px-2 py-0.5 rounded hover:bg-surface/50">
                    <span className="text-[9px] text-muted-foreground tabular-nums">
                      {new Date(ev.ts * 1000).toLocaleTimeString()}
                    </span>
                    <span className="text-accent font-medium">{ev.agent}</span>
                    <span className="text-muted-foreground">{ev.kind}</span>
                    <span className="text-muted-foreground/70 truncate">
                      {JSON.stringify(ev.data).slice(0, 80)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Add note (write) */}
          <div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">Add note</div>
            <div className="flex gap-2">
              <input
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleAddNote(); } }}
                placeholder="e.g. Remember to use Tailwind v4"
                className="flex-1 bg-surface border border-border rounded-xl px-3 py-2 text-sm outline-none focus:border-accent"
              />
              <button
                onClick={handleAddNote}
                disabled={writing || !noteText.trim()}
                className="px-3 h-9 rounded-xl bg-accent text-white text-sm disabled:opacity-40 flex items-center"
              >
                {writing ? "…" : "Add"}
              </button>
            </div>
          </div>

          {!loading && !state && !error && (
            <div className="text-center text-sm text-muted-foreground py-12">
              No memory yet. The .pied log initializes the first time an agent runs in this workspace.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
