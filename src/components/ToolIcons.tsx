/**
 * ToolIcons — unified toolbar of popover-driven tools for the chat input area.
 *
 * Popovers:
 *  - Effort (Gauge icon): low/med/high/max. Dims to 40% opacity if the
 *    selected model's capabilities don't include "effort". Descriptions only
 *    — judge counts moved to the Judge popover.
 *  - Web Search (Globe icon): "Regular Web Search" + "Browse Templates…"
 *    (opens the TemplateLibrary overlay filtered to kind=websearch).
 *    Dims if the model doesn't support webSearch.
 *  - Deep Research (Telescope icon): Default + ReAct Loop + Extended Thinking
 *    + "Browse Templates…" (filtered to kind=deepresearch). Dims if the model
 *    doesn't support deepResearch/extendedThinking.
 *  - Judge (Gavel icon): SEPARATE icon with judge count (1-6 buttons) +
 *    4 quick-pick templates (Critique/Verify/Improve/Debate) +
 *    "Browse Templates…" (filtered to kind=judge) + Run button. Fires
 *    onRunJudge(input?) when the user clicks Run.
 *
 * All popovers close on outside-click via a fixed-position overlay.
 */
import { useState, useEffect, useRef } from "react";
import { Popover as PopoverPortal } from "./Popover";

type PopoverKind = "effort" | "web" | "deep" | "judge" | null;

export interface ToolIconsProps {
  // Selection state
  effort: "low" | "med" | "high" | "max";
  webSearch: boolean;
  deepResearch: boolean;
  /** Accepts legacy IDs ("breadth" | "deepdive" | "compare" | "factcheck") OR
   *  a template library template id. */
  webTemplate: string;
  /** Accepts legacy IDs ("react" | "extended") OR a template library id. */
  deepTemplate: string;
  judge: { count: number; template: string };

  // Capabilities — derived from the selected model's `capabilities` array.
  // When a capability is false/missing, the corresponding icon dims to 40%.
  capabilities: {
    effort: boolean;
    webSearch: boolean;
    deepResearch: boolean;
    extendedThinking: boolean;
  };

  // Disabled while a turn is running (popovers still open but values can't change).
  disabled?: boolean;

  // Setters
  setEffort: (e: "low" | "med" | "high" | "max") => void;
  toggleWebSearch: () => void;
  toggleDeepResearch: () => void;
  setWebTemplate: (t: string) => void;
  setDeepTemplate: (t: string) => void;
  setJudge: (cfg: Partial<{ count: number; template: string }>) => void;

  /** Fire the judge panel. If no input is provided, the store uses inputText. */
  onRunJudge: (input?: string) => void;

  /** Open the TemplateLibrary overlay pre-filtered by kind
   *  ("websearch" | "deepresearch" | "judge" | "chat" | "custom" | ""). */
  onOpenTemplateLibrary: (kind: string) => void;
}

/** Effort level descriptions — kept in sync with the backend's effort budget
 *  table (lib/research_templates.py EFFORT_BUDGETS). NO judge counts here —
 *  those moved to the Judge popover. */
const EFFORT_DESCRIPTIONS: Record<"low" | "med" | "high" | "max", string> = {
  low: "Quick answer, light reasoning. Minimal thinking budget.",
  med: "Balanced — the default. Moderate reasoning.",
  high: "Deep reasoning, longer answer. More thinking steps.",
  max: "Maximum reasoning budget. Slowest, most thorough.",
};

const DEEP_TEMPLATES: {
  id: "react" | "extended";
  label: string;
  description: string;
}[] = [
  { id: "react", label: "ReAct Loop", description: "Reason → Act → Observe loop. Best for tool-heavy multi-step research" },
  { id: "extended", label: "Extended Thinking", description: "Long silent reasoning before responding. Best for hard problems" },
];

// BATCH-2 Task 5.10 — JUDGE_TEMPLATES quick-picks removed from the judge
// popover. Templates now flow from the Template Library exclusively
// (Browse Templates… button). Kept the legacy IDs here for backwards-compat
// with the store's `judge.template` default ("critique") — they're the
// legacy template IDs the backend still understands.
const LEGACY_JUDGE_TEMPLATE_IDS = ["critique", "verify", "improve", "debate"] as const;

export function ToolIcons({
  effort,
  webSearch,
  deepResearch,
  webTemplate,
  deepTemplate,
  judge,
  capabilities,
  disabled,
  setEffort,
  toggleWebSearch,
  toggleDeepResearch,
  setWebTemplate,
  setDeepTemplate,
  setJudge,
  onRunJudge,
  onOpenTemplateLibrary,
}: ToolIconsProps) {
  const [active, setActive] = useState<PopoverKind>(null);
  // Judge popover tracks the in-progress count/template separately so the
  // user can change them without immediately firing.
  const [judgeCount, setJudgeCount] = useState(judge.count);
  const [judgeTemplate, setJudgeTemplate] = useState(judge.template);
  const judgeBtnRef = useRef<HTMLButtonElement>(null);
  // Each tool button gets its own ref so the portal popover can anchor to it.
  const effortBtnRef = useRef<HTMLButtonElement>(null);
  const webBtnRef = useRef<HTMLButtonElement>(null);
  const deepBtnRef = useRef<HTMLButtonElement>(null);

  // Sync local judge state when the prop changes (e.g. resetTools)
  useEffect(() => {
    setJudgeCount(judge.count);
    setJudgeTemplate(judge.template);
  }, [judge.count, judge.template]);

  // Close on Escape — the portal Popover handles its own Escape, but we
  // keep this for the case where active is set but no popover renders yet.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setActive(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);

  const effortActive = capabilities.effort;
  const webActive = capabilities.webSearch;
  const deepActive = capabilities.deepResearch || capabilities.extendedThinking;

  const btn = (k: Exclude<PopoverKind, null>) =>
    `touch-target w-9 h-9 rounded-xl transition-colors ${
      active === k
        ? "text-accent bg-accent/15"
        : "text-muted-foreground hover:text-foreground hover:bg-surface2"
    }`;

  // Helper to determine if a custom template (non-legacy) is currently
  // selected — used to render the "Using: <id>" affordance.
  const isCustomWebTemplate = webTemplate && !["breadth", "deepdive", "compare", "factcheck"].includes(webTemplate);
  const isCustomDeepTemplate = deepTemplate && !["react", "extended"].includes(deepTemplate);
  const isCustomJudgeTemplate = judge.template && !LEGACY_JUDGE_TEMPLATE_IDS.includes(judge.template as any);

  return (
    <div className="flex items-center gap-0.5 shrink-0 mb-0.5">
      {/* ─── Effort ─────────────────────────────────────────── */}
      <div className="relative">
        <button
          ref={effortBtnRef}
          onClick={() => !disabled && setActive(active === "effort" ? null : "effort")}
          className={`${btn("effort")} ${!effortActive ? "opacity-40" : ""}`}
          title={`Effort: ${effort}${!effortActive ? " (not supported by this model)" : ""}`}
          aria-label={`Effort: ${effort}${!effortActive ? " (not supported)" : ""}`}
          disabled={disabled && active !== "effort"}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
          </svg>
        </button>
        <PopoverPortal
          open={active === "effort"}
          onClose={() => setActive(null)}
          anchorRef={effortBtnRef}
          align="left"
          direction="up"
          width={240}
          title="Effort Level"
        >
          {(["low", "med", "high", "max"] as const).map((e) => (
            <button
              key={e}
              onClick={() => { setEffort(e); setActive(null); }}
              className={`touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors ${
                effort === e ? "bg-accent/15 text-accent font-medium" : "text-muted-foreground hover:bg-surface2"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="capitalize">{e}</span>
                {effort === e && <span className="text-[10px] text-accent">●</span>}
              </div>
              <div className="text-[10px] opacity-70 mt-0.5 leading-snug">
                {EFFORT_DESCRIPTIONS[e]}
              </div>
            </button>
          ))}
          {!effortActive && (
            <div className="text-[9px] text-amber-400/70 px-2 py-1 mt-1 border-t border-white/5">
              This model may not honor effort settings
            </div>
          )}
        </PopoverPortal>
      </div>

      {/* ─── Web Search ─────────────────────────────────────── */}
      <div className="relative">
        <button
          ref={webBtnRef}
          onClick={() => !disabled && setActive(active === "web" ? null : "web")}
          className={`${btn("web")} ${webSearch || active === "web" ? "text-accent bg-accent/10" : ""} ${!webActive ? "opacity-40" : ""}`}
          title="Web search"
          disabled={disabled && active !== "web"}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="2" y1="12" x2="22" y2="12" />
            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
          </svg>
        </button>
        <PopoverPortal
          open={active === "web"}
          onClose={() => setActive(null)}
          anchorRef={webBtnRef}
          align="left"
          direction="up"
          width={256}
          title="Web Search"
        >
          <button
            onClick={() => { toggleWebSearch(); setWebTemplate(""); setActive(null); }}
            className={`touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors ${
              webSearch && !webTemplate ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
            }`}
          >
            <div className="flex items-center justify-between">
              <span>Regular Web Search</span>
              <span className={`size-1.5 rounded-full ${webSearch && !webTemplate ? "bg-accent" : "bg-muted-foreground/30"}`} />
            </div>
            <div className="text-[10px] opacity-60 mt-0.5">Agent can search the web during its turn</div>
          </button>

          {isCustomWebTemplate && (
            <div className="mx-2 mt-1 px-1.5 py-1 rounded bg-accent/10 border border-accent/30 text-[10px] text-accent flex items-center gap-1">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              <span className="truncate">Using template: {webTemplate.slice(0, 18)}</span>
              <button
                onClick={() => setWebTemplate("")}
                className="ml-auto opacity-70 hover:opacity-100"
                title="Clear template"
              >
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          )}

          <div className="border-t border-white/5 mt-1 pt-1">
            <button
              onClick={() => {
                if (!webSearch) toggleWebSearch();
                setActive(null);
                onOpenTemplateLibrary("websearch");
              }}
              className="touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors text-accent hover:bg-accent/10 flex items-center gap-1.5"
              title="Browse web search templates from the library"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 4v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.343a2 2 0 0 0-.586-1.414l-4.343-4.343A2 2 0 0 0 15.657 2H6a2 2 0 0 0-2 2z" />
                <path d="M14 2v6h6" />
              </svg>
              <span>Browse Templates…</span>
            </button>
          </div>
        </PopoverPortal>
      </div>

      {/* ─── Deep Research ──────────────────────────────────── */}
      <div className="relative">
        <button
          ref={deepBtnRef}
          onClick={() => !disabled && setActive(active === "deep" ? null : "deep")}
          className={`${btn("deep")} ${deepResearch || active === "deep" ? "text-accent bg-accent/10" : ""} ${!deepActive ? "opacity-40" : ""}`}
          title="Deep research"
          disabled={disabled && active !== "deep"}
        >
          {/* Telescope icon */}
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.065 12.493l-6.18 1.318a.934.934 0 0 1-1.108-.702l-.537-2.15a1.07 1.07 0 0 1 .691-1.265l13.504-4.44" />
            <path d="M13.56 11.747l4.334-1.428" />
            <path d="M17.5 5.5l2.5-2.5 1.5 1.5L19 7" />
            <path d="m14.5 14.5 4 4-1.5 1.5-4-4" />
            <path d="M9.5 14.5 5 19l-1.5-1.5L8 13" />
          </svg>
        </button>
        <PopoverPortal
          open={active === "deep"}
          onClose={() => setActive(null)}
          anchorRef={deepBtnRef}
          align="left"
          direction="up"
          width={288}
          title="Deep Research"
        >
          <button
            onClick={() => { toggleDeepResearch(); setDeepTemplate(""); setActive(null); }}
            className={`touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors ${
              deepResearch && !deepTemplate ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
            }`}
          >
            <div className="flex items-center justify-between">
              <span>Default</span>
              <span className={`size-1.5 rounded-full ${deepResearch && !deepTemplate ? "bg-accent" : "bg-muted-foreground/30"}`} />
            </div>
            <div className="text-[10px] opacity-60 mt-0.5">Extended reasoning + web ReAct loop</div>
          </button>
          <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-2 pb-1 border-t border-white/5 mt-1">
            Template Overrides
          </div>
          {DEEP_TEMPLATES.map((t) => {
            const supported = t.id === "react" ? capabilities.deepResearch : capabilities.extendedThinking;
            return (
              <button
                key={t.id}
                onClick={() => { if (!deepResearch) toggleDeepResearch(); setDeepTemplate(t.id); setActive(null); }}
                className={`touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors ${
                  deepResearch && deepTemplate === t.id ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                } ${!supported ? "opacity-50" : ""}`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{t.label}</span>
                  {deepResearch && deepTemplate === t.id && <span className="text-[10px] text-accent">●</span>}
                </div>
                <div className="text-[10px] opacity-60 mt-0.5">
                  {t.description}
                  {!supported && <span className="text-amber-400/70"> · not supported</span>}
                </div>
              </button>
            );
          })}

          {isCustomDeepTemplate && (
            <div className="mx-2 mt-1 px-1.5 py-1 rounded bg-accent/10 border border-accent/30 text-[10px] text-accent flex items-center gap-1">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              <span className="truncate">Using template: {deepTemplate.slice(0, 18)}</span>
              <button
                onClick={() => setDeepTemplate("")}
                className="ml-auto opacity-70 hover:opacity-100"
                title="Clear template"
              >
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          )}

          <div className="border-t border-white/5 mt-1 pt-1">
            <button
              onClick={() => {
                if (!deepResearch) toggleDeepResearch();
                setActive(null);
                onOpenTemplateLibrary("deepresearch");
              }}
              className="touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors text-accent hover:bg-accent/10 flex items-center gap-1.5"
              title="Browse deep research templates from the library"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 4v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.343a2 2 0 0 0-.586-1.414l-4.343-4.343A2 2 0 0 0 15.657 2H6a2 2 0 0 0-2 2z" />
                <path d="M14 2v6h6" />
              </svg>
              <span>Browse Templates…</span>
            </button>
          </div>
          <div className="text-[9px] text-muted-foreground/50 px-2 py-1 mt-1 border-t border-white/5">
            Uses more tokens + longer timeouts for thorough analysis
          </div>
        </PopoverPortal>
      </div>

      {/* ─── Judge ──────────────────────────────────────────── */}
      <div className="relative">
        <button
          ref={judgeBtnRef}
          onClick={() => !disabled && setActive(active === "judge" ? null : "judge")}
          className={btn("judge")}
          title="Judge panel"
          disabled={disabled && active !== "judge"}
        >
          {/* Gavel icon */}
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m14 12-8.5 8.5a2.12 2.12 0 1 1-3-3L11 9" />
            <path d="M15 13 9 7" />
            <path d="m18 15-4-4" />
            <path d="M21 3a2.85 2.83 0 0 0-4 4l-2 2" />
            <path d="m21 3-2 2" />
            <path d="m3 21 2-2" />
          </svg>
        </button>
        <PopoverPortal
          open={active === "judge"}
          onClose={() => setActive(null)}
          anchorRef={judgeBtnRef}
          align="left"
          direction="up"
          width={304}
          title="Judge Panel"
        >
          <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-1 pb-1">Count</div>
          <div className="flex gap-1 px-2 pb-2">
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <button
                key={n}
                onClick={() => setJudgeCount(n)}
                className={`touch-target flex-1 h-10 rounded-xl text-[12px] font-medium transition-colors ${
                  judgeCount === n
                    ? "bg-accent text-white"
                    : "bg-surface2 text-muted-foreground hover:text-foreground"
                }`}
              >
                {n}
              </button>
            ))}
          </div>
          <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-1 pb-1 border-t border-white/5">
            Template
          </div>
          {/* BATCH-2 Task 5.10 — removed TEMPLATE QUICK PICKS (Critique /
              Verify / Improve / Debate). The user said these were redundant
              with the "Browse Templates…" library entry below. The default
              judge.template ("critique") is still set by the store, so the
              Judge panel runs with a sensible default until the user picks
              one from the library. */}

          {isCustomJudgeTemplate && (
            <div className="mx-2 mt-1 px-1.5 py-1 rounded bg-accent/10 border border-accent/30 text-[10px] text-accent flex items-center gap-1">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              <span className="truncate">Custom template set</span>
              <button
                onClick={() => setJudgeTemplate("critique")}
                className="ml-auto opacity-70 hover:opacity-100"
                title="Reset to default"
              >
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          )}

          <div className="border-t border-white/5 mt-1 pt-1">
            <button
              onClick={() => {
                setActive(null);
                onOpenTemplateLibrary("judge");
              }}
              className="touch-target w-full text-left px-3 py-2 min-h-[40px] rounded-xl text-[12px] transition-colors text-accent hover:bg-accent/10 flex items-center gap-1.5"
              title="Browse judge templates from the library"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 4v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.343a2 2 0 0 0-.586-1.414l-4.343-4.343A2 2 0 0 0 15.657 2H6a2 2 0 0 0-2 2z" />
                <path d="M14 2v6h6" />
              </svg>
              <span>Browse Templates…</span>
            </button>
          </div>

          <div className="border-t border-white/5 mt-1 p-2">
            <button
              onClick={() => {
                setJudge({ count: judgeCount, template: judgeTemplate });
                setActive(null);
                // Fire on next tick so the popover closes first.
                setTimeout(() => onRunJudge(), 0);
              }}
              className="touch-target w-full py-3 rounded-2xl bg-gradient-to-br from-accent to-accentHover text-white text-[12px] font-medium hover:from-accentHover hover:to-accentDeep transition-colors flex items-center justify-center gap-1.5 shadow-sm"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              Run {judgeCount} judge{judgeCount !== 1 ? "s" : ""}
            </button>
          </div>
        </PopoverPortal>
      </div>
    </div>
  );
}

// NOTE: the legacy in-file Popover component has been replaced by the
// portal-based Popover from "./Popover". That version escapes overflow
// clipping and backdrop-filter containing blocks so popovers always render
// above the chat content. The local Popover wrapper is intentionally removed.
