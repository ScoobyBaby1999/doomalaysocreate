import { useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { PanelClient, type Settings, type PanelSnapshot, type Effort, type Privacy } from "../api/panel";
import { JudgeCard } from "../components/JudgeCard";

interface Turn {
  id: string;
  prompt: string;
  snapshot?: PanelSnapshot;
  error?: string;
}

const EFFORTS: Effort[] = ["low", "med", "high", "max"];

/** F0 chat: send a prompt to the panel, watch every frontier judge stream in parallel. */
export function Chat({ settings }: { settings: Settings }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [effort, setEffort] = useState<Effort>("med");
  const [privacy] = useState<Privacy>("strict");
  const listRef = useRef<VirtuosoHandle>(null);

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setInput("");
    setBusy(true);
    const id = crypto.randomUUID();
    setTurns((t) => [...t, { id, prompt }]);
    const client = new PanelClient(settings);
    try {
      await client.runPanel(
        { input: prompt, role: "critiquer", effort, privacy, profile: "app" },
        (snap) =>
          setTurns((t) => t.map((x) => (x.id === id ? { ...x, snapshot: snap } : x))),
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, error: msg } : x)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <Virtuoso
        ref={listRef}
        className="flex-1"
        data={turns}
        followOutput="smooth"
        itemContent={(_, turn) => (
          <div className="px-3 py-2 space-y-2 max-w-2xl mx-auto">
            <div className="flex justify-end">
              <div className="rounded-2xl rounded-br-sm bg-surface2 px-3 py-2 text-[15px] max-w-[85%] whitespace-pre-wrap">
                {turn.prompt}
              </div>
            </div>
            {turn.error && (
              <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
                {turn.error}
              </div>
            )}
            {turn.snapshot && (
              <div className="space-y-2">
                <div className="text-[11px] text-muted">
                  {turn.snapshot.meta.judges_settled}/{turn.snapshot.meta.judges_total} settled ·{" "}
                  {turn.snapshot.meta.age_s}s
                </div>
                {turn.snapshot.judges.map((j) => (
                  <JudgeCard key={j.model} judge={j} />
                ))}
              </div>
            )}
          </div>
        )}
        components={{
          Footer: () =>
            turns.length === 0 ? (
              <div className="text-center text-muted text-sm mt-20 px-6">
                Ask the panel anything. Every frontier model answers in parallel — watch them think.
              </div>
            ) : (
              <div className="h-2" />
            ),
        }}
      />
      <div className="border-t border-border bg-bg px-2 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
        <div className="flex items-center gap-1 mb-2 px-1">
          {EFFORTS.map((e) => (
            <button
              key={e}
              onClick={() => setEffort(e)}
              className={`text-[11px] px-2 py-0.5 rounded-full border ${
                effort === e ? "border-accent text-accent" : "border-border text-muted"
              }`}
            >
              {e}
            </button>
          ))}
          <span className="ml-auto text-[11px] text-muted">privacy: {privacy}</span>
        </div>
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
            placeholder="Message the panel…"
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
