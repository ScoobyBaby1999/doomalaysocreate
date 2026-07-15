/**
 * PriceGauge — a tiny badge showing the current message cost alongside the
 * running session total. Uses REAL cost data from the API:
 *
 *  - `currentCost` — the cost of the most recent assistant turn (from the
 *    `cost_usd` field on the message bubble).
 *  - `totalCost` — the session-wide running total (from the `cost` state on
 *    the chat store, which the backend reports on each status event).
 *
 * Free models (totalCost === 0 AND a free flag) render "FREE" instead of $0.
 * Format: `$0.003 / $0.015`  (current / total).
 */
interface PriceGaugeProps {
  /** Cost of the most recent assistant turn (USD). */
  currentCost?: number | null;
  /** Running session-wide total cost (USD). */
  totalCost?: number | null;
  /** True when the selected model is free (e.g. :free suffix). When true and
   *  the total is 0, the gauge renders "FREE". */
  isFree?: boolean;
  /** Compact mode — single line, no badge background. */
  compact?: boolean;
}

function formatUsd(v: number): string {
  // 4 decimal places for micro-costs, but trim trailing zeros beyond 4.
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(3)}`;
}

export function PriceGauge({
  currentCost,
  totalCost,
  isFree,
  compact,
}: PriceGaugeProps) {
  const cur = typeof currentCost === "number" ? currentCost : 0;
  const tot = typeof totalCost === "number" ? totalCost : 0;
  const showFree = !!isFree && tot === 0 && cur === 0;

  if (compact) {
    return (
      <span
        className="text-[10px] tabular-nums font-mono text-amber-400/80 cursor-help"
        title={`Current turn: ${formatUsd(cur)}\nSession total: ${formatUsd(tot)}`}
      >
        {showFree ? "FREE" : `${formatUsd(cur)} / ${formatUsd(tot)}`}
      </span>
    );
  }

  return (
    <div
      className="flex items-center gap-1.5 px-2 py-1 rounded-full bg-surface2 border border-border text-[10px] font-mono tabular-nums cursor-help shrink-0"
      title={
        showFree
          ? "Free model — no cost"
          : `Current turn: ${formatUsd(cur)}\nSession total: ${formatUsd(tot)}`
      }
    >
      {/* Coin glyph */}
      <svg
        width="10"
        height="10"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={showFree ? "text-emerald-400" : "text-amber-400/80"}
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M12 6v12" />
        <path d="M15 9.5c-.5-1-1.5-1.5-3-1.5s-3 .5-3 2 1.5 1.5 3 2 3 .5 3 2-1.5 2-3 2-2.5-.5-3-1.5" />
      </svg>
      {showFree ? (
        <span className="text-emerald-400 font-medium">FREE</span>
      ) : (
        <span className="flex items-center gap-1">
          <span className="text-amber-400/80">{formatUsd(cur)}</span>
          <span className="text-muted-foreground/40">/</span>
          <span className="text-amber-400/60">{formatUsd(tot)}</span>
        </span>
      )}
    </div>
  );
}
