/**
 * ContextCircle — a 44px radial gauge that shows how much of the selected
 * model's context window is in use.
 *
 * Visual spec:
 *  - 44px diameter (was 16px in the original)
 *  - Centered percentage label
 *  - Color: green (≤60%) → amber (60–85%) → red (>85%)
 *  - Pulses at ≥100%
 *
 * The component takes the model's REAL contextLength (from the roster) and the
 * number of tokens used in the current turn. It does NOT hardcode 128k.
 */
interface ContextCircleProps {
  used: number;
  max: number;
  /** Optional title for the hover tooltip. */
  title?: string;
}

export function ContextCircle({ used, max, title }: ContextCircleProps) {
  const safeMax = max > 0 ? max : 1;
  const pct = Math.min(1, Math.max(0, used / safeMax));
  const pctNum = Math.round(pct * 100);

  // Color thresholds: green → amber → red.
  const color =
    pct >= 0.85 ? "#ef4444" : pct >= 0.6 ? "#f59e0b" : "#22c55e";
  const trackColor = "rgba(255,255,255,0.08)";

  // SVG geometry — radius 18, circumference 2πr ≈ 113.1
  const r = 18;
  const circumference = 2 * Math.PI * r;
  const dash = pct * circumference;

  const atMax = pct >= 1;
  const tooltip =
    title ||
    `Context: ${used.toLocaleString()} / ${max.toLocaleString()} tokens (${pctNum}%)`;

  return (
    <div
      className={`relative shrink-0 cursor-help ${atMax ? "animate-pulse" : ""}`}
      style={{ width: 44, height: 44 }}
      title={tooltip}
    >
      <svg
        width="44"
        height="44"
        viewBox="0 0 44 44"
        className="-rotate-90"
        style={{ transform: "rotate(-90deg)" }}
      >
        <circle
          cx="22"
          cy="22"
          r={r}
          fill="none"
          stroke={trackColor}
          strokeWidth="3.5"
        />
        <circle
          cx="22"
          cy="22"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="3.5"
          strokeDasharray={`${dash} ${circumference}`}
          strokeLinecap="round"
          style={{ transition: "stroke-dasharray 0.4s ease, stroke 0.3s ease" }}
        />
      </svg>
      {/* Centered percentage label */}
      <div
        className="absolute inset-0 flex items-center justify-center font-mono tabular-nums"
        style={{ color, fontSize: 11, fontWeight: 600, lineHeight: 1 }}
      >
        {pctNum}
      </div>
      {atMax && (
        <span
          className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-red-500 animate-pulse"
          aria-hidden
        />
      )}
    </div>
  );
}
