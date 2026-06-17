/**
 * ConsciousScreen — mobile-first multi-agent constellation UI.
 *
 * Agents are circles on an auto-layouted canvas. The orchestrator sits at
 * center-top; sub-agents radiate below. Lines connect each agent to the
 * orchestrator and pulse when agents communicate. Tapping a circle opens a
 * bottom sheet with the agent's chat / invoke interface. A floating "+" adds
 * a GLM 5.2 agent. A drawer icon opens the output list.
 *
 * Design goals:
 * - Mobile-first: touch targets ≥56px, bottom sheet (native pattern), no drag
 * - Minimal clicks: 1 tap to open agent, 1 tap to invoke, 1 tap to add agent
 * - Visual communication: lines pulse on drawer events / proposals / merges
 * - Works on any screen size: radial layout auto-scales
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Brain, Plus, X, Send, GitMerge, Inbox, FolderTree,
  Activity, Loader2, CheckCircle2, AlertCircle, Clock,
} from "lucide-react";
import { ConsciousClient, ApiError } from "../api/conscious";
import type { Settings } from "../api/panel";
import type { Agent, DrawerEntry, Conscious, Proposal } from "../api/conscious";

// ---------------------------------------------------------------------------
// layout — radial auto-positioning (no drag needed)
// ---------------------------------------------------------------------------

interface NodePos { x: number; y: number; }

function layoutAgents(agents: Agent[]): Record<string, NodePos> {
  const orch = agents.find((a) => a.isOrchestrator === 1);
  const subs = agents.filter((a) => a.isOrchestrator !== 1);
  const pos: Record<string, NodePos> = {};

  if (orch) {
    pos[orch.id] = { x: 50, y: 18 }; // center-top, percentage
  }

  const radius = Math.min(35, Math.max(22, subs.length * 8));
  subs.forEach((a, i) => {
    const angle = (i / Math.max(subs.length, 1)) * Math.PI - Math.PI / 2;
    // spread below the orchestrator in a semicircle
    pos[a.id] = {
      x: 50 + Math.sin(angle) * radius,
      y: 18 + Math.cos(angle) * radius * 1.3 + 10,
    };
  });

  return pos;
}

// ---------------------------------------------------------------------------
// status helpers
// ---------------------------------------------------------------------------

const STATUS_COLORS: Record<string, string> = {
  idle: "#8b95a3",
  running: "#5b8cff",
  waiting: "#f59e0b",
  done: "#10b981",
  failed: "#ef4444",
  pending: "#f59e0b",
  claimed: "#5b8cff",
  in_progress: "#5b8cff",
};

const STATUS_ICON: Record<string, typeof Brain> = {
  idle: Clock,
  running: Loader2,
  waiting: Clock,
  done: CheckCircle2,
  failed: AlertCircle,
  pending: Clock,
};

// ---------------------------------------------------------------------------
// main screen
// ---------------------------------------------------------------------------

export function ConsciousScreen({ settings, workspaceId }: {
  settings: Settings; workspaceId?: string;
}) {
  const client = useRef(new ConsciousClient(settings));
  const wsId = workspaceId || "demo-workspace";

  const [conscious, setConscious] = useState<Conscious | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [drawer, setDrawer] = useState<DrawerEntry[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [pulseLines, setPulseLines] = useState<Record<string, number>>({});
  const [showDrawer, setShowDrawer] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // --- create or fetch conscious ---
  const init = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await client.current.listConscious(wsId);
      if (list.conscious.length > 0) {
        const c = list.conscious[0];
        const detail = await client.current.getConscious(c.id);
        setConscious(detail.conscious);
        setAgents(detail.agents);
        await refreshData(c.id);
      } else {
        // auto-create a conscious with 1 orchestrator
        await createConscious();
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [wsId]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { init(); }, [init]);

  const createConscious = async () => {
    setCreating(true);
    try {
      const r = await client.current.createConscious({
        workspace_id: wsId,
        title: "Conscious Workspace",
        goal: "Multi-agent collaboration with GLM 5.2",
      });
      setConscious(r.conscious);
      setAgents(r.agents);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  const refreshData = async (cid: string) => {
    try {
      const [d, p] = await Promise.all([
        client.current.listDrawer(cid, { limit: 20 }),
        client.current.listProposals(cid, "pending"),
      ]);
      setDrawer(d.entries);
      setProposals(p.proposals);
    } catch {}
  };

  // --- add a GLM agent ---
  const addAgent = async () => {
    if (!conscious) return;
    try {
      const r = await client.current.spawnAgent(conscious.id, {
        role: "ai-engineer",
        model: "glm-5.2 (free)",
        tier: "zai",
      });
      setAgents((prev) => [...prev, r.agent]);
      // pulse the connection line
      const orch = agents.find((a) => a.isOrchestrator === 1);
      if (orch) {
        setPulseLines((prev) => ({ ...prev, [`${orch.id}-${r.agent.id}`]: Date.now() }));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  // --- invoke an agent ---
  const invokeAgent = async (agent: Agent, task: string) => {
    if (!conscious) return;
    const orch = agents.find((a) => a.isOrchestrator === 1);
    if (!orch) return;
    try {
      // pulse the line
      setPulseLines((prev) => ({ ...prev, [`${orch.id}-${agent.id}`]: Date.now() }));
      const r = await client.current.invokeAgent(conscious.id, {
        from_agent_id: orch.id,
        to_agent_id: agent.id,
        kind: "invoke",
        task,
      });
      // pulse the return line
      setPulseLines((prev) => ({ ...prev, [`${agent.id}-${orch.id}`]: Date.now() }));
      await refreshData(conscious.id);
      return r;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  // --- merge an agent's branch ---
  const mergeAgent = async (agent: Agent) => {
    if (!conscious) return;
    const orch = agents.find((a) => a.isOrchestrator === 1);
    if (!orch) return;
    try {
      await client.current.mergeAgentBranch(conscious.id, agent.id, orch.id);
      await refreshData(conscious.id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const positions = layoutAgents(agents);
  const orch = agents.find((a) => a.isOrchestrator === 1);
  const subs = agents.filter((a) => a.isOrchestrator !== 1);

  return (
    <div className="flex flex-col h-full bg-bg overflow-hidden relative">
      {/* --- header --- */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-accent/20 flex items-center justify-center">
            <Brain className="w-4 h-4 text-accent" />
          </div>
          <div>
            <div className="text-sm font-semibold text-text">
              {conscious?.title || "Conscious"}
            </div>
            <div className="text-[10px] text-muted">
              {agents.length} agent{agents.length !== 1 ? "s" : ""} · GLM 5.2
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowDrawer(true)}
            className="w-9 h-9 rounded-lg bg-surface2 flex items-center justify-center text-muted hover:text-text transition-colors relative"
            title="View outputs"
          >
            <Inbox className="w-4 h-4" />
            {drawer.length > 0 && (
              <span className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-accent text-[9px] text-white flex items-center justify-center">
                {drawer.length}
              </span>
            )}
          </button>
        </div>
      </div>

      {/* --- error bar --- */}
      {error && (
        <div className="px-4 py-2 bg-rose-500/10 border-b border-rose-500/30 text-rose-300 text-xs flex items-center justify-between">
          <span className="truncate">{error}</span>
          <button onClick={() => setError(null)} className="shrink-0 ml-2">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* --- constellation canvas --- */}
      <div className="flex-1 relative overflow-hidden">
        {loading || creating ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <Loader2 className="w-6 h-6 text-accent animate-spin" />
          </div>
        ) : (
          <>
            {/* SVG connection lines */}
            <svg className="absolute inset-0 w-full h-full pointer-events-none" style={{ zIndex: 1 }}>
              {orch && subs.map((sub) => {
                const op = positions[orch.id];
                const sp = positions[sub.id];
                if (!op || !sp) return null;
                const key = `${orch.id}-${sub.id}`;
                const pulseTime = pulseLines[key];
                const pulsing = pulseTime && Date.now() - pulseTime < 3000;
                return (
                  <line
                    key={key}
                    x1={`${op.x}%`} y1={`${op.y}%`}
                    x2={`${sp.x}%`} y2={`${sp.y}%`}
                    stroke={pulsing ? "#5b8cff" : "#262d36"}
                    strokeWidth={pulsing ? 2 : 1}
                    className={pulsing ? "animate-pulse" : ""}
                    style={{ transition: "stroke 0.3s, stroke-width 0.3s" }}
                  />
                );
              })}
            </svg>

            {/* Agent nodes */}
            <div className="absolute inset-0" style={{ zIndex: 2 }}>
              {agents.map((agent) => {
                const pos = positions[agent.id];
                if (!pos) return null;
                return (
                  <AgentNode
                    key={agent.id}
                    agent={agent}
                    pos={pos}
                    drawerCount={drawer.filter((d) => d.toAgentId === agent.id).length}
                    pendingProps={proposals.filter((p) => p.proposerAgentId === agent.id && p.status === "pending").length}
                    onTap={() => setSelectedAgent(agent)}
                  />
                );
              })}
            </div>

            {/* Empty state hint */}
            {agents.length <= 1 && (
              <div className="absolute bottom-24 left-1/2 -translate-x-1/2 text-center text-muted text-xs px-8">
                Tap <span className="text-accent font-medium">+</span> to add a GLM 5.2 agent.
                Each agent works in its own git worktree.
              </div>
            )}
          </>
        )}

        {/* Floating add button */}
        <button
          onClick={addAgent}
          disabled={!conscious || loading}
          className="absolute bottom-6 right-6 w-14 h-14 rounded-full bg-accent text-white flex items-center justify-center shadow-lg shadow-accent/30 hover:scale-105 active:scale-95 transition-transform disabled:opacity-50"
          style={{ zIndex: 10 }}
          title="Add GLM 5.2 agent"
        >
          <Plus className="w-6 h-6" />
        </button>
      </div>

      {/* --- agent bottom sheet --- */}
      {selectedAgent && conscious && (
        <AgentSheet
          agent={selectedAgent}
          drawer={drawer.filter((d) => d.toAgentId === selectedAgent.id)}
          onClose={() => setSelectedAgent(null)}
          onInvoke={(task) => invokeAgent(selectedAgent, task)}
          onMerge={() => mergeAgent(selectedAgent)}
        />
      )}

      {/* --- drawer panel --- */}
      {showDrawer && (
        <DrawerPanel
          entries={drawer}
          agents={agents}
          onClose={() => setShowDrawer(false)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentNode — a single agent circle
// ---------------------------------------------------------------------------

function AgentNode({ agent, pos, drawerCount, pendingProps, onTap }: {
  agent: Agent;
  pos: NodePos;
  drawerCount: number;
  pendingProps: number;
  onTap: () => void;
}) {
  const isOrch = agent.isOrchestrator === 1;
  const color = STATUS_COLORS[agent.status] || "#8b95a3";
  const Icon = isOrch ? Brain : (STATUS_ICON[agent.status] || Brain);
  const size = isOrch ? 72 : 56;

  return (
    <button
      onClick={onTap}
      className="absolute flex flex-col items-center gap-1 group"
      style={{
        left: `${pos.x}%`,
        top: `${pos.y}%`,
        transform: "translate(-50%, -50%)",
        zIndex: 3,
      }}
    >
      {/* circle */}
      <div
        className="rounded-full flex items-center justify-center border-2 transition-all group-hover:scale-110 group-active:scale-95"
        style={{
          width: size,
          height: size,
          borderColor: color,
          background: isOrch ? "rgba(91,140,255,0.15)" : "rgba(20,24,29,0.9)",
          boxShadow: agent.status === "running" ? `0 0 12px ${color}40` : "none",
        }}
      >
        <Icon
          className={agent.status === "running" ? "animate-spin" : ""}
          style={{ width: isOrch ? 28 : 22, height: isOrch ? 28 : 22, color }}
        />
        {/* notification badges */}
        {drawerCount > 0 && (
          <span className="absolute -top-1 -right-1 w-5 h-5 rounded-full bg-emerald-500 text-[10px] text-white flex items-center justify-center font-bold">
            {drawerCount}
          </span>
        )}
        {pendingProps > 0 && (
          <span className="absolute -bottom-1 -right-1 w-5 h-5 rounded-full bg-amber-500 text-[10px] text-white flex items-center justify-center font-bold">
            {pendingProps}
          </span>
        )}
      </div>
      {/* label */}
      <div className="text-center max-w-[80px]">
        <div className="text-[10px] font-medium text-text truncate">
          {agent.role}
        </div>
        <div className="text-[9px] text-muted truncate">
          {agent.model.split(" ")[0]}
        </div>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// AgentSheet — bottom sheet for agent interaction
// ---------------------------------------------------------------------------

function AgentSheet({ agent, drawer, onClose, onInvoke, onMerge }: {
  agent: Agent;
  drawer: DrawerEntry[];
  onClose: () => void;
  onInvoke: (task: string) => Promise<unknown>;
  onMerge: () => Promise<void>;
}) {
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [merged, setMerged] = useState(false);
  const isOrch = agent.isOrchestrator === 1;
  const color = STATUS_COLORS[agent.status] || "#8b95a3";

  const handleInvoke = async () => {
    if (!task.trim()) return;
    setBusy(true);
    try {
      await onInvoke(task);
      setTask("");
    } finally {
      setBusy(false);
    }
  };

  const handleMerge = async () => {
    setBusy(true);
    try {
      await onMerge();
      setMerged(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {/* backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
      />
      {/* sheet */}
      <div
        className="fixed bottom-0 left-0 right-0 bg-surface rounded-t-2xl border-t border-border z-50 flex flex-col"
        style={{ maxHeight: "85vh", paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {/* drag handle */}
        <div className="flex justify-center pt-2 pb-1">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* header */}
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center border-2"
              style={{ borderColor: color, background: isOrch ? "rgba(91,140,255,0.15)" : "rgba(20,24,29,0.9)" }}
            >
              {isOrch ? (
                <Brain className="w-4 h-4" style={{ color }} />
              ) : (
                <Activity className="w-4 h-4" style={{ color }} />
              )}
            </div>
            <div>
              <div className="text-sm font-semibold text-text">
                {agent.role} {isOrch && <span className="text-[10px] text-accent">(orchestrator)</span>}
              </div>
              <div className="text-[10px] text-muted">
                {agent.model} · {agent.tier} · {agent.status}
              </div>
            </div>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg bg-surface2 flex items-center justify-center text-muted">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* content */}
        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
          {/* agent info */}
          {agent.worktreePath && (
            <div className="text-[10px] text-muted bg-surface2 rounded-lg p-2">
              <div>branch: <code className="text-text">{agent.branch}</code></div>
              <div>worktree: <span className="text-text/70 truncate">{agent.worktreePath.split("/").slice(-2).join("/")}</span></div>
            </div>
          )}

          {/* invoke form (non-orchestrator only) */}
          {!isOrch && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-text">Give this agent a task:</div>
              <textarea
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder="e.g. Write a Python function that checks if a number is prime"
                rows={2}
                className="w-full rounded-xl bg-surface2 border border-border focus:border-accent text-sm text-text p-3 resize-none outline-none"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleInvoke(); }
                }}
              />
              <div className="flex gap-2">
                <button
                  onClick={handleInvoke}
                  disabled={busy || !task.trim()}
                  className="flex-1 h-10 rounded-xl bg-accent text-white flex items-center justify-center gap-2 text-sm font-medium disabled:opacity-50"
                >
                  {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  {busy ? "Working…" : "Invoke"}
                </button>
                <button
                  onClick={handleMerge}
                  disabled={busy || merged || drawer.length === 0}
                  className="h-10 px-4 rounded-xl bg-surface2 border border-border text-text flex items-center justify-center gap-1 text-sm disabled:opacity-50"
                >
                  <GitMerge className="w-4 h-4" />
                  Merge
                </button>
              </div>
            </div>
          )}

          {/* drawer entries */}
          {drawer.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-text">Outputs ({drawer.length}):</div>
              {drawer.slice().reverse().map((d) => (
                <div key={d.id} className="rounded-lg bg-surface2 border border-border p-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-medium text-accent">{d.kind}</span>
                    <span
                      className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: `${STATUS_COLORS[d.status] || "#8b95a3"}20`, color: STATUS_COLORS[d.status] || "#8b95a3" }}
                    >
                      {d.status}
                    </span>
                  </div>
                  <div className="text-xs text-text/80 mb-1">{d.task}</div>
                  {d.result && (
                    <pre className="text-[11px] text-muted whitespace-pre-wrap max-h-32 overflow-y-auto mt-1 font-mono">
                      {d.result.slice(0, 500)}
                      {d.result.length > 500 ? "…" : ""}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}

          {drawer.length === 0 && !isOrch && (
            <div className="text-center text-muted text-xs py-4">
              No outputs yet. Invoke this agent with a task to see results here.
            </div>
          )}

          {isOrch && (
            <div className="text-center text-muted text-xs py-4">
              The orchestrator manages the brain. Use other agents to do work,
              then merge their branches here.
            </div>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// DrawerPanel — slide-over showing all outputs
// ---------------------------------------------------------------------------

function DrawerPanel({ entries, agents, onClose }: {
  entries: DrawerEntry[];
  agents: Agent[];
  onClose: () => void;
}) {
  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-40" onClick={onClose} />
      <div className="fixed top-0 right-0 bottom-0 w-full max-w-sm bg-surface border-l border-border z-50 flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex items-center gap-2">
            <FolderTree className="w-4 h-4 text-accent" />
            <span className="text-sm font-semibold text-text">All Outputs</span>
            <span className="text-[10px] text-muted">({entries.length})</span>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg bg-surface2 flex items-center justify-center text-muted">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {entries.length === 0 ? (
            <div className="text-center text-muted text-sm py-8">No outputs yet.</div>
          ) : (
            entries.slice().reverse().map((d) => {
              const fromAgent = agents.find((a) => a.id === d.fromAgentId);
              const toAgent = agents.find((a) => a.id === d.toAgentId);
              return (
                <div key={d.id} className="rounded-lg bg-surface2 border border-border p-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-accent font-medium">{d.kind}</span>
                    <span className="text-[10px] text-muted">
                      {fromAgent?.role || "?"} → {toAgent?.role || "?"}
                    </span>
                  </div>
                  <div className="text-xs text-text/80 mb-1">{d.task}</div>
                  <div className="flex items-center gap-2 mt-1">
                    <span
                      className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: `${STATUS_COLORS[d.status] || "#8b95a3"}20`, color: STATUS_COLORS[d.status] || "#8b95a3" }}
                    >
                      {d.status}
                    </span>
                  </div>
                  {d.result && (
                    <pre className="text-[10px] text-muted whitespace-pre-wrap max-h-24 overflow-y-auto mt-2 font-mono">
                      {d.result.slice(0, 300)}
                      {d.result.length > 300 ? "…" : ""}
                    </pre>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </>
  );
}
