import type { Judge } from "../api/panel";
import { Markdown } from "./Markdown";

const DOT: Record<string, string> = {
  pending: "bg-muted",
  running: "bg-accent animate-pulse",
  done: "bg-emerald-400",
  error: "bg-rose-400",
};

/** One frontier judge: header with live status + (while running) the "watch it think"
 *  counters and tail, then its full output once settled. */
export function JudgeCard({ judge }: { judge: Judge }) {
  const status = judge.status ?? (judge.ok === undefined ? "pending" : judge.ok ? "done" : "error");
  const body = judge.critique ?? judge.output ?? "";
  const pg = judge.progress;
  return (
    <div className="rounded-xl border border-border bg-surface overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <span className={`h-2 w-2 rounded-full ${DOT[status] ?? "bg-muted"}`} />
        <span className="font-medium text-sm">{judge.model}</span>
        {judge.routed_to && (
          <span className="text-[11px] text-muted truncate">→ {judge.routed_to.split("/")[0]}</span>
        )}
        {judge.cached && <span className="text-[11px] text-emerald-400">cached</span>}
        <span className="ml-auto text-[11px] text-muted">
          {status === "running" && pg
            ? `${pg.content_chars}c${pg.reasoning_chars ? ` · ${pg.reasoning_chars}r` : ""}`
            : judge.elapsed_s != null
              ? `${judge.elapsed_s}s`
              : status}
        </span>
      </div>
      <div className="px-3 py-2">
        {status === "error" ? (
          <p className="text-sm text-rose-300">{judge.error}</p>
        ) : body ? (
          <Markdown text={body} />
        ) : status === "running" && pg?.tail ? (
          <p className="text-[13px] text-muted font-mono whitespace-pre-wrap">…{pg.tail}</p>
        ) : (
          <p className="text-sm text-muted">{status === "pending" ? "queued" : "thinking…"}</p>
        )}
        {judge.coverage && (
          <p className="mt-1 text-[11px] text-muted">
            coverage: {judge.coverage.files_included}/{judge.coverage.files_total} files
          </p>
        )}
      </div>
    </div>
  );
}
