/**
 * QueueMonitor — collapsible panel showing live job activity + suggestions.
 *
 * Three sections:
 *  - Suggestions (z.ai-style): follow-up actions sorted by importance +
 *    creativity. Clickable to fill the input box.
 *  - Active jobs: queued + running, with live stage updates and a cancel
 *    button.
 *  - Recent jobs: complete or error, expandable to show merged output /
 *    suggestions / error message.
 *
 * Driven by the chatStore's `jobs` and `suggestions` arrays, which are fed by
 * the /api/monitor SSE stream.
 */
import { useState } from "react";
import type { MonitorJob, MonitorSuggestion } from "../api/agent";

interface QueueMonitorProps {
  open: boolean;
  onClose: () => void;
  jobs: MonitorJob[];
  suggestions: MonitorSuggestion[];
  onApplySuggestion: (id: string) => void;
  onCancelJob: (jobId: string) => void;
}

export function QueueMonitor({
  open,
  onClose,
  jobs,
  suggestions,
  onApplySuggestion,
  onCancelJob,
}: QueueMonitorProps) {
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  const recent = jobs.filter((j) => j.status === "complete" || j.status === "error");
  // Sort recent by ended_at desc (most recent first).
  const recentSorted = [...recent].sort(
    (a, b) => (b.ended_at || 0) - (a.ended_at || 0),
  );

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/30"
          onClick={onClose}
        />
      )}
      {/* Panel — slides in from the right. */}
      <div
        className={`fixed top-0 right-0 z-40 h-full w-[360px] max-w-[88vw] bg-bg border-l border-border shadow-2xl transition-transform duration-300 flex flex-col ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between px-3 h-11 border-b border-border shrink-0">
          <div className="flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-accent">
              <line x1="8" y1="6" x2="21" y2="6" />
              <line x1="8" y1="12" x2="21" y2="12" />
              <line x1="8" y1="18" x2="21" y2="18" />
              <line x1="3" y1="6" x2="3.01" y2="6" />
              <line x1="3" y1="12" x2="3.01" y2="12" />
              <line x1="3" y1="18" x2="3.01" y2="18" />
            </svg>
            <span className="text-[13px] font-medium">Queue Monitor</span>
            {active.length > 0 && (
              <span className="text-[9px] font-mono px-1.5 py-0.5 rounded-full bg-accent/15 text-accent">
                {active.length} active
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground p-1.5 rounded-xl hover:bg-surface2 transition-colors"
            title="Close"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {/* Suggestions */}
          {suggestions.length > 0 && (
            <section className="border-b border-border p-2.5">
              <div className="flex items-center gap-1.5 px-1.5 pb-2">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-400/80">
                  <path d="M12 2v2" /><path d="M12 20v2" /><path d="m4.93 4.93 1.41 1.41" /><path d="m17.66 17.66 1.41 1.41" /><path d="M2 12h2" /><path d="M20 12h2" /><path d="m6.34 17.66-1.41 1.41" /><path d="m19.07 4.93-1.41 1.41" />
                  <circle cx="12" cy="12" r="4" />
                </svg>
                <span className="text-[9.5px] uppercase tracking-wide text-muted-foreground/70 font-medium">
                  Suggestions
                </span>
              </div>
              <div className="space-y-1">
                {suggestions.map((s) => (
                  <SuggestionRow
                    key={s.id}
                    suggestion={s}
                    onApply={() => onApplySuggestion(s.id)}
                  />
                ))}
              </div>
            </section>
          )}

          {/* Active jobs */}
          <section className="border-b border-border p-2.5">
            <div className="flex items-center justify-between px-1.5 pb-2">
              <span className="text-[9.5px] uppercase tracking-wide text-muted-foreground/70 font-medium">
                Active Jobs
              </span>
              {active.length > 0 && (
                <span className="text-[9.5px] text-muted-foreground/50 tabular-nums">
                  {active.length}
                </span>
              )}
            </div>
            {active.length === 0 ? (
              <p className="text-[11.5px] text-muted-foreground/50 px-1.5 py-3 text-center italic">
                No active jobs. Send a message or run a template to see activity here.
              </p>
            ) : (
              <div className="space-y-1.5">
                {active.map((job) => (
                  <ActiveJobRow key={job.id} job={job} onCancel={() => onCancelJob(job.id)} />
                ))}
              </div>
            )}
          </section>

          {/* Recent jobs */}
          <section className="p-2.5">
            <div className="flex items-center justify-between px-1.5 pb-2">
              <span className="text-[9.5px] uppercase tracking-wide text-muted-foreground/70 font-medium">
                Recent Jobs
              </span>
              {recentSorted.length > 0 && (
                <span className="text-[9.5px] text-muted-foreground/50 tabular-nums">
                  {recentSorted.length}
                </span>
              )}
            </div>
            {recentSorted.length === 0 ? (
              <p className="text-[11.5px] text-muted-foreground/50 px-1.5 py-3 text-center italic">
                No completed jobs yet.
              </p>
            ) : (
              <div className="space-y-1">
                {recentSorted.map((job) => (
                  <RecentJobRow key={job.id} job={job} />
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  );
}

/** One suggestion row — clicking fills the input box. */
function SuggestionRow({
  suggestion,
  onApply,
}: {
  suggestion: MonitorSuggestion;
  onApply: () => void;
}) {
  // Creativity label: low (≤3), med (≤7), high (>7).
  const creativity =
    suggestion.creativity > 7 ? "creative" : suggestion.creativity > 3 ? "balanced" : "practical";
  const kindIcon = kindToIcon(suggestion.kind);
  return (
    <button
      onClick={onApply}
      className="w-full text-left px-2 py-1.5 rounded-xl hover:bg-surface2 transition-colors group flex items-start gap-2"
      title="Click to fill the input box"
    >
      <span className="text-[11px] mt-0.5 shrink-0">{kindIcon}</span>
      <span className="flex-1 text-[11.5px] text-muted-foreground group-hover:text-foreground leading-relaxed">
        {suggestion.text}
      </span>
      <span className="text-[9px] text-muted-foreground/50 shrink-0 mt-0.5 capitalize">
        {creativity}
      </span>
    </button>
  );
}

function kindToIcon(kind?: string): string {
  switch (kind) {
    case "followup":
      return "↪";
    case "action":
      return "⚡";
    case "question":
      return "?";
    case "template":
      return "📄";
    default:
      return "•";
  }
}

/** Active job row with live stage + cancel. */
function ActiveJobRow({ job, onCancel }: { job: MonitorJob; onCancel: () => void }) {
  const pct = typeof job.progress === "number" ? Math.round(job.progress * 100) : null;
  const stage = job.stage ? stageLabel(job.stage) : job.status === "queued" ? "Queued" : "Working…";
  return (
    <div className="rounded-xl border border-border bg-surface/40 overflow-hidden">
      <div className="flex items-center gap-2 px-2.5 py-2">
        <span className="size-1.5 rounded-full bg-accent animate-pulse shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-[11px] text-foreground truncate font-medium">{job.prompt}</div>
          <div className="text-[9.5px] text-muted-foreground/70 mt-0.5 flex items-center gap-2">
            <span className="capitalize">{stage}</span>
            {job.model && <span className="opacity-60">· {job.model.split("/").pop()}</span>}
            {pct != null && <span className="opacity-60 tabular-nums">· {pct}%</span>}
          </div>
        </div>
        <button
          onClick={onCancel}
          className="text-[9.5px] px-2 py-1 rounded border border-border hover:border-red-500/40 hover:text-red-300 transition-colors shrink-0"
          title="Cancel job"
        >
          Cancel
        </button>
      </div>
      {pct != null && (
        <div className="h-0.5 bg-surface2">
          <div
            className="h-full bg-accent transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

function stageLabel(stage: string): string {
  if (stage === "searching") return "Searching the web…";
  if (stage === "synthesizing") return "Synthesizing…";
  if (stage.startsWith("reading:")) {
    const n = stage.slice("reading:".length);
    return `Reading ${n} source${n === "1" ? "" : "s"}…`;
  }
  if (stage === "judge:running") return "Judge panel running…";
  if (stage === "cancelled") return "Cancelled";
  return stage;
}

/** Recent job row — expandable to show merged output / error. */
function RecentJobRow({
  job,
}: {
  job: MonitorJob;
}) {
  const [open, setOpen] = useState(false);
  const ok = job.status === "complete";
  const elapsed = job.ended_at && job.started_at ? Math.round((job.ended_at - job.started_at) / 1000) : null;
  return (
    <div className="rounded-xl border border-border overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-2.5 py-2 hover:bg-surface transition-colors"
      >
        <span
          className={`size-1.5 rounded-full shrink-0 ${
            ok ? "bg-emerald-400" : "bg-rose-400"
          }`}
        />
        <span className="text-[11px] text-foreground truncate flex-1 text-left font-medium">
          {job.prompt}
        </span>
        <span className="text-[9px] text-muted-foreground/60 shrink-0 tabular-nums">
          {ok ? "done" : "error"}
          {elapsed != null && ` · ${elapsed}s`}
        </span>
        <svg
          width="9"
          height="9"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`text-muted-foreground/60 transition-transform duration-150 shrink-0 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>
      {open && (
        <div className="px-2.5 pb-2.5 pt-1 border-t border-border/50 space-y-1.5 text-[10.5px]">
          {job.stage && !ok && (
            <p className="text-rose-300">{job.stage}</p>
          )}
          {job.model && (
            <p className="text-muted-foreground/70">
              Model: <span className="font-mono">{job.model}</span>
            </p>
          )}
          {typeof job.cost_usd === "number" && job.cost_usd > 0 && (
            <p className="text-amber-400/80 font-mono">
              Cost: ${job.cost_usd.toFixed(4)}
            </p>
          )}
          {typeof job.cost_usd === "number" && job.cost_usd === 0 && (
            <p className="text-emerald-400/80">Cost: free</p>
          )}
          {/* Suggestions attached to a completed job get their own click-to-fill rows. */}
        </div>
      )}
    </div>
  );
}
