/**
 * ToolIcons — unified toolbar of popover-driven tools for the chat input area.
 *
 * Popovers:
 *  - Effort (Gauge icon): low/med/high/max. Dims to 40% opacity if the
 *    selected model's capabilities don't include "effort".
 *  - Web Search (Globe icon): Regular Web Search + 4 template overrides
 *    (Breadth Search, Deep Dive, Compare & Contrast, Fact Check).
 *  - Deep Research (Telescope icon): Default + ReAct Loop + Extended Thinking.
 *    Dims if the model doesn't support deepResearch/extendedThinking.
 *  - Judge (Gavel icon): SEPARATE icon with judge count (1-6 buttons) +
 *    template (Critique/Verify/Improve/Debate) + Run button. Fires
 *    onRunJudge(input?) when the user clicks Run.
 *
 * All popovers close on outside-click via a fixed-position overlay.
 */
import { useState, useEffect, useRef } from "react";

type PopoverKind = "effort" | "web" | "deep" | "judge" | null;

export interface ToolIconsProps {
  // Selection state
  effort: "low" | "med" | "high" | "max";
  webSearch: boolean;
  deepResearch: boolean;
  webTemplate: "" | "breadth" | "deepdive" | "compare" | "factcheck";
  deepTemplate: "" | "react" | "extended";
  judge: { count: number; template: "critique" | "verify" | "improve" | "debate" };

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
  setWebTemplate: (t: "" | "breadth" | "deepdive" | "compare" | "factcheck") => void;
  setDeepTemplate: (t: "" | "react" | "extended") => void;
  setJudge: (cfg: Partial<{ count: number; template: "critique" | "verify" | "improve" | "debate" }>) => void;

  /** Fire the judge panel. If no input is provided, the store uses inputText. */
  onRunJudge: (input?: string) => void;
}

const WEB_TEMPLATES: {
  id: "breadth" | "deepdive" | "compare" | "factcheck";
  label: string;
  description: string;
}[] = [
  { id: "breadth", label: "Breadth Search", description: "Wide net — discover every angle of a topic via parallel sub-agents" },
  { id: "deepdive", label: "Deep Dive", description: "One source, drilled all the way down with follow-up questions" },
  { id: "compare", label: "Compare & Contrast", description: "Two competing options evaluated head-to-head with criteria" },
  { id: "factcheck", label: "Fact Check", description: "Cross-reference a claim against multiple authoritative sources" },
];

const DEEP_TEMPLATES: {
  id: "react" | "extended";
  label: string;
  description: string;
}[] = [
  { id: "react", label: "ReAct Loop", description: "Reason → Act → Observe loop. Best for tool-heavy multi-step research" },
  { id: "extended", label: "Extended Thinking", description: "Long silent reasoning before responding. Best for hard problems" },
];

const JUDGE_TEMPLATES: {
  id: "critique" | "verify" | "improve" | "debate";
  label: string;
  description: string;
}[] = [
  { id: "critique", label: "Critique", description: "Find flaws, missing cases, security holes" },
  { id: "verify", label: "Verify", description: "Check correctness against authoritative sources" },
  { id: "improve", label: "Improve", description: "Rewrite with concrete fixes applied" },
  { id: "debate", label: "Debate", description: "Judges argue opposing sides, then synthesize" },
];

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
}: ToolIconsProps) {
  const [active, setActive] = useState<PopoverKind>(null);
  // Judge popover tracks the in-progress count/template separately so the
  // user can change them without immediately firing.
  const [judgeCount, setJudgeCount] = useState(judge.count);
  const [judgeTemplate, setJudgeTemplate] = useState(judge.template);
  const judgeBtnRef = useRef<HTMLButtonElement>(null);

  // Sync local judge state when the prop changes (e.g. resetTools)
  useEffect(() => {
    setJudgeCount(judge.count);
    setJudgeTemplate(judge.template);
  }, [judge.count, judge.template]);

  // Close on Escape
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
    `p-2 rounded-lg transition-colors ${
      active === k
        ? "text-accent bg-accent/10"
        : "text-muted-foreground hover:text-foreground hover:bg-surface2"
    }`;

  return (
    <div className="flex items-center gap-0.5 shrink-0 mb-0.5">
      {/* ─── Effort ─────────────────────────────────────────── */}
      <div className="relative">
        <button
          onClick={() => !disabled && setActive(active === "effort" ? null : "effort")}
          className={`${btn("effort")} ${!effortActive ? "opacity-40" : ""}`}
          title={`Effort: ${effort}${!effortActive ? " (not supported by this model)" : ""}`}
          disabled={disabled && active !== "effort"}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
          </svg>
        </button>
        {active === "effort" && (
          <Popover onClose={() => setActive(null)} align="left" width="w-48">
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">
              Effort Level
            </div>
            {(["low", "med", "high", "max"] as const).map((e) => (
              <button
                key={e}
                onClick={() => { setEffort(e); setActive(null); }}
                className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                  effort === e ? "bg-accent/15 text-accent font-medium" : "text-muted-foreground hover:bg-surface2"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="capitalize">{e}</span>
                  <span className="text-[9px] opacity-60">
                    {e === "low" ? "1 judge" : e === "med" ? "3 judges" : e === "high" ? "5 judges" : "all judges"}
                  </span>
                </div>
              </button>
            ))}
            {!effortActive && (
              <div className="text-[9px] text-amber-400/70 px-2 py-1 mt-1 border-t border-border/50">
                This model may not honor effort settings
              </div>
            )}
          </Popover>
        )}
      </div>

      {/* ─── Web Search ─────────────────────────────────────── */}
      <div className="relative">
        <button
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
        {active === "web" && (
          <Popover onClose={() => setActive(null)} align="left" width="w-60">
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">
              Web Search
            </div>
            <button
              onClick={() => { toggleWebSearch(); setWebTemplate(""); setActive(null); }}
              className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                webSearch && !webTemplate ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
              }`}
            >
              <div className="flex items-center justify-between">
                <span>Regular Web Search</span>
                <span className={`size-1.5 rounded-full ${webSearch && !webTemplate ? "bg-accent" : "bg-muted-foreground/30"}`} />
              </div>
              <div className="text-[9px] opacity-60 mt-0.5">Agent can search the web during its turn</div>
            </button>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-2 pb-1 border-t border-border/50 mt-1">
              Template Overrides
            </div>
            {WEB_TEMPLATES.map((t) => (
              <button
                key={t.id}
                onClick={() => { if (!webSearch) toggleWebSearch(); setWebTemplate(t.id); setActive(null); }}
                className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                  webSearch && webTemplate === t.id ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{t.label}</span>
                  {webSearch && webTemplate === t.id && <span className="text-[9px]">●</span>}
                </div>
                <div className="text-[9px] opacity-60 mt-0.5">{t.description}</div>
              </button>
            ))}
          </Popover>
        )}
      </div>

      {/* ─── Deep Research ──────────────────────────────────── */}
      <div className="relative">
        <button
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
        {active === "deep" && (
          <Popover onClose={() => setActive(null)} align="left" width="w-60">
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-2 py-1">
              Deep Research
            </div>
            <button
              onClick={() => { toggleDeepResearch(); setDeepTemplate(""); setActive(null); }}
              className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                deepResearch && !deepTemplate ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
              }`}
            >
              <div className="flex items-center justify-between">
                <span>Default</span>
                <span className={`size-1.5 rounded-full ${deepResearch && !deepTemplate ? "bg-accent" : "bg-muted-foreground/30"}`} />
              </div>
              <div className="text-[9px] opacity-60 mt-0.5">Extended reasoning + web ReAct loop</div>
            </button>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-2 pb-1 border-t border-border/50 mt-1">
              Template Overrides
            </div>
            {DEEP_TEMPLATES.map((t) => {
              const supported = t.id === "react" ? capabilities.deepResearch : capabilities.extendedThinking;
              return (
                <button
                  key={t.id}
                  onClick={() => { if (!deepResearch) toggleDeepResearch(); setDeepTemplate(t.id); setActive(null); }}
                  className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                    deepResearch && deepTemplate === t.id ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                  } ${!supported ? "opacity-50" : ""}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{t.label}</span>
                    {deepResearch && deepTemplate === t.id && <span className="text-[9px]">●</span>}
                  </div>
                  <div className="text-[9px] opacity-60 mt-0.5">
                    {t.description}
                    {!supported && <span className="text-amber-400/70"> · not supported</span>}
                  </div>
                </button>
              );
            })}
            <div className="text-[9px] text-muted-foreground/50 px-2 py-1 mt-1 border-t border-border/50">
              Uses more tokens + longer timeouts for thorough analysis
            </div>
          </Popover>
        )}
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
        {active === "judge" && (
          <Popover onClose={() => setActive(null)} align="left" width="w-64">
            <div className="flex items-center justify-between px-2 py-1">
              <span className="text-[9px] uppercase tracking-wide text-muted-foreground/60">Judge Panel</span>
              <span className="text-[9px] text-muted-foreground/50">Fan-out + merge</span>
            </div>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-2 pb-1">Count</div>
            <div className="flex gap-1 px-2 pb-2">
              {[1, 2, 3, 4, 5, 6].map((n) => (
                <button
                  key={n}
                  onClick={() => setJudgeCount(n)}
                  className={`size-7 rounded text-[11px] font-medium transition-colors ${
                    judgeCount === n
                      ? "bg-accent text-white"
                      : "bg-surface2 text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground/50 px-2 pt-1 pb-1 border-t border-border/50">
              Template
            </div>
            {JUDGE_TEMPLATES.map((t) => (
              <button
                key={t.id}
                onClick={() => setJudgeTemplate(t.id)}
                className={`w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors ${
                  judgeTemplate === t.id ? "bg-accent/15 text-accent" : "text-muted-foreground hover:bg-surface2"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{t.label}</span>
                  {judgeTemplate === t.id && <span className="text-[9px]">●</span>}
                </div>
                <div className="text-[9px] opacity-60 mt-0.5">{t.description}</div>
              </button>
            ))}
            <div className="border-t border-border/50 mt-1 p-2">
              <button
                onClick={() => {
                  setJudge({ count: judgeCount, template: judgeTemplate });
                  setActive(null);
                  // Fire on next tick so the popover closes first.
                  setTimeout(() => onRunJudge(), 0);
                }}
                className="w-full py-2 rounded-md bg-accent text-white text-[11px] font-medium hover:bg-accent/90 transition-colors flex items-center justify-center gap-1.5"
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                Run {judgeCount} judge{judgeCount !== 1 ? "s" : ""}
              </button>
            </div>
          </Popover>
        )}
      </div>
    </div>
  );
}

/** Shared popover wrapper. Renders a fixed-position overlay for outside-click,
 *  then the panel positioned above the trigger. */
function Popover({
  children,
  onClose,
  align,
  width,
}: {
  children: React.ReactNode;
  onClose: () => void;
  align: "left" | "right";
  width: string;
}) {
  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div
        className={`absolute bottom-full ${align === "right" ? "right-0" : "left-0"} mb-1 z-50 ${width} rounded-lg border border-border bg-surface shadow-xl p-1.5 max-h-[420px] overflow-y-auto`}
      >
        {children}
      </div>
    </>
  );
}
