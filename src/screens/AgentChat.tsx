import { useEffect, useRef, useState, useCallback } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { Settings, Effort } from "../api/panel";
import type { Workspace } from "../api/github";
import {
  AgentClient,
  type AgentEvent,
  type AgentFile,
  type AgentStatus,
  type ChatSession,
} from "../api/agent";
import { Markdown } from "../components/Markdown";
import { JudgeCard } from "../components/JudgeCard";
import { useModelStore } from "../lib/model-store";
import { PanelDrawer, type PanelInvocation } from "../components/PanelDrawer";
import { FileDrawer } from "../components/FileDrawer";
import { DiffView } from "../components/DiffView";
import { GitStatus } from "../components/GitStatus";
import { SessionSidebar } from "../components/SessionSidebar";
import { useMediaQuery } from "../hooks/useMediaQuery";

const SESSION_KEY = "doomalaysocreate.agent.session";
const CHAT_SESSION_KEY = "doomalaysocreate.agent.chat_session_v2";
const MODEL_KEY = "doomalaysocreate.agent.model";
const EFFORTS: Effort[] = ["low", "med", "high", "max"];

export function AgentChat({
  settings,
  workspaceId,
}: {
  settings: Settings;
  workspaceId?: string;
}) {
  const [selected, setSelected] = useState<string>(localStorage.getItem(MODEL_KEY) || "");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [files, setFiles] = useState<AgentFile[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cost, setCost] = useState<number | null>(null);
  const [effort, setEffort] = useState<Effort>("med");
  const [webSearch, setWebSearch] = useState(false);
  const [deepResearch, setDeepResearch] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [fileDrawerOpen, setFileDrawerOpen] = useState(false);
  const [panelDrawerOpen, setPanelDrawerOpen] = useState(false);
  const [panelInvocations, setPanelInvocations] = useState<PanelInvocation[]>([]);
  const [sessionSidebarOpen, setSessionSidebarOpen] = useState(false);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);

  const openOverlay = useModelStore((s) => s.openOverlay);
  const selectedModelId = useModelStore((s) => s.selectedModelId);
  const selectedProviderName = useModelStore((s) => s.selectedProviderName);
  const providers = useModelStore((s) => s.providers);
  const selectedModelLabel = (() => {
    if (!selectedModelId) return selected || null;
    for (const p of providers) {
      const m = p.models.find((m) => m.id === selectedModelId);
      if (m) return m.displayName;
    }
    return selectedModelId;
  })();
  const selectedProviderColor = (() => {
    if (!selectedProviderName) return "#5b8cff";
    const p = providers.find((g) => g.name === selectedProviderName);
    return p?.color || "#5b8cff";
  })();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(workspaceId || null);
  const sessionRef = useRef<string | null>(sessionStorage.getItem(SESSION_KEY));
  const chatSessionIdRef = useRef<string | null>(localStorage.getItem(CHAT_SESSION_KEY));
  const lastMsgRef = useRef<string>("");
  const listRef = useRef<VirtuosoHandle>(null);
  const client = useRef(new AgentClient(settings));
  client.current = new AgentClient(settings);
  const abortRef = useRef<AbortController | null>(null);

  // load model list on mount
  useEffect(() => {
    let alive = true;
    client.current
      .models()
      .then((r) => {
        if (!alive) return;
        const valid = r.models.find((m) => m.model === selected);
        if (!valid) {
          const def = r.models.find((m) => m.default) || r.models[0];
          if (def) {
            setSelected(def.model);
            localStorage.setItem(MODEL_KEY, def.model);
          }
        }
      })
      .catch(() => {});
    return () => { alive = false; };
  }, [settings.baseUrl, selected]);

  // load workspaces on mount (if github connected)
  useEffect(() => {
    if (!settings.githubSessionId) return;
    let alive = true;
    fetch(`${settings.baseUrl}/api/workspaces`, {
      headers: { Authorization: `Bearer ${settings.githubSessionId}` },
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("fetch workspaces failed"))))
      .then((data) => { if (alive) setWorkspaces(data.workspaces || []); })
      .catch(() => {});
    return () => { alive = false; };
  }, [settings.baseUrl, settings.githubSessionId]);

  // load chat sessions + restore active session on mount
  useEffect(() => {
    let alive = true;
    client.current.listChatSessions()
      .then((r) => {
        if (!alive) return;
        setChatSessions(r.sessions);
        const active = chatSessionIdRef.current;
        if (active && r.sessions.find((s) => s.id === active)) {
          return client.current.getChatEvents(active);
        }
        return null;
      })
      .then((evData) => {
        if (!alive || !evData) return;
        setEvents(evData.events);
        // Try to resume agent session polling if we have an agent session ID
        const sid = sessionRef.current;
        if (sid) {
          client.current.poll(sid, 0)
            .then((snap) => {
              if (!alive) return;
              setStatus(snap.status);
              if (snap.events.length && snap.events.length > evData.events.length) {
                setEvents(snap.events);
              }
              if (snap.status === "running" || snap.status === "starting") {
                pollUntilSettled(sid, snap.next);
              }
            })
            .catch(() => {
              sessionRef.current = null;
              sessionStorage.removeItem(SESSION_KEY);
            });
        }
      })
      .catch(() => {});
    return () => { alive = false };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refreshFiles(sid?: string | null) {
    const id = sid ?? sessionRef.current;
    if (id) {
      try {
        const f = await client.current.files(id);
        setFiles(f.files);
      } catch { /* ignore */ }
    }
  }

  async function pollUntilSettled(sessionId: string, since: number) {
    let cursor = since;
    const ac = abortRef.current;
    for (;;) {
      if (ac?.signal.aborted) return cursor;
      const snap = await client.current.poll(sessionId, cursor);
      if (snap.events.length) {
        setEvents((prev) => [...prev, ...snap.events]);
        cursor = snap.next;
        for (const e of snap.events) {
          if (e.type === "status" && typeof e.cost_usd === "number") setCost(e.cost_usd);
          if (e.type === "panel") {
            setPanelInvocations((prev) => {
              const inv = e.invoke_id;
              const existing = prev.findIndex((p) => p.invoke_id === inv);
              const entry: PanelInvocation = {
                task_name: e.task_name || "panel",
                invoke_id: inv || "",
                prompt: e.prompt || "",
                snapshot: e.snapshot,
                error: e.error,
              };
              if (existing >= 0) {
                const next = [...prev];
                next[existing] = { ...next[existing], ...entry };
                return next;
              }
              if (e.status === "starting") return [...prev, entry];
              return prev;
            });
          }
        }
      }
      setStatus(snap.status);
      if (snap.status !== "running" && snap.status !== "starting") {
        if (snap.status !== "error") {
          client.current.files(sessionId).then((f) => setFiles(f.files)).catch(() => {});
        }
        return cursor;
      }
      await new Promise((res) => setTimeout(res, 200));
    }
  }

  const switchSession = useCallback(async (chatSessionId: string) => {
    abortRef.current?.abort();
    chatSessionIdRef.current = chatSessionId;
    localStorage.setItem(CHAT_SESSION_KEY, chatSessionId);
    sessionRef.current = null;
    sessionStorage.removeItem(SESSION_KEY);
    setEvents([]);
    setFiles([]);
    setStatus("idle");
    setError("");
    setCost(null);
    setBusy(false);
    setPanelInvocations([]);
    try {
      const evData = await client.current.getChatEvents(chatSessionId);
      setEvents(evData.events);
    } catch { /* ignore */ }
  }, []);

  async function send(text?: string) {
    const message = (text ?? input).trim();
    if (!message || busy) return;
    if (text === undefined) setInput("");
    lastMsgRef.current = message;
    setBusy(true);
    setError("");
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setEvents((prev) => [
      ...prev,
      { i: -1, ts: Date.now() / 1000, type: "user", text: message } as AgentEvent,
    ]);
    setStatus("running");
    try {
      const start = await client.current.send(
        message,
        sessionRef.current ?? undefined,
        sessionRef.current ? undefined : selectedModelId || selected || undefined,
        selectedWorkspace ?? undefined,
        chatSessionIdRef.current ?? undefined,
      );
      sessionRef.current = start.session_id;
      sessionStorage.setItem(SESSION_KEY, start.session_id);
      if (start.chat_session_id) {
        chatSessionIdRef.current = start.chat_session_id;
        localStorage.setItem(CHAT_SESSION_KEY, start.chat_session_id);
        // Add new session to list if not already there
        setChatSessions((prev) => {
          if (!start.chat_session_id) return prev;
          if (prev.find((s) => s.id === start.chat_session_id)) return prev;
          return [{ id: start.chat_session_id, title: "New Chat", model: start.model, workspace_id: null, created_at: "", updated_at: "" }, ...prev];
        });
      }
      const fresh = await client.current.poll(start.session_id, 0);
      setEvents((prev) => [...prev.slice(0, -1), ...fresh.events]);
      if (!ac.signal.aborted) {
        await pollUntilSettled(start.session_id, fresh.next);
      }
      // Reload full conversation from DB to restore any history that was lost
      // when the agent session's in-memory events replaced the DB-backed events.
      if (chatSessionIdRef.current) {
        try {
          const dbEvents = await client.current.getChatEvents(chatSessionIdRef.current);
          if (!ac.signal.aborted) setEvents(dbEvents.events);
        } catch { /* non-fatal */ }
      }
      // Refresh session list after completion to get updated title
      client.current.listChatSessions().then((r) => {
        if (!ac.signal.aborted) setChatSessions(r.sessions);
      }).catch(() => {});
    } catch (e) {
      if (ac.signal.aborted) return;
      sessionRef.current = null;
      sessionStorage.removeItem(SESSION_KEY);
      setError(e instanceof Error ? e.message : String(e));
      setStatus("error");
    } finally {
      if (!ac.signal.aborted) setBusy(false);
    }
  }

  async function stop() {
    if (!sessionRef.current) return;
    setStatus("idle");
    try { await client.current.interrupt(sessionRef.current); }
    catch { setError("Interrupt failed"); setStatus("running"); }
  }

  async function newSession(model?: string) {
    abortRef.current?.abort();
    sessionRef.current = null;
    sessionStorage.removeItem(SESSION_KEY);
    setEvents([]);
    setFiles([]);
    setStatus("idle");
    setError("");
    setCost(null);
    setBusy(false);
    setPanelInvocations([]);
    setSessionSidebarOpen(false);
    if (model) { setSelected(model); localStorage.setItem(MODEL_KEY, model); }
    // Create persistent session in DB
    try {
      const cs = await client.current.createChatSession();
      chatSessionIdRef.current = cs.id;
      localStorage.setItem(CHAT_SESSION_KEY, cs.id);
      setChatSessions((prev) => [cs, ...prev]);
    } catch { /* ignore */ }
  }

  async function deleteSession(sessionId: string) {
    try {
      await client.current.deleteChatSession(sessionId);
      setChatSessions((prev) => prev.filter((s) => s.id !== sessionId));
      if (chatSessionIdRef.current === sessionId) {
        chatSessionIdRef.current = null;
        localStorage.removeItem(CHAT_SESSION_KEY);
        setEvents([]);
      }
    } catch { /* ignore */ }
  }

  const isDesktop = useMediaQuery("(min-width: 768px)"); void isDesktop;

  const running = status === "running" || status === "starting";

  return (
    <div className="flex flex-col h-full relative">
      {/* header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted shrink-0">
        <button
          onClick={() => setSessionSidebarOpen((o) => !o)}
          className={`text-sm px-1 shrink-0 ${sessionSidebarOpen ? "text-accent" : "text-muted hover:text-accent"}`}
          title="Sessions"
        >
          ☰
        </button>
        <button
          onClick={() => !running && openOverlay()}
          disabled={running}
          className="flex items-center gap-1.5 px-2 py-1 rounded-lg border border-border hover:border-accent transition-colors text-[12px] disabled:opacity-50 max-w-[45%]"
        >
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: selectedProviderColor || "#5b8cff" }} />
          <span className="text-accent truncate">{selectedModelLabel || "Select model"}</span>
        </button>
        {workspaces.length > 0 && (
          <select
            value={selectedWorkspace || ""}
            onChange={(e) => {
              setSelectedWorkspace(e.target.value || null);
              setFiles([]);
              refreshFiles(sessionRef.current);
            }}
            disabled={running}
            className="bg-surface border border-border rounded-lg px-2 py-1 text-[12px] text-accent outline-none focus:border-accent disabled:opacity-50 max-w-[35%]"
          >
            <option value="">(sandbox)</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>{w.title || w.id}</option>
            ))}
          </select>
        )}
        {cost != null && <span className="text-[10px] shrink-0">${cost.toFixed(4)}</span>}
        <span className="ml-auto text-[10px] capitalize shrink-0">{status}</span>
        <button
          onClick={() => setFileDrawerOpen((o) => !o)}
          className="text-muted hover:text-accent text-sm px-1"
          title="Files"
        >
          📎
        </button>
        <button
          onClick={() => setPanelDrawerOpen((o) => !o)}
          className={`text-sm px-1 ${panelDrawerOpen ? "text-accent" : "text-muted hover:text-accent"}`}
          title="Panel invocations"
        >
          💬
        </button>
        {running ? (
          <button onClick={stop} className="text-rose-300 underline shrink-0">stop</button>
        ) : (
          <button onClick={() => newSession()} className="text-accent underline shrink-0">new</button>
        )}
      </div>

      <div className="flex-1 min-h-0 relative">
        <Virtuoso
          ref={listRef}
          className="h-full"
          data={events}
          followOutput="smooth"
          itemContent={(_, ev) => <EventRow ev={ev} />}
          components={{
            Footer: () => (
              <>
{running && events.some((e) => e.type === "user") &&
  !events.some((e) => e.type === "assistant" || e.type === "thinking" || e.type === "tool_use") && (
                  <div className="px-3 py-3 max-w-2xl mx-auto">
                    <div className="flex items-center gap-2 text-muted">
                      <span className="flex gap-0.5">
                        <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "0ms" }} />
                        <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "150ms" }} />
                        <span className="w-1.5 h-1.5 rounded-full bg-current animate-bounce" style={{ animationDelay: "300ms" }} />
                      </span>
                      <span className="text-[12px]">thinking</span>
                    </div>
                  </div>
                )}
                {events.length === 0 ? (
                  <div className="text-center text-muted text-sm mt-20 px-6">
                    Ask the agent to build, edit, run, or pack something. It works in a
                    private workspace — files appear in the 📎 drawer.
                  </div>
                ) : (
                  <div className="h-2" />
                )}
              </>
            ),
          }}
        />
      </div>

      {error && (
        <div className="border-t border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300 flex items-center gap-2 shrink-0">
          <span className="flex-1">{error}</span>
          {lastMsgRef.current && (
            <button onClick={() => send(lastMsgRef.current)} className="px-2 py-1 rounded-lg border border-rose-400/40 text-xs">
              Retry
            </button>
          )}
        </div>
      )}

      <div className="border-t border-border bg-bg pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] shrink-0">
        {settingsOpen && (
          <div className="flex items-center gap-2 px-3 pb-2 overflow-x-auto">
            {EFFORTS.map((e) => (
              <button
                key={e}
                onClick={() => setEffort(e)}
                className={`text-[11px] px-2 py-0.5 rounded-full border shrink-0 ${
                  effort === e ? "border-accent text-accent" : "border-border text-muted"
                }`}
              >
                {e}
              </button>
            ))}
            <button
              onClick={() => setWebSearch((p) => !p)}
              className={`text-[11px] px-2 py-0.5 rounded-full border shrink-0 flex items-center gap-1 ${
                webSearch ? "border-accent text-accent" : "border-border text-muted"
              }`}
            >
              🌐 Web
            </button>
            <button
              onClick={() => setDeepResearch((p) => !p)}
              className={`text-[11px] px-2 py-0.5 rounded-full border shrink-0 flex items-center gap-1 ${
                deepResearch ? "border-accent text-accent" : "border-border text-muted"
              }`}
            >
              🔬 Deep
            </button>
          </div>
        )}
        <div className="flex items-end gap-2 px-2">
          <button
            onClick={() => {}}
            className="text-muted hover:text-accent text-lg pb-2 shrink-0"
            title="Attach files (coming soon)"
          >
            <span className="opacity-50">+</span>
          </button>
          <button
            onClick={() => setSettingsOpen((p) => !p)}
            className={`text-sm pb-2 shrink-0 ${settingsOpen ? "text-accent" : "text-muted hover:text-accent"}`}
            title="Settings"
          >
            ⚙
          </button>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            rows={1}
            placeholder="Tell the agent what to do…"
            className="flex-1 resize-none bg-surface border border-border rounded-2xl px-3 py-2 text-[15px] outline-none focus:border-accent max-h-32"
          />
          <button
            onClick={() => (running ? stop() : send())}
            disabled={!running && (busy || !input.trim())}
            className={`h-10 px-4 rounded-2xl font-medium disabled:opacity-40 shrink-0 ${
              running ? "bg-rose-500/80 text-white" : "bg-accent text-white"
            }`}
          >
            {running ? "Stop" : "Send"}
          </button>
        </div>
      </div>

      {/* Git status bar — only when workspace is selected */}
      {selectedWorkspace && (
        <div className="shrink-0">
          <GitStatus client={client.current} wsId={selectedWorkspace} />
        </div>
      )}

      <SessionSidebar
        open={sessionSidebarOpen}
        sessions={chatSessions}
        activeId={chatSessionIdRef.current}
        onSelect={switchSession}
        onDelete={deleteSession}
        onNew={newSession}
        onClose={() => setSessionSidebarOpen(false)}
      />
      <FileDrawer
        open={fileDrawerOpen}
        onClose={() => setFileDrawerOpen(false)}
        files={files}
        sessionId={sessionRef.current}
        settings={settings}
        onRefresh={() => refreshFiles()}
      />
      <PanelDrawer
        open={panelDrawerOpen}
        onClose={() => setPanelDrawerOpen(false)}
        invocations={panelInvocations}
      />
    </div>
  );
}

const TOOL_ICONS: Record<string, string> = {
  bash: "⌘", shell: "⌘",
  write: "✎", edit: "✎", str_replace: "✎", fileeditor: "✎",
  read: "👁", view: "👁",
  glob: "🔍", grep: "🔍", search: "🔍",
  web: "🌐", fetch: "🌐", http_request: "🌐",
};

function toolIcon(name: string): string {
  const k = name.toLowerCase();
  for (const key of Object.keys(TOOL_ICONS)) if (k.includes(key)) return TOOL_ICONS[key];
  return "⚙";
}

function isDiff(text: string): boolean {
  return text.includes("---") && text.includes("+++") && /^diff --git/.test(text.trim());
}

function EventRow({ ev }: { ev: AgentEvent }) {
  if (ev.type === "user") {
    return (
      <div className="px-3 py-1.5 max-w-2xl mx-auto flex justify-end">
        <div className="rounded-2xl rounded-br-sm bg-surface2 px-3 py-2 text-[15px] max-w-[85%] whitespace-pre-wrap">
          {ev.text}
        </div>
      </div>
    );
  }
  if (ev.type === "assistant") {
    return (
      <div className="px-3 py-1.5 max-w-2xl mx-auto">
        <Markdown text={ev.text} />
      </div>
    );
  }
  if (ev.type === "thinking") {
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <Collapsible label="thinking"><Markdown text={ev.text} /></Collapsible>
      </div>
    );
  }
  if (ev.type === "tool_use") {
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <div className="text-[12px] text-muted flex items-baseline gap-2">
          <span className="text-accent shrink-0">
            {toolIcon(ev.name)} <span className="font-medium">{ev.name}</span>
          </span>
          {ev.summary ? (
            <span className="font-mono text-[11px] truncate">{ev.summary}</span>
          ) : null}
        </div>
      </div>
    );
  }
  if (ev.type === "tool_result") {
    const isDiffContent = !ev.is_error && isDiff(ev.text);
    if (isDiffContent) {
      return (
        <div className="px-3 py-1 max-w-2xl mx-auto">
          <div className="rounded-lg border border-border overflow-hidden">
            <div className="px-2 py-1 text-[11px] text-muted border-b border-border bg-surface/50">diff</div>
            <DiffView diff={ev.text} />
          </div>
        </div>
      );
    }
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <Collapsible label={ev.is_error ? "result (error)" : "result"} error={ev.is_error}>
          <Markdown text={ev.text} />
        </Collapsible>
      </div>
    );
  }
  if (ev.type === "status" && ev.state === "error") {
    return (
      <div className="px-3 py-1.5 max-w-2xl mx-auto">
        <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
          {ev.detail || "agent error"}
        </div>
      </div>
    );
  }
  if (ev.type === "status" && ev.state === "idle" && ev.detail === "interrupted") {
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto text-center text-[11px] text-muted">
        — stopped —
      </div>
    );
  }
  if (ev.type === "panel") {
    return <PanelEventRow ev={ev} />;
  }
  return null;
}

function PanelEventRow({ ev }: { ev: Extract<AgentEvent, { type: "panel" }> }) {
  const snap = ev.snapshot;
  const status = ev.status;
  return (
    <div className="px-3 py-1.5 max-w-2xl mx-auto">
      <div className="rounded-xl border border-border bg-surface overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
          <span className={`h-2 w-2 rounded-full ${status === "starting" ? "bg-muted" : status === "running" ? "bg-accent animate-pulse" : "bg-emerald-400"}`} />
          <span className="text-sm font-medium">Panel: {ev.task_name || "judges"}</span>
          <span className="ml-auto text-[11px] text-muted">{status}</span>
        </div>
        {snap ? (
          <div className="px-3 py-2 space-y-1.5">
            <div className="text-[11px] text-muted">
              {snap.meta.judges_settled}/{snap.meta.judges_total} settled · {snap.meta.age_s}s
            </div>
            {snap.judges.map((j) => (
              <JudgeCard key={j.model} judge={j} />
            ))}
          </div>
        ) : status === "starting" ? (
          <div className="px-3 py-2 text-sm text-muted">Starting panel…</div>
        ) : null}
      </div>
    </div>
  );
}

function Collapsible({
  label, children, error,
}: {
  label: string; children: React.ReactNode; error?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`rounded-lg border ${error ? "border-rose-500/30" : "border-border"}`}>
      <button
        onClick={() => setOpen((o) => !o)}
        className={`w-full text-left text-[11px] px-2 py-1 ${error ? "text-rose-300" : "text-muted"}`}
      >
        {open ? "▾" : "▸"} {label}
      </button>
      {open && (
        <div className="px-2 pb-2 text-[13px] overflow-x-auto">
          {children}
        </div>
      )}
    </div>
  );
}
