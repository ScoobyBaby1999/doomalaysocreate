// WorkspaceScreen — game-designer-quality 2x2 grid of 3D workspace cards.
//
// Layout: 2 columns x N rows (4 cards visible at a time, scrolls vertically
// to reveal more — up to 10 workspaces total).
//
// Card features:
//  - 3D-ish polished rectangle (gradient + glassmorphism + shadow + hover lift)
//  - Customizable color gradient (preset palette)
//  - Customizable icon (emoji picker)
//  - Name, description, owner, updated since, license badge, ext/type badge
//  - Brain icon → opens WorkspaceMemoryPanel (per-workspace .pied log)
//  - Click body → opens WorkspaceMindsPanel (per-workspace agent config)
//  - Trash icon → unclone/delete (FIXES the "can't remove workspace" bug)
//  - Drag-and-drop reordering (HTML5 DnD on desktop, long-press swap on touch)
//
// Empty slots:
//  - "Create" card (+ icon) → opens CreateRepoDialog (creates a NEW GitHub repo)
//  - "Clone" card (+ icon) → opens the existing clone flow (select repo)
//  - Dynamic "+" card: keeps moving right as workspaces are added; disappears
//    at 10 workspaces.
//
// If user is NOT signed in to GitHub: a prominent banner below the grid
// reads "GitHub strongly recommended" + a "Sign in" button.
//
// State:
//  - view: "grid" | "clone" | "detail"
//  - Panels (memory, minds, create-dialog) overlay the grid as modals.
//
// Persistence:
//  - Customizations (gradient, icon, ext, license): localStorage key
//    "doomalaysocreate.workspace.custom" = { [wsId]: { gradient, icon, ext, license } }
//  - Ordering: localStorage key "doomalaysocreate.workspace.order" = [wsId, ...]

import { useEffect, useMemo, useRef, useState } from "react";
import {
  GitHubClient,
  type Workspace,
  type Repo,
  type Branch,
  type PushLog,
} from "../api/github";
import { GithubConnect } from "../components/GithubConnect";
import { CreateRepoDialog } from "../components/CreateRepoDialog";
import { WorkspaceMemoryPanel } from "../components/WorkspaceMemoryPanel";
import { WorkspaceMindsPanel } from "../components/WorkspaceMindsPanel";
import { useChatStore } from "../state/chatStore";
import type { Settings } from "../api/panel";

type View = "grid" | "clone" | "detail";

// --- preset gradients (each workspace gets one) ---------------------------
const GRADIENTS: { id: string; label: string; css: string; from: string; to: string }[] = [
  { id: "purple-pink", label: "Purple → Pink", css: "linear-gradient(135deg, #a855f7 0%, #ec4899 100%)", from: "#a855f7", to: "#ec4899" },
  { id: "indigo-purple", label: "Indigo → Purple", css: "linear-gradient(135deg, #6366f1 0%, #a855f7 100%)", from: "#6366f1", to: "#a855f7" },
  { id: "blue-cyan", label: "Blue → Cyan", css: "linear-gradient(135deg, #3b82f6 0%, #06b6d4 100%)", from: "#3b82f6", to: "#06b6d4" },
  { id: "emerald-teal", label: "Emerald → Teal", css: "linear-gradient(135deg, #10b981 0%, #14b8a6 100%)", from: "#10b981", to: "#14b8a6" },
  { id: "green-yellow", label: "Green → Yellow", css: "linear-gradient(135deg, #22c55e 0%, #fbbf24 100%)", from: "#22c55e", to: "#fbbf24" },
  { id: "orange-red", label: "Orange → Red", css: "linear-gradient(135deg, #f97316 0%, #ef4444 100%)", from: "#f97316", to: "#ef4444" },
  { id: "rose-amber", label: "Rose → Amber", css: "linear-gradient(135deg, #f43f5e 0%, #f59e0b 100%)", from: "#f43f5e", to: "#f59e0b" },
  { id: "slate-zinc", label: "Slate → Zinc", css: "linear-gradient(135deg, #475569 0%, #71717a 100%)", from: "#475569", to: "#71717a" },
];

const ICONS = ["📦", "🚀", "🧠", "🛠️", "🎮", "💡", "🔬", "🎨", "📚", "⚡", "🌱", "🔥", "💧", "🌟", "🎯", "🧩", "🏗️", "⚙️", "🎧", "👾"];

const DEFAULT_GRADIENT = GRADIENTS[0];
const DEFAULT_ICON = "📦";

const MAX_WORKSPACES = 10;

const CUSTOM_KEY = "doomalaysocreate.workspace.custom";
const ORDER_KEY = "doomalaysocreate.workspace.order";

interface WsCustom {
  gradientId?: string;
  icon?: string;
  ext?: string;
  license?: string;
}

type CustomMap = Record<string, WsCustom>;

function loadCustom(): CustomMap {
  try {
    const raw = localStorage.getItem(CUSTOM_KEY);
    return raw ? (JSON.parse(raw) as CustomMap) : {};
  } catch {
    return {};
  }
}

function saveCustom(map: CustomMap) {
  try {
    localStorage.setItem(CUSTOM_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

function loadOrder(): string[] {
  try {
    const raw = localStorage.getItem(ORDER_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function saveOrder(ids: string[]) {
  try {
    localStorage.setItem(ORDER_KEY, JSON.stringify(ids));
  } catch {
    /* ignore */
  }
}

function gradientFor(id: string | undefined) {
  return GRADIENTS.find((g) => g.id === id) || DEFAULT_GRADIENT;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!then || isNaN(then)) return "—";
  const diff = Date.now() - then;
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

function guessExt(ws: Workspace, custom?: WsCustom): string {
  if (custom?.ext) return custom.ext;
  // Heuristic from source repo name.
  const src = ws.source_repo || "";
  if (/doomalaysocreate|react|next|vite|node/i.test(src)) return "Node";
  if (/python|fastapi|django|flask/i.test(src)) return "Python";
  if (/rust|cargo/i.test(src)) return "Rust";
  if (/\.go$|^go\//i.test(src)) return "Go";
  return "Repo";
}

function guessLicense(_ws: Workspace, custom?: WsCustom): string {
  return custom?.license || "—";
}

// ===========================================================================
// Main screen
// ===========================================================================

export function WorkspaceScreen({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (s: Settings) => void;
}) {
  const clearingRef = useRef(false);
  const handleUnauthorized = () => {
    if (clearingRef.current) return;
    clearingRef.current = true;
    const probe = new GitHubClient(settings);
    probe
      .status()
      .then((s) => {
        if (s.authenticated) return;
        onChange({ ...settings, githubSessionId: "", githubUsername: "" });
        setView("grid");
      })
      .catch(() => {
        onChange({ ...settings, githubSessionId: "", githubUsername: "" });
        setView("grid");
      })
      .finally(() => {
        clearingRef.current = false;
      });
  };
  const client = useMemo(
    () => new GitHubClient(settings, handleUnauthorized),
    [settings, onChange],
  );

  const [view, setView] = useState<View>("grid");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refreshRef = useRef(0);

  // Customizations (localStorage-backed). The persisted order is read via
  // loadOrder() inside applyOrder() — we don't need to keep it in React state
  // because we always re-derive from the workspaces list + saved order.
  const [custom, setCustom] = useState<CustomMap>({});

  // Modal/panel state.
  const [createOpen, setCreateOpen] = useState(false);
  const [memoryWs, setMemoryWs] = useState<Workspace | null>(null);
  const [mindsWs, setMindsWs] = useState<Workspace | null>(null);

  // Picker popovers (one open at a time).
  const [pickerTarget, setPickerTarget] = useState<{ wsId: string; kind: "gradient" | "icon" } | null>(null);

  // Drag-and-drop swap (touch-friendly fallback for HTML5 DnD).
  const [swapSrc, setSwapSrc] = useState<string | null>(null);

  const connected = !!settings.githubSessionId;

  // Load workspaces + persisted custom.
  useEffect(() => {
    setCustom(loadCustom());
    if (!connected) {
      setLoading(false);
      return;
    }
    let alive = true;
    client
      .listWorkspaces()
      .then((r) => {
        if (alive) {
          const sorted = applyOrder(r.workspaces, loadOrder());
          setWorkspaces(sorted);
        }
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected]);

  function applyOrder(list: Workspace[], savedOrder: string[]): Workspace[] {
    if (!savedOrder.length) return list;
    const byId = new Map(list.map((w) => [w.id, w]));
    const ordered: Workspace[] = [];
    for (const id of savedOrder) {
      const w = byId.get(id);
      if (w) {
        ordered.push(w);
        byId.delete(id);
      }
    }
    // Append any new workspaces not in savedOrder.
    for (const w of byId.values()) ordered.push(w);
    return ordered;
  }

  function refresh() {
    setLoading(true);
    setError("");
    const reqId = ++refreshRef.current;
    client
      .listWorkspaces()
      .then((r) => {
        if (reqId === refreshRef.current) {
          setWorkspaces(applyOrder(r.workspaces, loadOrder()));
        }
      })
      .catch((e) => {
        if (reqId === refreshRef.current) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (reqId === refreshRef.current) setLoading(false);
      });
  }

  function updateCustom(wsId: string, patch: WsCustom) {
    setCustom((prev) => {
      const next = { ...prev, [wsId]: { ...prev[wsId], ...patch } };
      saveCustom(next);
      return next;
    });
  }

  function reorder(fromIdx: number, toIdx: number) {
    if (fromIdx === toIdx) return;
    setWorkspaces((prev) => {
      const next = [...prev];
      const [moved] = next.splice(fromIdx, 1);
      next.splice(toIdx, 0, moved);
      const ids = next.map((w) => w.id);
      saveOrder(ids);
      return next;
    });
  }

  function openDetail(ws: Workspace) {
    setSelected(ws);
    setView("detail");
  }

  function handleCreated(ws: Workspace) {
    setWorkspaces((prev) => [ws, ...prev.filter((w) => w.id !== ws.id)]);
    setCreateOpen(false);
    setSelected(ws);
    // Auto-open the Minds panel for the new workspace so the user lands in
    // the agent view immediately.
    setMindsWs(ws);
  }

  function handleCloned(ws: Workspace) {
    setWorkspaces((prev) => [ws, ...prev.filter((w) => w.id !== ws.id)]);
    setView("grid");
    setSelected(ws);
    setMindsWs(ws);
  }

  function handleDeleted(id: string) {
    setWorkspaces((prev) => prev.filter((w) => w.id !== id));
    if (selected?.id === id) setSelected(null);
    if (memoryWs?.id === id) setMemoryWs(null);
    if (mindsWs?.id === id) setMindsWs(null);
  }

  async function handleDeleteFromCard(ws: Workspace) {
    if (!confirm(`Remove workspace "${ws.title}"?\n\nThis deletes the local workspace (sandbox + branches). The GitHub repo is NOT deleted.`)) return;
    try {
      await client.deleteWorkspace(ws.id);
      handleDeleted(ws.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // --- Not connected: prompt to connect, but still show grid shell ---
  if (!connected) {
    return (
      <div className="flex flex-col h-full">
        <GridHeader
          connected={false}
          settings={settings}
          onChange={onChange}
          onRefresh={refresh}
        />
        <div className="flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-2 gap-3 max-w-xl mx-auto">
            <EmptySlot kind="create" disabled onClick={() => setCreateOpen(true)} />
            <EmptySlot kind="clone" disabled onClick={() => { /* needs GitHub */ }} />
          </div>
          <div className="max-w-xl mx-auto mt-6 rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4 flex items-center gap-3">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
              <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold text-amber-200">GitHub strongly recommended</div>
              <div className="text-[11px] text-amber-200/70">Connect GitHub to create repos, clone workspaces, and push code.</div>
            </div>
            <GithubConnect
              settings={settings}
              onConnected={(id, username) => {
                onChange({ ...settings, githubSessionId: id, githubUsername: username });
              }}
            />
          </div>
        </div>
      </div>
    );
  }

  // --- Clone view (existing flow, kept as-is) ---
  if (view === "clone") {
    return (
      <CloneWorkspace
        client={client}
        onCreated={handleCloned}
        onCancel={() => setView("grid")}
      />
    );
  }

  // --- Detail view (existing commit/push/logs UI) ---
  if (view === "detail" && selected) {
    return (
      <WorkspaceDetail
        client={client}
        workspace={selected}
        onBack={() => setView("grid")}
        onUpdated={(ws) => {
          setSelected(ws);
          setWorkspaces((prev) => prev.map((w) => (w.id === ws.id ? ws : w)));
        }}
        onDeleted={(id) => {
          handleDeleted(id);
          setView("grid");
        }}
      />
    );
  }

  // --- Grid view (default) ---
  const visibleWorkspaces = workspaces.slice(0, MAX_WORKSPACES);
  const atMax = workspaces.length >= MAX_WORKSPACES;

  return (
    <div className="flex flex-col h-full">
      <GridHeader
        connected={connected}
        settings={settings}
        onChange={onChange}
        onRefresh={refresh}
      />

      {error && (
        <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/40 bg-rose-500/10">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-3 sm:p-4">
        {loading && workspaces.length === 0 ? (
          <div className="grid grid-cols-2 gap-3 max-w-xl mx-auto">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="aspect-[4/5] sm:aspect-square rounded-2xl skeleton" />
            ))}
          </div>
        ) : workspaces.length === 0 ? (
          // Default state: 2 empty slots (Create + Clone)
          <div className="grid grid-cols-2 gap-3 max-w-xl mx-auto">
            <EmptySlot kind="create" onClick={() => setCreateOpen(true)} />
            <EmptySlot kind="clone" onClick={() => setView("clone")} />
          </div>
        ) : (
          // Populated grid: workspace cards + dynamic "+" Clone card
          <div className="grid grid-cols-2 gap-3 max-w-xl mx-auto">
            {visibleWorkspaces.map((ws, idx) => (
              <WorkspaceCard
                key={ws.id}
                ws={ws}
                idx={idx}
                custom={custom[ws.id]}
                owner={settings.githubUsername || ws.user_id || "you"}
                isSwapSrc={swapSrc === ws.id}
                onClick={() => setMindsWs(ws)}
                onOpenMemory={() => setMemoryWs(ws)}
                onOpenDetail={() => openDetail(ws)}
                onDelete={() => handleDeleteFromCard(ws)}
                onPickGradient={() => setPickerTarget({ wsId: ws.id, kind: "gradient" })}
                onPickIcon={() => setPickerTarget({ wsId: ws.id, kind: "icon" })}
                onReorder={reorder}
                onSwapSelect={() => {
                  if (swapSrc === ws.id) {
                    setSwapSrc(null);
                  } else if (swapSrc) {
                    const fromIdx = workspaces.findIndex((w) => w.id === swapSrc);
                    if (fromIdx >= 0) reorder(fromIdx, idx);
                    setSwapSrc(null);
                  } else {
                    setSwapSrc(ws.id);
                  }
                }}
              />
            ))}

            {/* Dynamic "+" Clone card — always at the end, disappears at 10 */}
            {!atMax && (
              <EmptySlot
                kind="clone"
                onClick={() => setView("clone")}
              />
            )}
          </div>
        )}

        {/* GitHub recommended banner (only when connected but few workspaces) */}
        {connected && workspaces.length === 0 && (
          <div className="max-w-xl mx-auto mt-6 rounded-2xl border border-accent/30 bg-accent/5 p-4 flex items-center gap-3">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>
            </svg>
            <div className="flex-1 min-w-0 text-[12px] text-muted-foreground">
              <span className="text-text font-medium">Tip:</span> Use <span className="text-accent">Create</span> to spin up a brand-new GitHub repo, or <span className="text-accent">Clone</span> to import an existing one as a workspace.
            </div>
          </div>
        )}
      </div>

      {/* Floating swap-mode hint */}
      {swapSrc && (
        <div className="fixed bottom-20 left-1/2 -translate-x-1/2 z-40 px-3 py-1.5 rounded-full bg-accent text-white text-[11px] shadow-lg flex items-center gap-2">
          <span>Tap another card to swap, or</span>
          <button onClick={() => setSwapSrc(null)} className="underline">cancel</button>
        </div>
      )}

      {/* Create-repo modal */}
      <CreateRepoDialog
        open={createOpen}
        client={client}
        defaultOwner={settings.githubUsername}
        onClose={() => setCreateOpen(false)}
        onRepoCreated={async (repo: Repo) => {
          // After GitHub repo is created, clone it as a workspace.
          const ws = await client.createWorkspace({
            title: repo.name,
            description: repo.description || "",
            source_repo: repo.clone_url || `https://github.com/${repo.full_name}.git`,
            source_branch: repo.default_branch,
            visibility: repo.private ? "private" : "public",
          });
          // Stash metadata from the repo into customizations.
          updateCustom(ws.id, {
            ext: guessExtFromRepo(repo),
            license: "—",
          });
          return ws;
        }}
        onWorkspaceReady={handleCreated}
      />

      {/* Memory panel */}
      <WorkspaceMemoryPanel
        open={!!memoryWs}
        workspace={memoryWs}
        settings={settings}
        onClose={() => setMemoryWs(null)}
      />

      {/* Minds panel */}
      {mindsWs && (
        <div className="fixed inset-0 z-[60] bg-bg">
          <WorkspaceMindsPanel
            open={!!mindsWs}
            workspace={mindsWs}
            settings={settings}
            onBack={() => setMindsWs(null)}
          />
        </div>
      )}

      {/* Picker popovers (gradient + icon) */}
      {pickerTarget && (
        <PickerPopover
          target={pickerTarget}
          custom={custom[pickerTarget.wsId]}
          onPickGradient={(gid) => {
            updateCustom(pickerTarget.wsId, { gradientId: gid });
            setPickerTarget(null);
          }}
          onPickIcon={(icon) => {
            updateCustom(pickerTarget.wsId, { icon });
            setPickerTarget(null);
          }}
          onClose={() => setPickerTarget(null)}
        />
      )}
    </div>
  );
}

function guessExtFromRepo(repo: Repo): string {
  const name = (repo.name || "").toLowerCase();
  const full = (repo.full_name || "").toLowerCase();
  if (/doomalaysocreate|react|next|vite|node|npm|webpack/.test(name + full)) return "Node";
  if (/python|fastapi|django|flask/.test(name + full)) return "Python";
  if (/rust|cargo/.test(name + full)) return "Rust";
  if (/^go[-_]|golang/.test(name + full)) return "Go";
  return "Repo";
}

// ===========================================================================
// Grid header
// ===========================================================================

function GridHeader({
  connected,
  settings,
  onChange,
  onRefresh,
}: {
  connected: boolean;
  settings: Settings;
  onChange: (s: Settings) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="flex items-center gap-2 px-3 h-10 border-b border-border text-[11px] text-muted shrink-0 bg-surface/40">
      <span className="font-medium text-text">Workspaces</span>
      {connected && (
        <>
          <button onClick={onRefresh} className="ml-auto px-2 py-1 rounded-lg border border-border hover:border-accent/60 text-text" title="Refresh">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
            </svg>
          </button>
          {settings.githubSessionId && (
            <button
              onClick={() => onChange({ ...settings, githubSessionId: "", githubUsername: "" })}
              className="px-2 py-1 rounded-lg border border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
              title="Clear session"
            >
              clear
            </button>
          )}
        </>
      )}
      {!connected && (
        <GithubConnect
          settings={settings}
          onConnected={(id, username) => {
            onChange({ ...settings, githubSessionId: id, githubUsername: username });
          }}
        />
      )}
    </div>
  );
}

// ===========================================================================
// Empty slot (Create or Clone)
// ===========================================================================

function EmptySlot({
  kind,
  onClick,
  disabled,
}: {
  kind: "create" | "clone";
  onClick: () => void;
  disabled?: boolean;
}) {
  const isCreate = kind === "create";
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`group aspect-[4/5] sm:aspect-square rounded-2xl border-2 border-dashed flex flex-col items-center justify-center gap-2 transition-all ${
        disabled
          ? "border-border/40 opacity-40 cursor-not-allowed"
          : "border-border hover:border-accent hover:bg-accent/5 active:scale-[0.98]"
      }`}
    >
      <div className={`w-12 h-12 rounded-full flex items-center justify-center transition-transform group-hover:scale-110 ${isCreate ? "bg-accent/15" : "bg-surface2"}`}>
        {isCreate ? (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        ) : (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#c084fc" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        )}
      </div>
      <div className="text-[12px] font-medium text-text">
        {isCreate ? "Create" : "Clone"}
      </div>
      <div className="text-[10px] text-muted-foreground text-center px-2">
        {isCreate ? "New GitHub repo" : "From existing repo"}
      </div>
    </button>
  );
}

// ===========================================================================
// WorkspaceCard — 3D-ish polished rectangle
// ===========================================================================

function WorkspaceCard({
  ws,
  idx,
  custom,
  owner,
  isSwapSrc,
  onClick,
  onOpenMemory,
  onOpenDetail,
  onDelete,
  onPickGradient,
  onPickIcon,
  onReorder,
  onSwapSelect,
}: {
  ws: Workspace;
  idx: number;
  custom?: WsCustom;
  owner: string;
  isSwapSrc: boolean;
  onClick: () => void;
  onOpenMemory: () => void;
  onOpenDetail: () => void;
  onDelete: () => void;
  onPickGradient: () => void;
  onPickIcon: () => void;
  onReorder: (fromIdx: number, toIdx: number) => void;
  onSwapSelect: () => void;
}) {
  const grad = gradientFor(custom?.gradientId);
  const icon = custom?.icon || DEFAULT_ICON;
  const ext = guessExt(ws, custom);
  const license = guessLicense(ws, custom);
  const isPublic = ws.visibility === "public";

  // HTML5 DnD handlers (desktop reorder). The dragged card's idx is stashed
  // in dataTransfer; the drop target reads it and calls onReorder(from, to).
  const handleDragStart = (e: React.DragEvent) => {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(idx));
  };
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const raw = e.dataTransfer.getData("text/plain");
    const fromIdx = parseInt(raw, 10);
    if (!isNaN(fromIdx) && fromIdx !== idx) onReorder(fromIdx, idx);
  };

  return (
    <div
      draggable
      onDragStart={handleDragStart}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={onClick}
      className={`group relative aspect-[4/5] sm:aspect-square rounded-2xl overflow-hidden cursor-pointer transition-all duration-200
                  hover:-translate-y-1 hover:shadow-xl active:scale-[0.98]
                  ${isSwapSrc ? "ring-2 ring-accent" : ""}`}
      style={{
        background: `
          linear-gradient(180deg, rgba(10,10,10,0.0) 0%, rgba(10,10,10,0.65) 100%),
          ${grad.css}
        `,
        boxShadow: `0 8px 24px -8px ${grad.from}80, 0 2px 8px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.08)`,
        backdropFilter: "blur(8px) saturate(1.1)",
      }}
    >
      {/* glossy top highlight */}
      <div className="absolute inset-x-0 top-0 h-1/3 pointer-events-none" style={{ background: "linear-gradient(180deg, rgba(255,255,255,0.08), transparent)" }} />

      {/* top row: icon (left) + action buttons (right) */}
      <div className="absolute top-0 left-0 right-0 p-2.5 flex items-start justify-between">
        <button
          onClick={(e) => { e.stopPropagation(); onPickIcon(); }}
          className="touch-target w-9 h-9 rounded-xl bg-black/40 backdrop-blur flex items-center justify-center text-lg hover:bg-black/60 active:scale-95"
          title="Change icon"
          aria-label="Change icon"
        >
          {icon}
        </button>
        <div className="flex items-center gap-1">
          <CardActionBtn label="Memory" onClick={(e) => { e.stopPropagation(); onOpenMemory(); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
              <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
            </svg>
          </CardActionBtn>
          <CardActionBtn label="Detail" onClick={(e) => { e.stopPropagation(); onOpenDetail(); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
            </svg>
          </CardActionBtn>
          <CardActionBtn label="Remove" onClick={(e) => { e.stopPropagation(); onDelete(); }} danger>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
          </CardActionBtn>
        </div>
      </div>

      {/* body: name + description */}
      <div className="absolute inset-x-0 bottom-0 p-3">
        <div className="flex items-center gap-1.5 mb-0.5">
          <span className="text-[13px] font-bold text-white truncate drop-shadow">{ws.title}</span>
          {isPublic && (
            <span className="text-[9px] px-1 py-0.5 rounded bg-white/20 text-white font-medium shrink-0">public</span>
          )}
        </div>
        <div className="text-[10px] text-white/80 line-clamp-2 mb-1.5 drop-shadow">
          {ws.description || "No description"}
        </div>
        <div className="flex items-center gap-1.5 text-[9px] text-white/70 mb-1.5">
          <span className="inline-flex items-center gap-0.5">
            <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="8" r="4"/><path d="M4 22v-2a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v2"/></svg>
            {owner}
          </span>
          <span>·</span>
          <span>{relativeTime(ws.last_modified)}</span>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          <span className="text-[9px] px-1.5 py-0.5 rounded bg-black/40 text-white/90 font-medium">{ext}</span>
          <span className="text-[9px] px-1.5 py-0.5 rounded bg-black/40 text-white/90 font-medium">{license}</span>
          {ws.current_branch && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-black/40 text-white/70 font-mono truncate max-w-[80px]">{ws.current_branch}</span>
          )}
          {/* gradient swatch (color picker trigger) */}
          <button
            onClick={(e) => { e.stopPropagation(); onPickGradient(); }}
            className="ml-auto w-4 h-4 rounded-full border border-white/30 hover:scale-110 active:scale-90 transition-transform"
            style={{ background: grad.css }}
            title="Change gradient"
            aria-label="Change gradient"
          />
          {/* swap (mobile reorder) handle */}
          <button
            onClick={(e) => { e.stopPropagation(); onSwapSelect(); }}
            className={`w-4 h-4 rounded flex items-center justify-center hover:scale-110 active:scale-90 transition-transform ${isSwapSrc ? "text-accent" : "text-white/60"}`}
            title="Move (tap, then tap another card)"
            aria-label="Move workspace"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}

function CardActionBtn({
  children,
  onClick,
  label,
  danger,
}: {
  children: React.ReactNode;
  onClick: (e: React.MouseEvent) => void;
  label: string;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`touch-target w-7 h-7 rounded-lg bg-black/40 backdrop-blur flex items-center justify-center hover:bg-black/60 active:scale-95 ${
        danger ? "text-rose-300 hover:text-rose-200" : "text-white/80 hover:text-white"
      }`}
    >
      {children}
    </button>
  );
}

// ===========================================================================
// Picker popover (gradient + icon)
// ===========================================================================

function PickerPopover({
  target,
  custom,
  onPickGradient,
  onPickIcon,
  onClose,
}: {
  target: { wsId: string; kind: "gradient" | "icon" };
  custom?: WsCustom;
  onPickGradient: (gid: string) => void;
  onPickIcon: (icon: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[75] flex items-end sm:items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative w-full sm:max-w-sm bg-bg sm:rounded-2xl rounded-t-2xl border border-border shadow-2xl p-4 slide-up"
        onClick={(e) => e.stopPropagation()}
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <div className="sm:hidden flex justify-center pt-1 pb-2"><div className="w-10 h-1 rounded-full bg-border" /></div>
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm font-semibold text-text">
            {target.kind === "gradient" ? "Pick gradient" : "Pick icon"}
          </span>
          <button onClick={onClose} className="touch-target w-8 h-8 rounded-lg bg-surface2 flex items-center justify-center text-muted hover:text-text">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        {target.kind === "gradient" ? (
          <div className="grid grid-cols-4 gap-2">
            {GRADIENTS.map((g) => {
              const selected = (custom?.gradientId || DEFAULT_GRADIENT.id) === g.id;
              return (
                <button
                  key={g.id}
                  onClick={() => onPickGradient(g.id)}
                  className={`aspect-square rounded-xl border-2 transition-all hover:scale-105 active:scale-95 ${selected ? "border-white ring-2 ring-accent" : "border-transparent"}`}
                  style={{ background: g.css }}
                  title={g.label}
                  aria-label={g.label}
                />
              );
            })}
          </div>
        ) : (
          <div className="grid grid-cols-6 gap-2">
            {ICONS.map((ic) => {
              const selected = (custom?.icon || DEFAULT_ICON) === ic;
              return (
                <button
                  key={ic}
                  onClick={() => onPickIcon(ic)}
                  className={`aspect-square rounded-xl border text-xl flex items-center justify-center transition-all hover:scale-105 active:scale-95 ${
                    selected ? "border-accent bg-accent/15" : "border-border bg-surface hover:border-accent/50"
                  }`}
                >
                  {ic}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// Clone workspace (existing flow, lightly restyled)
// ===========================================================================

function CloneWorkspace({
  client,
  onCreated,
  onCancel,
}: {
  client: GitHubClient;
  onCreated: (ws: Workspace) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string>("");
  const [branches, setBranches] = useState<Branch[]>([]);
  const [selectedBranch, setSelectedBranch] = useState<string>("");
  const [selectedBranches, setSelectedBranches] = useState<Set<string>>(new Set());
  const [branchMode, setBranchMode] = useState<"single" | "all" | "select">("single");
  const [loadingRepos, setLoadingRepos] = useState(true);
  const [loadingBranches, setLoadingBranches] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const repoSelectRef = useRef(0);
  const titleEdited = useRef(false);
  const descriptionEdited = useRef(false);

  useEffect(() => {
    let alive = true;
    client
      .repos(1, 100)
      .then((r) => {
        if (alive) setRepos(r.repos);
      })
      .catch(() => {
        if (alive) setError("Failed to load repos");
      })
      .finally(() => {
        if (alive) setLoadingRepos(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleRepoSelect(fullName: string) {
    setSelectedRepo(fullName);
    setSelectedBranch("");
    setSelectedBranches(new Set());
    if (!fullName) {
      setBranches([]);
      if (!titleEdited.current) setTitle("");
      if (!descriptionEdited.current) setDescription("");
      return;
    }
    setLoadingBranches(true);
    const reqId = ++repoSelectRef.current;
    try {
      const [owner, repo] = fullName.split("/");
      const branchesResult = await client.branches(owner, repo);
      if (reqId !== repoSelectRef.current) return;
      setBranches(branchesResult.branches);
      const def = branchesResult.branches.find((b) => b.name === "main") || branchesResult.branches[0];
      if (def) setSelectedBranch(def.name);
      const repoInfo = repos.find((r) => r.full_name === fullName);
      if (repoInfo && !titleEdited.current) setTitle(repoInfo.name);
      if (repoInfo && !descriptionEdited.current) setDescription(repoInfo.description || "");
    } catch (e) {
      if (reqId === repoSelectRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (reqId === repoSelectRef.current) setLoadingBranches(false);
    }
  }

  async function handleCreate() {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const opts: Parameters<GitHubClient["createWorkspace"]>[0] = {
        title: title.trim(),
        description: description.trim(),
      };
      if (selectedRepo) {
        opts.source_repo = `https://github.com/${selectedRepo}.git`;
        if (branchMode === "single") {
          opts.source_branch = selectedBranch || undefined;
        } else if (branchMode === "select") {
          opts.source_branches = Array.from(selectedBranches);
          opts.source_branch = opts.source_branches[0] || undefined;
        }
      }
      const ws = await client.createWorkspace(opts);
      onCreated(ws);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const field = "w-full bg-surface border border-border rounded-xl px-3 py-2.5 text-sm outline-none focus:border-accent";

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border text-[12px] text-muted shrink-0">
        <button onClick={onCancel} className="touch-target h-9 px-3 rounded-lg bg-surface2 border border-border text-text hover:border-accent/60 flex items-center gap-1">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          Back
        </button>
        <span className="font-semibold text-text">Clone Workspace</span>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4 max-w-xl mx-auto w-full">
        {error && (
          <div className="text-sm text-rose-300 bg-rose-500/10 border border-rose-500/40 rounded-lg px-3 py-2">{error}</div>
        )}

        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Title</span>
          <input
            value={title}
            onChange={(e) => { titleEdited.current = true; setTitle(e.target.value); }}
            placeholder="my-project"
            className={field}
          />
        </label>

        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Description (optional)</span>
          <input
            value={description}
            onChange={(e) => { descriptionEdited.current = true; setDescription(e.target.value); }}
            placeholder="What this workspace is for"
            className={field}
          />
        </label>

        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Clone from repo (optional)</span>
          <select
            value={selectedRepo}
            onChange={(e) => handleRepoSelect(e.target.value)}
            disabled={loadingRepos}
            className={field}
          >
            <option value="">{loadingRepos ? "loading repos…" : "— none (empty workspace) —"}</option>
            {repos.map((r) => (
              <option key={r.full_name} value={r.full_name}>
                {r.full_name} {r.private ? "🔒" : ""}
              </option>
            ))}
          </select>
        </label>

        {selectedRepo && (
          <>
            <div className="space-y-1.5">
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Branch selection</span>
              <div className="flex gap-2 text-sm">
                {(["single", "all", "select"] as const).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => setBranchMode(mode)}
                    className={`px-3 py-1.5 rounded-xl border text-[11px] ${
                      branchMode === mode ? "border-accent bg-accent/10 text-accent" : "border-border text-muted hover:border-accent/50"
                    }`}
                  >
                    {mode === "single" ? "Single" : mode === "all" ? "All" : "Select"}
                  </button>
                ))}
              </div>
            </div>

            {branchMode === "single" && (
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Branch</span>
                <select value={selectedBranch} onChange={(e) => setSelectedBranch(e.target.value)} disabled={loadingBranches} className={field}>
                  {loadingBranches ? <option>loading…</option> : branches.map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
                </select>
              </label>
            )}

            {branchMode === "select" && (
              <div className="space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Branches to clone</span>
                <div className="max-h-40 overflow-y-auto border border-border rounded-xl p-2 space-y-1">
                  {loadingBranches ? (
                    <div className="text-[11px] text-muted px-2 py-1">loading…</div>
                  ) : (
                    branches.map((b) => (
                      <label key={b.name} className="flex items-center gap-2 px-2 py-1 rounded-lg hover:bg-surface cursor-pointer text-sm">
                        <input
                          type="checkbox"
                          checked={selectedBranches.has(b.name)}
                          onChange={(e) => {
                            const next = new Set(selectedBranches);
                            if (e.target.checked) next.add(b.name);
                            else next.delete(b.name);
                            setSelectedBranches(next);
                          }}
                          className="accent-accent"
                        />
                        {b.name}
                      </label>
                    ))
                  )}
                </div>
              </div>
            )}

            {branchMode === "all" && (
              <div className="text-[11px] text-muted bg-surface border border-border rounded-xl px-3 py-2.5">
                All branches will be cloned (shallow).
              </div>
            )}
          </>
        )}

        <button
          onClick={handleCreate}
          disabled={submitting || !title.trim()}
          className="w-full py-2.5 rounded-xl bg-accent text-white font-medium disabled:opacity-40 flex items-center justify-center gap-2"
        >
          {submitting ? (
            <>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
                <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
                <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
                <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
              </svg>
              Cloning…
            </>
          ) : (
            "Clone Workspace"
          )}
        </button>
      </div>
    </div>
  );
}

// ===========================================================================
// WorkspaceDetail — existing commit/push/logs UI (kept as-is, lightly restyled)
// ===========================================================================

function WorkspaceDetail({
  client,
  workspace: initial,
  onBack,
  onUpdated,
  onDeleted,
}: {
  client: GitHubClient;
  workspace: Workspace;
  onBack: () => void;
  onUpdated: (ws: Workspace) => void;
  onDeleted: (id: string) => void;
}) {
  const [ws, setWs] = useState(initial);
  const [logs, setLogs] = useState<PushLog[]>([]);
  const [commitMsg, setCommitMsg] = useState("");
  const [committing, setCommitting] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [pendingApproval, setPendingApproval] = useState<{
    requestId: string;
    status: string;
    branch?: string;
    title?: string;
  } | null>(null);
  const [approving, setApproving] = useState(false);
  const approvingRef = useRef(false);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    let alive = true;
    client
      .pushLogs(ws.id, 10)
      .then((r) => {
        if (alive) setLogs(r.logs);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.id]);

  useEffect(() => {
    if (!pendingApproval || pendingApproval.status !== "pending") return;
    let alive = true;
    const iv = setInterval(async () => {
      try {
        const result = await client.pushRequestStatus(pendingApproval.requestId);
        if (!alive) return;
        if (result.status === "approved" || result.status === "rejected" || result.status === "error") {
          clearInterval(iv);
          setPendingApproval(null);
          if (result.status === "approved") {
            setSuccess("Push approved! Reloading…");
            const fresh = await client.getWorkspace(ws.id);
            if (alive) {
              setWs(fresh);
              onUpdated(fresh);
              const logsR = await client.pushLogs(ws.id, 10);
              if (alive) setLogs(logsR.logs);
            }
          } else if (result.status === "rejected") {
            setError("Push rejected.");
          } else {
            setError("Push request failed.");
          }
        }
      } catch {
        /* keep polling */
      }
    }, 3000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingApproval?.requestId, pendingApproval?.status]);

  async function handleCommit() {
    if (!commitMsg.trim()) return;
    setCommitting(true);
    setError("");
    setSuccess("");
    try {
      const r = await client.commit(ws.id, commitMsg.trim());
      setSuccess(`Committed: ${r.commit_sha.slice(0, 7)}`);
      setCommitMsg("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCommitting(false);
    }
  }

  async function handlePush() {
    setPushing(true);
    setError("");
    setSuccess("");
    try {
      const result = await client.push(ws.id, { branch: ws.current_branch });
      if ("request_id" in result) {
        setPendingApproval({
          requestId: result.request_id,
          status: "pending",
          branch: result.branch,
          title: result.title,
        });
        setSuccess("Push pending approval — waiting for review");
      } else {
        setSuccess(`Pushed: ${(result as { commit_sha: string }).commit_sha.slice(0, 7)}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPushing(false);
    }
  }

  async function handleApprovePush(approved: boolean) {
    if (!pendingApproval || approvingRef.current) return;
    approvingRef.current = true;
    setApproving(true);
    try {
      await client.resolvePushRequest(pendingApproval.requestId, approved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      approvingRef.current = false;
      setApproving(false);
    }
  }

  async function handleDelete() {
    if (!confirm(`Delete workspace "${ws.title}"? This cannot be undone.`)) return;
    try {
      await client.deleteWorkspace(ws.id);
      onDeleted(ws.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handlePublish() {
    setPublishing(true);
    setError("");
    try {
      await client.publish(ws.id);
      setSuccess("Published to registry");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }

  async function handleUnpublish() {
    setPublishing(true);
    setError("");
    try {
      await client.unpublish(ws.id);
      setSuccess("Removed from registry");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border text-[12px] text-muted shrink-0 bg-surface/40">
        <button onClick={onBack} className="touch-target h-9 px-3 rounded-lg bg-surface2 border border-border text-text hover:border-accent/60 flex items-center gap-1">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          Workspaces
        </button>
        <span className="font-semibold text-text">{ws.title}</span>
        <span className="px-1.5 py-0.5 rounded bg-surface2 text-[10px]">{ws.current_branch}</span>
        <button
          onClick={() => {
            useChatStore.getState().setWorkspaceId(ws.id);
            setSuccess("Workspace set for chat! Switch to the Chat tab.");
            setTimeout(() => setSuccess(""), 3000);
          }}
          className="ml-auto px-2 py-1 rounded-lg bg-accent text-white text-[10px] font-medium hover:bg-accent/80"
        >
          Use for chat
        </button>
        {ws.source_repo && (
          <a href={ws.source_repo} target="_blank" rel="noreferrer noopener" className="text-accent underline text-[11px]">repo ↗</a>
        )}
      </div>

      {error && <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/40 bg-rose-500/10">{error}</div>}
      {success && <div className="px-3 py-2 text-sm text-green-300 border-b border-green-500/40 bg-green-500/10">{success}</div>}

      {pendingApproval && pendingApproval.status === "pending" && (
        <div className="px-3 py-3 border-b border-border bg-amber-500/5">
          <div className="flex items-center gap-2 mb-2">
            <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="text-sm text-amber-200">Push pending approval</span>
          </div>
          {pendingApproval.branch && (
            <div className="text-[11px] text-muted mb-2">Branch: <span className="text-text">{pendingApproval.branch}</span></div>
          )}
          <div className="flex gap-2">
            <button onClick={() => handleApprovePush(true)} disabled={approving} className="flex-1 py-1.5 rounded-lg bg-green-600 text-white text-sm disabled:opacity-40">{approving ? "…" : "Approve"}</button>
            <button onClick={() => handleApprovePush(false)} disabled={approving} className="flex-1 py-1.5 rounded-lg border border-rose-500/40 text-rose-300 text-sm hover:bg-rose-500/10 disabled:opacity-40">{approving ? "…" : "Reject"}</button>
          </div>
        </div>
      )}

      <div className="border-b border-border p-3 space-y-2">
        <textarea
          value={commitMsg}
          onChange={(e) => setCommitMsg(e.target.value)}
          placeholder="Commit message…"
          rows={2}
          className="w-full bg-surface border border-border rounded-xl px-3 py-2 text-sm outline-none focus:border-accent resize-none"
        />
        <div className="flex gap-2">
          <button onClick={handleCommit} disabled={committing || !commitMsg.trim()} className="flex-1 py-1.5 rounded-xl border border-border text-sm hover:border-accent disabled:opacity-40">{committing ? "committing…" : "Commit"}</button>
          <button onClick={handlePush} disabled={pushing} className="flex-1 py-1.5 rounded-xl bg-accent text-white text-sm disabled:opacity-40">{pushing ? "pushing…" : "Push"}</button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="px-3 py-2 text-[11px] text-muted font-medium border-b border-border">push history</div>
        {logs.length === 0 ? (
          <div className="text-center text-muted text-sm mt-8">No pushes yet</div>
        ) : (
          <div className="divide-y divide-border">
            {logs.map((log) => (
              <div key={log.id} className="px-3 py-2 text-[11px]">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-text">{log.commit_sha.slice(0, 7)}</span>
                  <span className="text-muted">{log.push_type}</span>
                  {log.pr_url && <a href={log.pr_url} target="_blank" rel="noreferrer noopener" className="text-accent underline">PR #{log.pr_number}</a>}
                </div>
                {log.commit_message && <div className="text-muted mt-0.5">{log.commit_message}</div>}
                <div className="text-muted mt-0.5">→ {log.target_repo.replace("https://github.com/", "")}/{log.target_branch} · {new Date(log.created_at).toLocaleString()}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="border-t border-border p-3 space-y-2">
        <button onClick={handlePublish} disabled={publishing} className="w-full py-1.5 rounded-xl border border-border text-[11px] hover:border-accent disabled:opacity-40">{publishing ? "…" : "Publish to Registry"}</button>
        <button onClick={handleUnpublish} disabled={publishing} className="w-full py-1.5 rounded-xl border border-border text-[11px] hover:border-accent disabled:opacity-40">{publishing ? "…" : "Unpublish from Registry"}</button>
        <button onClick={handleDelete} className="w-full py-1.5 rounded-xl border border-rose-500/40 text-rose-300 text-[11px] hover:bg-rose-500/10">Delete Workspace</button>
      </div>
    </div>
  );
}
