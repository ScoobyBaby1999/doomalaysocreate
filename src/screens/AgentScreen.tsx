import { useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { Settings } from "../api/panel";
import { AgentClient, type AgentEvent, type AgentFile, type AgentStatus } from "../api/agent";

const SESSION_KEY = "loom.agent.session";

/** The agent tab: a chat with the orchestrator running inside the user's Space.
 *  Tier (Claude vs the open SDK) is chosen by the backend from available keys. */
export function AgentScreen({ settings }: { settings: Settings }) {
  const [tier, setTier] = useState<"claude" | "open" | "mock" | null | undefined>(undefined);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [files, setFiles] = useState<AgentFile[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sessionRef = useRef<string | null>(sessionStorage.getItem(SESSION_KEY));
  const listRef = useRef<VirtuosoHandle>(null);
  const client = useRef(new AgentClient(settings));
  client.current = new AgentClient(settings);

  // discover which tier this Space offers
  useEffect(() => {
    let alive = true;
    fetch(settings.baseUrl + "/health", { cache: "no-store" })
      .then((r) => r.json())
      .then((h) => alive && setTier((h.agent ?? null) as typeof tier))
      .catch(() => alive && setTier(null));
    return () => {
      alive = false;
    };
  }, [settings.baseUrl]);

  async function pollUntilSettled(sessionId: string, since: number) {
    let cursor = since;
    for (;;) {
      const snap = await client.current.poll(sessionId, cursor);
      if (snap.events.length) {
        setEvents((prev) => [...prev, ...snap.events]);
        cursor = snap.next;
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

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setError("");
    // optimistic echo so the user sees their message immediately
    setEvents((prev) => [
      ...prev,
      { i: -1, ts: Date.now() / 1000, type: "user", text: message } as AgentEvent,
    ]);
    setStatus("running");
    try {
      const start = await client.current.send(message, sessionRef.current ?? undefined);
      sessionRef.current = start.session_id;
      sessionStorage.setItem(SESSION_KEY, start.session_id);
      setTier(start.tier);
      // replace the optimistic echo by reloading the authoritative transcript
      const fresh = await client.current.poll(start.session_id, 0);
      setEvents(fresh.events);
      await pollUntilSettled(start.session_id, fresh.next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus("error");
    } finally {
      setBusy(false);
    }
  }

  function newSession() {
    sessionRef.current = null;
    sessionStorage.removeItem(SESSION_KEY);
    setEvents([]);
    setFiles([]);
    setStatus("idle");
    setError("");
  }

  if (tier === null) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-6 text-center space-y-3">
        <p className="text-sm font-medium">The agent isn't set up yet</p>
        <p className="text-sm text-muted max-w-xs">
          Add an <span className="text-accent">Anthropic key</span> in Settings for the
          Claude agent, or any free provider key (Groq, NVIDIA, OpenRouter, Google) for
          the open-source agent.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-8 border-b border-border text-[11px] text-muted">
        <span>
          agent:{" "}
          <span className="text-accent">
            {tier === "claude" ? "Claude" : tier === "open" ? "open SDK" : tier ?? "…"}
          </span>
        </span>
        <span className="ml-auto capitalize">{status}</span>
        <button onClick={newSession} className="text-accent underline">
          new session
        </button>
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
                title={`${f.size} bytes`}
              >
                ↓ {f.path}
              </button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="border-t border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
          {error}
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
            onClick={send}
            disabled={busy || !input.trim()}
            className="h-10 px-4 rounded-2xl bg-accent text-white font-medium disabled:opacity-40"
          >
            {busy ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
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
        <Collapsible label="thinking" muted>
          {ev.text}
        </Collapsible>
      </div>
    );
  }
  if (ev.type === "tool_use") {
    return (
      <div className="px-3 py-1 max-w-2xl mx-auto">
        <div className="text-[12px] text-muted">
          <span className="text-accent">⚙ {ev.name}</span>
          {ev.summary ? <span className="ml-2 font-mono">{ev.summary}</span> : null}
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
  return null;
}

function Collapsible({
  label,
  children,
  muted,
  error,
}: {
  label: string;
  children: React.ReactNode;
  muted?: boolean;
  error?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`rounded-lg border ${error ? "border-rose-500/30" : "border-border"}`}>
      <button
        onClick={() => setOpen((o) => !o)}
        className={`w-full text-left text-[11px] px-2 py-1 ${
          error ? "text-rose-300" : muted ? "text-muted" : "text-muted"
        }`}
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
