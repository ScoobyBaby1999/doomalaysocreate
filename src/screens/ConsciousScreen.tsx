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
 * - No external icon dependency — all icons are inline SVG
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ConsciousClient, ApiError } from "../api/conscious";
import type { Settings } from "../api/panel";
import type { Agent, DrawerEntry, Conscious, Proposal } from "../api/conscious";
import type { Workspace } from "../api/github";

// ---------------------------------------------------------------------------
// inline SVG icons (no dependency)
// ---------------------------------------------------------------------------

const Icon = {
  Brain: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
      <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
    </svg>
  ),
  Plus: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
  ),
  X: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  ),
  Send: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
    </svg>
  ),
  Merge: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>
    </svg>
  ),
  Inbox: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
    </svg>
  ),
  Folder: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
    </svg>
  ),
  Activity: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
    </svg>
  ),
  Loader: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
      <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
      <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
      <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
      <line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/>
    </svg>
  ),
  CheckCircle: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
    </svg>
  ),
  AlertCircle: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  ),
  Clock: ({ size = 16, color = "currentColor" }: { size?: number; color?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
    </svg>
  ),
};

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
  idle: "#8b95a3", running: "#5b8cff", waiting: "#f59e0b",
  done: "#10b981", failed: "#ef4444", pending: "#f59e0b",
  claimed: "#5b8cff", in_progress: "#5b8cff",
};

function StatusIcon({ status, size, color }: { status: string; size: number; color: string }) {
  if (status === "running") return <Icon.Loader size={size} color={color} />;
  if (status === "done") return <Icon.CheckCircle size={size} color={color} />;
  if (status === "failed") return <Icon.AlertCircle size={size} color={color} />;
  return <Icon.Clock size={size} color={color} />;
}

// ---------------------------------------------------------------------------
// main screen
// ---------------------------------------------------------------------------

export function ConsciousScreen({ settings, workspaceId }: {
  settings: Settings; workspaceId?: string;
}) {
  const client = useRef(new ConsciousClient(settings));

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWsId, setSelectedWsId] = useState<string | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);

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

  const wsId = selectedWsId || workspaceId || "demo-workspace";

  const init = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const list = await client.current.listConscious(wsId);
      if (list.conscious.length > 0) {
        const c = list.conscious[0];
        const detail = await client.current.getConscious(c.id);
        setConscious(detail.conscious); setAgents(detail.agents);
        await refreshData(c.id);
      } else { await createConscious(); }
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [wsId]);

  useEffect(() => {
    client.current.listWorkspaces().then((r) => {
      setWorkspaces(r.workspaces);
      if (!selectedWsId && r.workspaces.length > 0) {
        setSelectedWsId(r.workspaces[0].id);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => { init(); }, [init]);

  const createConscious = async () => {
    setCreating(true);
    try {
      const r = await client.current.createConscious({
        workspace_id: wsId, title: "Conscious Workspace",
        goal: "Multi-agent collaboration with GLM 5.2",
      });
      setConscious(r.conscious); setAgents(r.agents);
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
    finally { setCreating(false); }
  };

  const refreshData = async (cid: string) => {
    try {
      const [d, p] = await Promise.all([
        client.current.listDrawer(cid, { limit: 20 }),
        client.current.listProposals(cid, "pending"),
      ]);
      setDrawer(d.entries); setProposals(p.proposals);
    } catch {}
  };

  const addAgent = async () => {
    if (!conscious) return;
    try {
      const r = await client.current.spawnAgent(conscious.id, {
        role: "ai-engineer", model: "glm-5.2 (free)", tier: "zai",
      });
      setAgents((prev) => [...prev, r.agent]);
      const orch = agents.find((a) => a.isOrchestrator === 1);
      if (orch) setPulseLines((prev) => ({ ...prev, [`${orch.id}-${r.agent.id}`]: Date.now() }));
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
  };

  const invokeAgent = async (agent: Agent, task: string) => {
    if (!conscious) return;
    const orch = agents.find((a) => a.isOrchestrator === 1);
    if (!orch) return;
    try {
      setPulseLines((prev) => ({ ...prev, [`${orch.id}-${agent.id}`]: Date.now() }));
      const r = await client.current.invokeAgent(conscious.id, {
        from_agent_id: orch.id, to_agent_id: agent.id, kind: "invoke", task,
      });
      setPulseLines((prev) => ({ ...prev, [`${agent.id}-${orch.id}`]: Date.now() }));
      await refreshData(conscious.id);
      return r;
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
  };

  const mergeAgent = async (agent: Agent) => {
    if (!conscious) return;
    const orch = agents.find((a) => a.isOrchestrator === 1);
    if (!orch) return;
    try {
      await client.current.mergeAgentBranch(conscious.id, agent.id, orch.id);
      await refreshData(conscious.id);
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
  };

  const positions = layoutAgents(agents);
  const orch = agents.find((a) => a.isOrchestrator === 1);
  const subs = agents.filter((a) => a.isOrchestrator !== 1);

  return (
    <div className="flex flex-col h-full bg-bg overflow-hidden relative">
      {/* header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-accent/20 flex items-center justify-center shrink-0">
            <Icon.Brain size={16} color="#5b8cff" />
          </div>
          <div className="relative min-w-0">
            <div className="flex items-center gap-1">
              <button onClick={() => setWorkspaceOpen(!workspaceOpen)}
                className="flex items-center gap-1 text-sm font-semibold text-text truncate max-w-[160px] hover:text-accent transition-colors">
                <span className="truncate">{(workspaces.find((w) => w.id === selectedWsId)?.title) || selectedWsId || "Select workspace"}</span>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" className={`transition-transform ${workspaceOpen ? "rotate-180" : ""}`}>
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
              </button>
            </div>
            <div className="text-[10px] text-muted truncate">{agents.length} agent{agents.length !== 1 ? "s" : ""} · GLM 5.2</div>
            {workspaceOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setWorkspaceOpen(false)} />
                <div className="absolute top-full left-0 mt-1 w-56 bg-surface2 border border-border rounded-xl shadow-xl z-40 max-h-60 overflow-y-auto">
                  {workspaces.length === 0 ? (
                    <div className="px-3 py-3 text-xs text-muted text-center">No workspaces found</div>
                  ) : workspaces.map((w) => (
                    <button key={w.id} onClick={() => { setSelectedWsId(w.id); setWorkspaceOpen(false); }}
                      className={`w-full text-left px-3 py-2.5 text-xs flex items-center gap-2 hover:bg-surface transition-colors ${w.id === selectedWsId ? "text-accent bg-accent/10" : "text-text"}`}>
                      <Icon.Folder size={12} color={w.id === selectedWsId ? "#5b8cff" : "#8b95a3"} />
                      <span className="truncate">{w.title}</span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>
        <button onClick={() => setShowDrawer(true)}
          className="w-9 h-9 rounded-lg bg-surface2 flex items-center justify-center text-muted hover:text-text transition-colors relative"
          title="View outputs">
          <Icon.Inbox size={16} />
          {drawer.length > 0 && (
            <span className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-accent text-[9px] text-white flex items-center justify-center">{drawer.length}</span>
          )}
        </button>
      </div>

      {/* error bar */}
      {error && (
        <div className="px-4 py-2 bg-rose-500/10 border-b border-rose-500/30 text-rose-300 text-xs flex items-center justify-between">
          <span className="truncate">{error}</span>
          <button onClick={() => setError(null)} className="shrink-0 ml-2"><Icon.X size={12} /></button>
        </div>
      )}

      {/* constellation canvas */}
      <div className="flex-1 relative overflow-hidden">
        {loading || creating ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <Icon.Loader size={24} color="#5b8cff" />
          </div>
        ) : (
          <>
            <svg className="absolute inset-0 w-full h-full pointer-events-none" style={{ zIndex: 1 }}>
              {orch && subs.map((sub) => {
                const op = positions[orch.id]; const sp = positions[sub.id];
                if (!op || !sp) return null;
                const key = `${orch.id}-${sub.id}`;
                const pulseTime = pulseLines[key];
                const pulsing = pulseTime && Date.now() - pulseTime < 3000;
                return (
                  <line key={key} x1={`${op.x}%`} y1={`${op.y}%`} x2={`${sp.x}%`} y2={`${sp.y}%`}
                    stroke={pulsing ? "#5b8cff" : "#262d36"} strokeWidth={pulsing ? 2 : 1}
                    className={pulsing ? "animate-pulse" : ""}
                    style={{ transition: "stroke 0.3s, stroke-width 0.3s" }} />
                );
              })}
            </svg>

            <div className="absolute inset-0" style={{ zIndex: 2 }}>
              {agents.map((agent) => {
                const pos = positions[agent.id]; if (!pos) return null;
                return (
                  <AgentNode key={agent.id} agent={agent} pos={pos}
                    drawerCount={drawer.filter((d) => d.toAgentId === agent.id).length}
                    pendingProps={proposals.filter((p) => p.proposerAgentId === agent.id && p.status === "pending").length}
                    onTap={() => setSelectedAgent(agent)} />
                );
              })}
            </div>

            {agents.length <= 1 && (
              <div className="absolute bottom-24 left-1/2 -translate-x-1/2 text-center text-muted text-xs px-8">
                Tap <span className="text-accent font-medium">+</span> to add a GLM 5.2 agent.
                Each agent works in its own git worktree.
              </div>
            )}
          </>
        )}

        <button onClick={addAgent} disabled={!conscious || loading}
          className="absolute bottom-6 right-6 w-14 h-14 rounded-full bg-accent text-white flex items-center justify-center shadow-lg shadow-accent/30 hover:scale-105 active:scale-95 transition-transform disabled:opacity-50"
          style={{ zIndex: 10 }} title="Add GLM 5.2 agent">
          <Icon.Plus size={24} color="white" />
        </button>
      </div>

      {selectedAgent && conscious && (
        <AgentSheet agent={selectedAgent}
          drawer={drawer.filter((d) => d.toAgentId === selectedAgent.id)}
          onClose={() => setSelectedAgent(null)}
          onInvoke={(task) => invokeAgent(selectedAgent, task)}
          onMerge={() => mergeAgent(selectedAgent)} />
      )}

      {showDrawer && (
        <DrawerPanel entries={drawer} agents={agents} onClose={() => setShowDrawer(false)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentNode
// ---------------------------------------------------------------------------

function AgentNode({ agent, pos, drawerCount, pendingProps, onTap }: {
  agent: Agent; pos: NodePos; drawerCount: number; pendingProps: number; onTap: () => void;
}) {
  const isOrch = agent.isOrchestrator === 1;
  const color = STATUS_COLORS[agent.status] || "#8b95a3";
  const size = isOrch ? 72 : 56;

  return (
    <button onClick={onTap}
      className="absolute flex flex-col items-center gap-1 group"
      style={{ left: `${pos.x}%`, top: `${pos.y}%`, transform: "translate(-50%, -50%)", zIndex: 3 }}>
      <div className="rounded-full flex items-center justify-center border-2 transition-all group-hover:scale-110 group-active:scale-95"
        style={{ width: size, height: size, borderColor: color,
          background: isOrch ? "rgba(91,140,255,0.15)" : "rgba(20,24,29,0.9)",
          boxShadow: agent.status === "running" ? `0 0 12px ${color}40` : "none" }}>
        {isOrch ? <Icon.Brain size={28} color={color} /> : <StatusIcon status={agent.status} size={22} color={color} />}
        {drawerCount > 0 && (
          <span className="absolute -top-1 -right-1 w-5 h-5 rounded-full bg-emerald-500 text-[10px] text-white flex items-center justify-center font-bold">{drawerCount}</span>
        )}
        {pendingProps > 0 && (
          <span className="absolute -bottom-1 -right-1 w-5 h-5 rounded-full bg-amber-500 text-[10px] text-white flex items-center justify-center font-bold">{pendingProps}</span>
        )}
      </div>
      <div className="text-center max-w-[80px]">
        <div className="text-[10px] font-medium text-text truncate">{agent.role}</div>
        <div className="text-[9px] text-muted truncate">{agent.model.split(" ")[0]}</div>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// AgentSheet — bottom sheet
// ---------------------------------------------------------------------------

function AgentSheet({ agent, drawer, onClose, onInvoke, onMerge }: {
  agent: Agent; drawer: DrawerEntry[];
  onClose: () => void; onInvoke: (task: string) => Promise<unknown>; onMerge: () => Promise<void>;
}) {
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [merged, setMerged] = useState(false);
  const isOrch = agent.isOrchestrator === 1;
  const color = STATUS_COLORS[agent.status] || "#8b95a3";

  const handleInvoke = async () => {
    if (!task.trim()) return;
    setBusy(true);
    try { await onInvoke(task); setTask(""); } finally { setBusy(false); }
  };

  const handleMerge = async () => {
    setBusy(true);
    try { await onMerge(); setMerged(true); } finally { setBusy(false); }
  };

  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-40" onClick={onClose} />
      <div className="fixed bottom-0 left-0 right-0 bg-surface rounded-t-2xl border-t border-border z-50 flex flex-col"
        style={{ maxHeight: "85vh", paddingBottom: "env(safe-area-inset-bottom)" }}>
        <div className="flex justify-center pt-2 pb-1"><div className="w-10 h-1 rounded-full bg-border" /></div>

        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-full flex items-center justify-center border-2"
              style={{ borderColor: color, background: isOrch ? "rgba(91,140,255,0.15)" : "rgba(20,24,29,0.9)" }}>
              {isOrch ? <Icon.Brain size={16} color={color} /> : <Icon.Activity size={16} color={color} />}
            </div>
            <div>
              <div className="text-sm font-semibold text-text">{agent.role} {isOrch && <span className="text-[10px] text-accent">(orchestrator)</span>}</div>
              <div className="text-[10px] text-muted">{agent.model} · {agent.tier} · {agent.status}</div>
            </div>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg bg-surface2 flex items-center justify-center text-muted"><Icon.X size={16} /></button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
          {agent.worktreePath && (
            <div className="text-[10px] text-muted bg-surface2 rounded-lg p-2">
              <div>branch: <code className="text-text">{agent.branch}</code></div>
              <div>worktree: <span className="text-text/70 truncate">{agent.worktreePath.split("/").slice(-2).join("/")}</span></div>
            </div>
          )}

          {!isOrch && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-text">Give this agent a task:</div>
              <textarea value={task} onChange={(e) => setTask(e.target.value)}
                placeholder="e.g. Write a Python function that checks if a number is prime"
                rows={2}
                className="w-full rounded-xl bg-surface2 border border-border focus:border-accent text-sm text-text p-3 resize-none outline-none"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleInvoke(); } }} />
              <div className="flex gap-2">
                <button onClick={handleInvoke} disabled={busy || !task.trim()}
                  className="flex-1 h-10 rounded-xl bg-accent text-white flex items-center justify-center gap-2 text-sm font-medium disabled:opacity-50">
                  {busy ? <Icon.Loader size={16} color="white" /> : <Icon.Send size={16} color="white" />}
                  {busy ? "Working…" : "Invoke"}
                </button>
                <button onClick={handleMerge} disabled={busy || merged || drawer.length === 0}
                  className="h-10 px-4 rounded-xl bg-surface2 border border-border text-text flex items-center justify-center gap-1 text-sm disabled:opacity-50">
                  <Icon.Merge size={16} /> Merge
                </button>
              </div>
            </div>
          )}

          {drawer.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-text">Outputs ({drawer.length}):</div>
              {drawer.slice().reverse().map((d) => (
                <div key={d.id} className="rounded-lg bg-surface2 border border-border p-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-medium text-accent">{d.kind}</span>
                    <span className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: `${STATUS_COLORS[d.status] || "#8b95a3"}20`, color: STATUS_COLORS[d.status] || "#8b95a3" }}>{d.status}</span>
                  </div>
                  <div className="text-xs text-text/80 mb-1">{d.task}</div>
                  {d.result && (
                    <pre className="text-[11px] text-muted whitespace-pre-wrap max-h-32 overflow-y-auto mt-1 font-mono">
                      {d.result.slice(0, 500)}{d.result.length > 500 ? "…" : ""}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}

          {drawer.length === 0 && !isOrch && (
            <div className="text-center text-muted text-xs py-4">No outputs yet. Invoke this agent with a task to see results here.</div>
          )}
          {isOrch && (
            <div className="text-center text-muted text-xs py-4">The orchestrator manages the brain. Use other agents to do work, then merge their branches here.</div>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// DrawerPanel
// ---------------------------------------------------------------------------

function DrawerPanel({ entries, agents, onClose }: {
  entries: DrawerEntry[]; agents: Agent[]; onClose: () => void;
}) {
  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-40" onClick={onClose} />
      <div className="fixed top-0 right-0 bottom-0 w-full max-w-sm bg-surface border-l border-border z-50 flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex items-center gap-2">
            <Icon.Folder size={16} color="#5b8cff" />
            <span className="text-sm font-semibold text-text">All Outputs</span>
            <span className="text-[10px] text-muted">({entries.length})</span>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg bg-surface2 flex items-center justify-center text-muted"><Icon.X size={16} /></button>
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
                    <span className="text-[10px] text-muted">{fromAgent?.role || "?"} → {toAgent?.role || "?"}</span>
                  </div>
                  <div className="text-xs text-text/80 mb-1">{d.task}</div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: `${STATUS_COLORS[d.status] || "#8b95a3"}20`, color: STATUS_COLORS[d.status] || "#8b95a3" }}>{d.status}</span>
                  </div>
                  {d.result && (
                    <pre className="text-[10px] text-muted whitespace-pre-wrap max-h-24 overflow-y-auto mt-2 font-mono">
                      {d.result.slice(0, 300)}{d.result.length > 300 ? "…" : ""}
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
