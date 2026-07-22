import { useState } from "react";
import type { PanelSnapshot } from "../api/panel";
import { JudgeCard } from "./JudgeCard";

interface PanelInvocation {
  task_name: string;
  invoke_id: string;
  prompt: string;
  status?: string;
  panel?: string[];
  snapshot?: PanelSnapshot;
  error?: string;
}

export function PanelDrawer({
  open,
  onClose,
  invocations,
}: {
  open: boolean;
  onClose: () => void;
  invocations: PanelInvocation[];
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const toggle = (id: string) =>
    setExpanded((p) => ({ ...p, [id]: !p[id] }));

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-[60] bg-black/30 backdrop-blur-sm"
          onClick={onClose}
        />
      )}
      <div
        className={`fixed top-0 right-0 z-[70] h-full w-[380px] max-w-[85vw] bg-bg border-l border-border shadow-2xl transition-transform duration-300 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between px-3 h-10 border-b border-border">
          <span className="text-sm font-medium">Panel Invocations</span>
          <button onClick={onClose} className="text-muted hover:text-accent text-sm px-1">
            ✕
          </button>
        </div>
        <div className="overflow-y-auto h-[calc(100%-40px)]">
          {invocations.length === 0 ? (
            <div className="text-center text-muted text-[13px] mt-8 px-4">
              No panel invocations yet. The agent will use the panel tool when it needs critiques.
            </div>
          ) : (
            <div className="space-y-1 p-2">
              {invocations.map((inv) => (
                <div
                  key={inv.invoke_id}
                  className="rounded-xl border border-border overflow-hidden"
                >
                  <button
                    onClick={() => toggle(inv.invoke_id)}
                    className="w-full flex items-center gap-2 px-3 py-2 text-left text-[12px] hover:bg-surface transition-colors"
                  >
                    <span className="text-muted shrink-0">
                      {expanded[inv.invoke_id] ? "▾" : "▸"}
                    </span>
                    <span className="font-medium truncate">{inv.task_name}</span>
                    {inv.snapshot && (
                      <span className="ml-auto text-muted shrink-0 text-[11px]">
                        {inv.snapshot.meta.judges_settled}/{inv.snapshot.meta.judges_total}
                      </span>
                    )}
                  </button>
                  {expanded[inv.invoke_id] && (
                    <div className="px-3 pb-3 space-y-2">
                      <p className="text-[11px] text-muted leading-relaxed line-clamp-3">
                        {inv.prompt}
                      </p>
                      {inv.error ? (
                        <div className="text-[12px] text-rose-300">{inv.error}</div>
                      ) : inv.snapshot ? (
                        <>
                          <div className="text-[11px] text-muted">
                            {inv.snapshot.meta.judges_settled}/{inv.snapshot.meta.judges_total} settled ·
                            {inv.snapshot.meta.age_s}s
                          </div>
                          <div className="space-y-1.5">
                            {inv.snapshot.judges.map((j) => (
                              <JudgeCard key={j.model} judge={j} />
                            ))}
                          </div>
                        </>
                      ) : (
                        <div className="text-[12px] text-muted">queued…</div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export type { PanelInvocation };
