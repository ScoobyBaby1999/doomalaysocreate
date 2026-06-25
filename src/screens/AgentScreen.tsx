import { useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { Settings } from "../api/panel";
import type { Workspace } from "../api/github";
import {
  AgentClient,
  type AgentEvent,
  type AgentFile,
  type AgentStatus,
} from "../api/agent";
import { useModelStore } from "../lib/model-store";

const SESSION_KEY = "doomalaysocreate.agent.session";
const MODEL_KEY = "doomalaysocreate.agent.model";

/** The agent tab: a chat with the orchestrator running inside the user's Space.
 *  A model picker chooses which frontier model drives it (Claude via its SDK,
 *  others via the open agent once that SDK lands).
 *  When workspaceId is provided, the agent operates in that workspace's sandbox. */
export function AgentScreen({
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
  // workspaces selector state: fetched from /api/workspaces; user picks which
  // sandbox the agent should operate in (or No workspace for ephemeral sandbox)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(workspaceId || null);
  const sessionRef = useRef<string | null>(sessionStorage.getItem(SESSION_KEY));
  const lastMsgRef = useRef<string>("");
  const listRef = useRef<VirtuosoHandle>(null);
  const client = useRef(new AgentClient(settings));
  client.current = new AgentClient(settings);
  const abortRef = useRef<AbortController | null>(null);

  // discover runnable models
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
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.baseUrl]);

  // load user workspaces for the selector
  useEffect(() => {
    if (!settings.githubSessionId) return;
    let alive = true;
    fetch(`${settings.baseUrl}/api/workspaces`, {
      headers: {
        Authorization: `Bearer ${settings.githubSessionId}`,
      },
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("fetch workspaces failed"))))
      .then((data) => {
        if (alive) setWorkspaces(data.workspaces || []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [settings.baseUrl, settings.githubSessionId]);

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
        }
      }
      setStatus(snap.status);
      if (snap.status !== "running" && snap.status !== "starting") {
        if (snap.status !== "error") {
          client.current
            .files(sessionId)
            .then((f) => setFiles(f.files))
            .catch(() => {});
        }
        return cursor;
      }
      await new Promise((res) => setTimeout(res, 1500));
    }
  }

  async function send(text?: string) {
    const message = (text ?? input).trim();
    if (!message || busy) return;
    if (text === undefined) setInput("");
    lastMsgRef.current = message;
    setBusy(true);
    setError("");
    // Cancel any previous polling loop
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    // Optimistically add user message to transcript
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
      );
      sessionRef.current = start.session_id;
      sessionStorage.setItem(SESSION_KEY, start.session_id);
      const fresh = await client.current.poll(start.session_id, 0);
      // Merge: keep optimistic user message if backend hasn't echoed it yet
      setEvents((prev) => {
        const userMsg = prev[prev.length - 1];
        const hasUserMsg =
          userMsg?.type === "user" &&
          fresh.events.some((e) => e.type === "user" && e.text === userMsg.text);
        return hasUserMsg ? fresh.events : [...prev.slice(0, -1), ...fresh.events];
      });
      if (!ac.signal.aborted) {
        await pollUntilSettled(start.session_id, fresh.next);
      }
    } catch (e) {
      if (ac.signal.aborted) return;
      // Clear stale session on error so retry doesn't get stuck
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
    setStatus("idle"); // optimistic
    try {
      await client.current.interrupt(sessionRef.current);
    } catch {
      setError("Interrupt failed — the agent may still be running");
      setStatus("running");
    }
  }

  function newSession(model?: string) {
    abortRef.current?.abort();
    sessionRef.current = null;
    sessionStorage.removeItem(SESSION_KEY);
    setEvents([]);
    setFiles([]);
    setStatus("idle");
    setError("");
    setCost(null);
    setBusy(false);
    if (model) {
      setSelected(model);
      localStorage.setItem(MODEL_KEY, model);
    }
  }

  const running = status === "running" || status === "starting";

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted">
        <button
          onClick={() => !running && openOverlay()}
          disabled={running}
          className="flex items-center gap-1.5 px-2 py-1 rounded-lg border border-border hover:border-accent transition-colors text-[12px] disabled:opacity-50 max-w-[55%]"
        >
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: selectedProviderColor || "#5b8cff" }} />
          <span className="text-accent truncate">{selectedModelLabel || "Select model"}</span>
        </button>
        {workspaces.length > 0 && (
          <select
            value={selectedWorkspace || ""}
            onChange={(e) => setSelectedWorkspace(e.target.value || null)}
            disabled={running}
            className="bg-surface border border-border rounded-lg px-2 py-1 text-[12px] text-accent outline-none focus:border-accent disabled:opacity-50 max-w-[40%]"
          >
            <option value="">(sandbox)</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.title || w.id}
              </option>
            ))}
          </select>
        )}
        {cost != null && <span className="text-[10px]">${cost.toFixed(4)}</span>}
        <span className="ml-auto capitalize">{status}</span>
        {running ? (
          <button onClick={stop} className="text-rose-300 underline">
            stop
          </button>
        ) : (
          <button onClick={() => newSession()} className="text-accent underline">
            new
          </button>
        )}
      </div>

      <Virtuoso
        ref={listRef}
        className="flex-1"
        data={events}
        followOutput="smooth"
        itemContent={(_, ev) => <EventRow ev={ev} />}
        components={{
          Footer: () =>
            events.length === 0 ? (
              <div className="text-center text-muted text-sm mt-20 px-6">
                Ask the agent to build, edit, run, or pack something. It works in a
                private workspace inside your Space — files it creates appear below to
                download. The workspace is temporary, so save what you need.
              </div>
            ) : (
              <div className="h-2" />
            ),
        }}
      />

      {files.length > 0 && (
        <div className="border-t border-border px-3 py-2 max-h-32 overflow-y-auto">
          <div className="text-[11px] text-muted mb-1">artifacts (temporary — download to keep)</div>
          <div className="flex flex-wrap gap-1.5">
            {files.map((f) => (
              <button
                key={f.path}
                onClick={() => client.current.download(sessionRef.current!, f.path).catch(() => {})}
                className="text-[11px] px-2 py-1 rounded-lg border border-border hover:border-accent"
              >
                ↓ {f.path} <span className="text-muted">({fmtSize(f.size)})</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="border-t border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300 flex items-center gap-2">
          <span className="flex-1">{error}</span>
          {lastMsgRef.current && (
            <button
              onClick={() => send(lastMsgRef.current)}
              className="px-2 py-1 rounded-lg border border-rose-400/40 text-xs"
            >
              Retry
            </button>
          )}
        </div>
      )}

      <div className="border-t border-border bg-bg px-2 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={1}
            placeholder="Tell the agent what to do…"
            className="flex-1 resize-none bg-surface border border-border rounded-2xl px-3 py-2 text-[15px] outline-none focus:border-accent max-h-32"
          />
          <button
            onClick={() => (running ? stop() : send())}
            disabled={!running && (busy || !input.trim())}
            className={`h-10 px-4 rounded-2xl font-medium disabled:opacity-40 ${
              running ? "bg-rose-500/80 text-white" : "bg-accent text-white"
            }`}
          >
            {running ? "Stop" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const TOOL_ICONS: Record<string, string> = {
  bash: "⌘",
  shell: "⌘",
  write: "✎",
  edit: "✎",
  fileeditor: "✎",
  str_replace: "✎",
  read: "👁",
  view: "👁",
  glob: "🔍",
  grep: "🔍",
  search: "🔍",
  web: "🌐",
  fetch: "🌐",
};

function toolIcon(name: string): string {
  const k = name.toLowerCase();
  for (const key of Object.keys(TOOL_ICONS)) if (k.includes(key)) return TOOL_ICONS[key];
  return "⚙";
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
        <div className="text-[15px] whitespace-pre-wrap">{ev.text}</div>
      </div>
    );
  }
  if (ev.type === "thinking") {
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <Collapsible label="thinking">{ev.text}</Collapsible>
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
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <Collapsible label={ev.is_error ? "result (error)" : "result"} error={ev.is_error}>
          {ev.text}
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
  return null;
}

function Collapsible({
  label,
  children,
  error,
}: {
  label: string;
  children: React.ReactNode;
  error?: boolean;
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
        <pre className="px-2 pb-2 text-[11px] font-mono whitespace-pre-wrap break-words overflow-x-auto">
          {children}
        </pre>
      )}
    </div>
  );
}
