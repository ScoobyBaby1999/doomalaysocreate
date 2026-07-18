import { useEffect, useMemo, useCallback, useRef, useState, memo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useModelStore, type ProviderGroup, type ProviderModel, type ModelAttributes, type CondensedHost, type CondensedModel } from "../lib/model-store";

// ── Icons ────────────────────────────────────────────────────────────────
function IcoX() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
  );
}
function IcoSearch() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
  );
}
function IcoCheck() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
  );
}
function IcoDot() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="1"/></svg>
  );
}
function IcoSettings() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
  );
}
function IcoShield() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>
  );
}
function IcoRefresh({ spinning }: { spinning?: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={spinning ? "animate-spin" : ""}><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
  );
}
// Drag handle — 6 dots in 2 cols × 3 rows (used for both provider boxes
// and condensed-row provider-rank rows).
function IcoDrag() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/><circle cx="15" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="18" r="1.4"/></svg>
  );
}

// ── Format helpers ──────────────────────────────────────────────────────
function fmtCtx(k: number): string {
  if (!k) return "\u2014";
  if (k >= 1_000_000) return `${(k / 1_000_000).toFixed(k % 1_000_000 === 0 ? 0 : 1)}M`;
  if (k >= 1_000) return `${(k / 1_000).toFixed(0)}K`;
  return String(k);
}

function confidenceDotColor(c: "high" | "medium" | "low"): string {
  if (c === "high") return "#22c55e";
  if (c === "medium") return "#f59e0b";
  return "#ef4444";
}

function syncedAgoLabel(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 5) return "synced just now";
  if (secs < 60) return `synced ${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `synced ${mins}m ago`;
  const hrs = Math.round(mins / 60);
  return `synced ${hrs}h ago`;
}

function scoreColor(s: number): string {
  if (s >= 70) return "#22c55e";
  if (s >= 40) return "#f59e0b";
  return "#ef4444";
}

const CAP_COLORS: Record<string, string> = {
  reasoning: "#f97316",
  code: "#3b82f6",
  tools: "#8b5cf6",
  vision: "#22c55e",
  speech: "#ec4899",
  audio: "#ec4899",
};

function capColor(cap: string): string | undefined {
  const key = cap.toLowerCase();
  return CAP_COLORS[key] ?? undefined;
}

// ── Filter / sort helpers ───────────────────────────────────────────────
function modelMatchesFilters(model: ProviderModel, activeFilters: string[], contextMin: number): boolean {
  const caps = model.attributes?.capabilities ?? [];
  const bm = model.attributes?.benchmarks;

  if (contextMin > 0 && model.contextLength < contextMin) return false;
  if (activeFilters.length === 0) return true;

  return activeFilters.some((filter) => {
    if (filter === "reasoning") {
      if (caps.includes("reasoning")) return true;
      if ((bm?.intelligence ?? 0) >= 20) return true;
    }
    if (filter === "code") {
      if (caps.includes("code")) return true;
      if ((bm?.coding ?? 0) >= 20 || (bm?.aaCoding ?? 0) >= 20) return true;
    }
    if (filter === "tools") {
      if (caps.includes("tools") || caps.includes("tool use") || caps.includes("function calling")) return true;
      if ((bm?.agentic ?? 0) >= 20) return true;
    }
    if (filter === "vision") {
      if (caps.includes("vision")) return true;
    }
    if (filter === "speech") {
      if (caps.includes("speech") || caps.includes("audio")) return true;
    }
    return false;
  });
}

function isReasoningModel(model: { displayName: string; id?: string; logical?: string; attributes?: ModelAttributes }): boolean {
  const caps = model.attributes?.capabilities ?? [];
  if (caps.includes("reasoning")) return true;
  const name = [model.displayName, model.id ?? model.logical ?? ""].join(" ").toLowerCase();
  return /\breasoning\b/.test(name);
}

function bestFilterScore(model: ProviderModel, activeFilters: string[]): number {
  const bm = model.attributes?.benchmarks;
  let best = 0;
  for (const filter of activeFilters) {
    if (filter === "reasoning") best = Math.max(best, isReasoningModel(model) ? 1 : 0);
    if (filter === "intelligence") best = Math.max(best, bm?.intelligence ?? 0);
    if (filter === "code") best = Math.max(best, bm?.coding ?? 0, bm?.aaCoding ?? 0);
    if (filter === "agent") best = Math.max(best, bm?.agentic ?? 0);
    if (filter === "tools") best = Math.max(best, bm?.agentic ?? 0);
  }
  return best;
}

function sortByFilterScore(models: ProviderModel[], activeFilters: string[]): ProviderModel[] {
  if (activeFilters.length === 0) return models;
  return [...models].sort((a, b) => bestFilterScore(b, activeFilters) - bestFilterScore(a, activeFilters));
}

function avgRank(attrs: ModelAttributes | undefined): number {
  const ranks = attrs?.ranks;
  if (!ranks || ranks.length === 0) return Infinity;
  return ranks.reduce((s, r) => s + r.rank, 0) / ranks.length;
}

function defaultSort(models: ProviderModel[]): ProviderModel[] {
  return [...models].sort((a, b) => {
    const aAvg = avgRank(a.attributes);
    const bAvg = avgRank(b.attributes);
    if (aAvg !== bAvg) return aAvg === Infinity ? 1 : bAvg === Infinity ? -1 : aAvg - bAvg;

    const aIntel = a.attributes?.benchmarks?.intelligence ?? 0;
    const bIntel = b.attributes?.benchmarks?.intelligence ?? 0;
    if (aIntel !== bIntel) return bIntel - aIntel;

    const aCode = a.attributes?.benchmarks?.coding ?? 0;
    const bCode = b.attributes?.benchmarks?.coding ?? 0;
    if (aCode !== bCode) return bCode - aCode;

    return 0;
  });
}

const FILTER_PILLS = [
  { id: "reasoning", label: "Reason", color: "#f97316" },
  { id: "intelligence", label: "Smart", color: "#a855f7" },
  { id: "code", label: "Code", color: "#3b82f6" },
  { id: "agent", label: "Agent", color: "#14b8a6" },
  { id: "tools", label: "Tools", color: "#8b5cf6" },
  { id: "vision", label: "Vision", color: "#22c55e" },
  { id: "speech", label: "Speech", color: "#ec4899" },
] as const;

const CONTEXT_OPTIONS = [
  { value: 0, label: "Any" },
  { value: 32000, label: "32K" },
  { value: 128000, label: "128K" },
  { value: 1000000, label: "1M" },
] as const;

interface AttrSegment {
  text: string;
  color?: string;
}

function attributeSegments(a: ModelAttributes | undefined): AttrSegment[] | null {
  if (!a) return null;
  const segs: AttrSegment[] = [];
  const b = a.benchmarks;
  if (b) {
    if (typeof b.intelligence === "number") {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: `AA ${b.intelligence}`, color: scoreColor(b.intelligence) });
    }
    if (typeof b.coding === "number") {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: `code ${b.coding}`, color: scoreColor(b.coding) });
    }
    if (typeof b.agentic === "number") {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: `agent ${b.agentic}`, color: scoreColor(b.agentic) });
    }
    if (typeof b.sweBench === "number") {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: `SWE ${b.sweBench}`, color: scoreColor(b.sweBench) });
    }
    if (typeof b.aaCoding === "number" && typeof b.sweBench === "undefined") {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: `AA-coding ${b.aaCoding}`, color: scoreColor(b.aaCoding) });
    }
  }
  if (a.ranks && a.ranks.length > 0) {
    const top = a.ranks.slice(0, 3).map((r) => `${r.label} #${r.rank}`).join(" \u00b7 ");
    if (segs.length) segs.push({ text: " \u00b7 " });
    segs.push({ text: top });
  }
  if (a.capabilities && a.capabilities.length > 0) {
    for (const cap of a.capabilities) {
      if (segs.length) segs.push({ text: " \u00b7 " });
      segs.push({ text: cap, color: capColor(cap) });
    }
  }
  if (a.pricing) {
    if (segs.length) segs.push({ text: " \u00b7 " });
    segs.push({ text: a.pricing });
  }

  if (segs.length === 0 && !a.note) return null;
  return segs;
}

// ── Persistence for provider box order + expanded state ─────────────────
const PREFIX = "doomalaysocreate.model-select";
const PROVIDER_BOX_ORDER_KEY = `${PREFIX}.providerBoxOrder`;
const PROVIDER_BOX_EXPANDED_KEY = `${PREFIX}.providerBoxExpanded`;

function loadProviderBoxOrder(): string[] | null {
  try {
    const raw = localStorage.getItem(PROVIDER_BOX_ORDER_KEY);
    if (!raw) return null;
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return arr as string[];
  } catch { /* ignore */ }
  return null;
}
function saveProviderBoxOrder(order: string[]) {
  try { localStorage.setItem(PROVIDER_BOX_ORDER_KEY, JSON.stringify(order)); } catch { /* ignore */ }
}
function loadProviderBoxExpanded(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(PROVIDER_BOX_EXPANDED_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Record<string, boolean>;
  } catch { return {}; }
}
function saveProviderBoxExpanded(map: Record<string, boolean>) {
  try { localStorage.setItem(PROVIDER_BOX_EXPANDED_KEY, JSON.stringify(map)); } catch { /* ignore */ }
}

// ── ModelRow (used inside ProviderBox) — condensed single-line format ────
const ModelRow = memo(function ModelRow({
  model,
  isSelected,
  onSelect,
  dimmed,
}: {
  model: ProviderModel;
  isSelected: boolean;
  onSelect: () => void;
  dimmed?: boolean;
}) {
  const segs = model.attributes ? attributeSegments(model.attributes) : null;
  const note = model.attributes?.note;
  const hasSub = !!(segs || note);

  return (
    <button
      onClick={onSelect}
      data-selected={isSelected ? "true" : undefined}
      aria-pressed={isSelected}
      className={`
        cv-auto flex flex-col w-full text-left rounded-lg px-2 py-1.5 min-h-[32px] max-h-[64px] overflow-hidden transition-colors duration-100 cursor-pointer touch-target
        ${isSelected ? "bg-accent/20 ring-1 ring-accent/40" : dimmed ? "" : "hover:bg-muted/40"}
        ${dimmed && !isSelected ? "opacity-35" : ""}
      `}
    >
      {/* Single-line row: [check] [model name] [context] */}
      <span className="flex items-center w-full gap-1.5">
        <span
          className="flex-shrink-0 flex items-center justify-center rounded-full"
          style={{
            width: 14, height: 14, fontSize: 0,
            border: isSelected ? "none" : "1.5px solid var(--border)",
            backgroundColor: isSelected ? "#a855f7" : "transparent",
          }}
        >
          {isSelected && <IcoCheck />}
        </span>
        <span
          className={`text-[12px] leading-tight truncate flex-1 font-medium ${isSelected ? "text-white" : "text-foreground"}`}
          title={model.id}
        >
          {model.displayName}
        </span>
        <span className="flex-shrink-0 text-[10px] text-muted-foreground tabular-nums">
          {fmtCtx(model.contextLength)}
        </span>
      </span>

      {hasSub && (
        <div className="flex flex-wrap gap-x-1 gap-y-px mt-0.5 pl-5 pr-1">
          {segs && segs.map((s, i) =>
            s.text === " \u00b7 " ? null : (
              <span
                key={i}
                className="text-[9px] leading-none px-1 py-px rounded-sm"
                style={s.color ? { color: dimmed ? undefined : s.color, backgroundColor: `${s.color}12` } : { color: dimmed ? undefined : "var(--muted-foreground)" }}
              >
                {s.text}
              </span>
            )
          )}
          {note && (
            <span
              className={`text-[9px] leading-none px-1 py-px rounded-sm ${note.startsWith("\u26a0") ? "text-amber-600 dark:text-amber-500 bg-amber-500/10" : "text-muted-foreground/70 bg-muted/30"}`}
            >
              {note}
            </span>
          )}
        </div>
      )}
    </button>
  );
});

// ── ProviderBox — single card per provider with collapse + DnD reorder ───
function ProviderBox({
  provider,
  selectedModelId,
  selectedProviderName,
  condensedView,
  onSelect,
  searchQuery,
  activeFilters,
  contextMin,
  expanded,
  onToggleExpand,
  // Drag-and-drop reorder props (HTML5 DnD for desktop; arrow buttons
  // provide a touch-friendly fallback).
  dragHandleProps,
  onMoveUp,
  onMoveDown,
  isFirst,
  isLast,
  isDragTarget,
  onDragOverBox,
  onDropOnBox,
}: {
  provider: ProviderGroup;
  selectedModelId: string | null;
  selectedProviderName: string | null;
  condensedView: boolean;
  onSelect: (model: ProviderModel, providerName: string) => void;
  searchQuery: string;
  activeFilters: string[];
  contextMin: number;
  expanded: boolean;
  onToggleExpand: () => void;
  dragHandleProps?: {
    draggable: boolean;
    onDragStart: (e: React.DragEvent) => void;
    onDragEnd: (e: React.DragEvent) => void;
  };
  onMoveUp: () => void;
  onMoveDown: () => void;
  isFirst: boolean;
  isLast: boolean;
  isDragTarget: boolean;
  onDragOverBox: (e: React.DragEvent) => void;
  onDropOnBox: (e: React.DragEvent) => void;
}) {
  const [showDimmed, setShowDimmed] = useState(false);

  const { matched, dimmed } = useMemo(() => {
    const allModels = provider.models;
    const searched = !searchQuery.trim()
      ? allModels
      : allModels.filter((m) => {
          const q = searchQuery.toLowerCase();
          return (
            m.displayName.toLowerCase().includes(q) ||
            m.id.toLowerCase().includes(q) ||
            (m.family?.toLowerCase().includes(q) ?? false)
          );
        });
    const m: ProviderModel[] = [];
    const d: ProviderModel[] = [];
    for (const mdl of searched) {
      if (modelMatchesFilters(mdl, activeFilters, contextMin)) {
        m.push(mdl);
      } else {
        d.push(mdl);
      }
    }
    return {
      matched: activeFilters.length > 0 || contextMin > 0 ? sortByFilterScore(m, activeFilters) : defaultSort(m),
      dimmed: d,
    };
  }, [provider.models, searchQuery, activeFilters, contextMin]);

  const openSettings = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    useModelStore.getState().openProvidersDialog(provider.name);
  }, [provider.name]);

  const hasDimmed = dimmed.length > 0;
  const empty = matched.length === 0 && !hasDimmed;

  return (
    <div
      // Drop target — the entire box accepts drops from sibling boxes.
      onDragOver={onDragOverBox}
      onDrop={onDropOnBox}
      className={`flex flex-col rounded-xl border overflow-hidden transition-colors ${
        isDragTarget ? "border-accent ring-2 ring-accent/40" : ""
      }`}
      style={{ borderColor: isDragTarget ? undefined : `${provider.color}30` }}
    >
      {/* Header — drag handle (left) + tap-to-expand (middle) + gear + chevron. */}
      <div
        className="flex items-center gap-1 px-2 shrink-0 min-h-[44px]"
        style={{ backgroundColor: `${provider.color}0d`, borderBottom: expanded ? `1px solid ${provider.color}1f` : "none" }}
      >
        {/* Drag handle — only visible on hover; HTML5 DnD works on desktop.
            Touch users use the arrow buttons (rendered further right) as a
            fallback per the spec. */}
        <button
          {...(dragHandleProps || {})}
          onClick={(e) => e.stopPropagation()}
          className="touch-target inline-flex items-center justify-center size-7 rounded-lg text-muted-foreground/40 hover:text-muted-foreground hover:bg-surface2/40 transition-colors shrink-0 cursor-grab active:cursor-grabbing"
          aria-label={`Drag to reorder ${provider.displayName}`}
          title="Drag to reorder"
        >
          <IcoDrag />
        </button>

        {/* Tap-to-expand main area */}
        <button
          onClick={onToggleExpand}
          className="touch-target flex items-center gap-2 flex-1 min-w-0 text-left rounded-lg"
          aria-expanded={expanded}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${provider.displayName}`}
        >
          <svg
            width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"
            className={`text-muted-foreground transition-transform duration-150 shrink-0 ${expanded ? "rotate-90" : ""}`}
          >
            <polyline points="9 18 15 12 9 6" />
          </svg>
          <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: provider.color }} />
          <span className="text-[12px] font-semibold text-foreground leading-none truncate flex-1">{provider.displayName}</span>
          <span
            className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ backgroundColor: provider.syncedLive ? "#22c55e" : "#f59e0b" }}
            title={provider.syncedLive ? "synced live from provider API" : "config-sourced (no public live API)"}
          />
          <span className="text-[11px] text-muted-foreground leading-none tabular-nums shrink-0">{provider.models.length}</span>
        </button>

        {/* Touch-friendly move buttons (fallback for DnD on touch). */}
        <div className="flex gap-px shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); onMoveUp(); }}
            disabled={isFirst}
            className="touch-target size-7 flex items-center justify-center rounded-lg text-muted-foreground/50 hover:text-foreground hover:bg-surface2/60 transition-colors disabled:opacity-20"
            title="Move provider up"
            aria-label="Move provider up"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m18 15-6-6-6 6"/></svg>
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onMoveDown(); }}
            disabled={isLast}
            className="touch-target size-7 flex items-center justify-center rounded-lg text-muted-foreground/50 hover:text-foreground hover:bg-surface2/60 transition-colors disabled:opacity-20"
            title="Move provider down"
            aria-label="Move provider down"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>
        </div>

        <button
          onClick={openSettings}
          className="touch-target inline-flex items-center justify-center size-7 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
          aria-label={`Manage ${provider.displayName} privacy & keys`}
          title={`${provider.manageLabel} \u2197`}
        >
          <IcoSettings />
        </button>
      </div>

      {/* Privacy notice — still tappable to open settings. */}
      {expanded && (
        <button
          onClick={openSettings}
          className="flex items-center gap-1.5 px-3 shrink-0 text-left hover:brightness-95 dark:hover:brightness-110 transition-all min-h-[28px]"
          style={{
            borderBottom: `1px solid ${confidenceDotColor(provider.privacy.confidence)}25`,
            backgroundColor: `${confidenceDotColor(provider.privacy.confidence)}0d`,
          }}
          title={provider.privacy.notice}
        >
          <span className="shrink-0" style={{ color: confidenceDotColor(provider.privacy.confidence) }}>
            <IcoShield />
          </span>
          <span className="text-[10px] leading-none text-muted-foreground truncate flex-1 py-1.5">
            {provider.privacy.notice}
          </span>
        </button>
      )}

      {/* Model list — only rendered when expanded. Properly spaced rows. */}
      {expanded && (
        <div className="flex flex-col gap-1 p-1.5 max-h-[50vh] overflow-y-auto">
          {empty ? (
            <div className="flex items-center justify-center h-10 text-[11px] text-muted-foreground/50">
              no models synced
            </div>
          ) : (
            <>
              {matched.map((m) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  isSelected={selectedModelId === m.id && (!condensedView ? selectedProviderName === provider.name : true)}
                  onSelect={() => onSelect(m, provider.name)}
                />
              ))}
              {hasDimmed && (
                <>
                  <button
                    onClick={() => setShowDimmed((v) => !v)}
                    className="touch-target flex items-center gap-1.5 px-2 py-1 mt-0.5 border-t border-border/30 text-[10px] text-muted-foreground/60 hover:text-muted-foreground transition-colors cursor-pointer select-none min-h-[28px] w-full text-left"
                  >
                    <span
                      className="text-[10px] leading-none transition-transform duration-150"
                      style={{ transform: showDimmed ? "rotate(90deg)" : "rotate(0deg)" }}
                    >
                      {"\u25b8"}
                    </span>
                    <span className="text-[9px] leading-none">
                      {showDimmed ? `Hide ${dimmed.length} dimmed` : `Show ${dimmed.length} dimmed`}
                    </span>
                  </button>
                  {showDimmed && dimmed.map((m) => (
                    <ModelRow
                      key={m.id}
                      model={m}
                      isSelected={selectedModelId === m.id && (!condensedView ? selectedProviderName === provider.name : true)}
                      onSelect={() => onSelect(m, provider.name)}
                      dimmed
                    />
                  ))}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── CondensedModelRow — single-line: name | dots | context, expandable ──
//
// Collapsed: shows the model name (left, bold), then a horizontal row of
// tiny provider color dots (one per host, ordered by priority), then the
// model context (right). Tapping the dots area expands the row.
//
// Expanded: each provider host is its own row with rank # + provider NAME +
// model id + context, plus a drag handle (HTML5 DnD) and arrow buttons
// (touch fallback). The drag handle reorders the host priority permanently
// (persisted via model-store.setProviderPriority).
const CondensedModelRow = memo(function CondensedModelRow({
  model,
  isSelected,
  onSelect,
  expanded,
  onToggleExpand,
  dimmed,
}: {
  model: CondensedModel;
  isSelected: boolean;
  onSelect: () => void;
  expanded: boolean;
  onToggleExpand: () => void;
  dimmed?: boolean;
}) {
  const segs = model.attributes ? attributeSegments(model.attributes) : null;
  const note = model.attributes?.note;
  const hasSub = !!(segs || note);

  const [localOrder, setLocalOrder] = useState<CondensedHost[]>(() =>
    useModelStore.getState().getOrderedHosts(model.logical, model.hosts)
  );

  useEffect(() => {
    const current = useModelStore.getState().getOrderedHosts(model.logical, model.hosts);
    setLocalOrder(current);
  }, [model.logical, model.hosts]);

  // Persist the new order to the model store so it survives reloads.
  function commitOrder(next: CondensedHost[]) {
    setLocalOrder(next);
    useModelStore.getState().setProviderPriority(
      model.logical,
      next.map((h) => h.provider),
    );
  }

  function moveUp(idx: number) {
    if (idx <= 0) return;
    const next = [...localOrder];
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
    commitOrder(next);
  }
  function moveDown(idx: number) {
    if (idx >= localOrder.length - 1) return;
    const next = [...localOrder];
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
    commitOrder(next);
  }

  // HTML5 drag-and-drop for host reordering (desktop). Touch uses arrows.
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dropIdx, setDropIdx] = useState<number | null>(null);

  function onHostDragStart(idx: number, e: React.DragEvent) {
    setDragIdx(idx);
    e.dataTransfer.effectAllowed = "move";
    // Some browsers require data to be set for drag to fire.
    e.dataTransfer.setData("text/plain", String(idx));
  }
  function onHostDragOver(idx: number, e: React.DragEvent) {
    if (dragIdx === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (dropIdx !== idx) setDropIdx(idx);
  }
  function onHostDrop(idx: number, e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (dragIdx === null || dragIdx === idx) {
      setDragIdx(null); setDropIdx(null);
      return;
    }
    const next = [...localOrder];
    const [moved] = next.splice(dragIdx, 1);
    next.splice(idx, 0, moved);
    commitOrder(next);
    setDragIdx(null); setDropIdx(null);
  }
  function onHostDragEnd() { setDragIdx(null); setDropIdx(null); }

  return (
    <div
      className="cv-auto flex flex-col w-full text-left rounded-lg px-2 py-1.5 min-h-[36px] max-h-[120px] overflow-hidden transition-colors duration-100"
      data-selected={isSelected ? "true" : undefined}
    >
      {/* ── Single-line row: [check] [name] [dots] [context] ──────────── */}
      <span className="flex items-center w-full gap-1.5">
        <button
          onClick={onSelect}
          data-selected={isSelected ? "true" : undefined}
          aria-pressed={isSelected}
          className="touch-target flex items-center flex-1 min-w-0 cursor-pointer text-left rounded-lg"
        >
          <span
            className={`flex-shrink-0 flex items-center justify-center rounded-full ${isSelected ? "text-primary-foreground" : "text-transparent"}`}
            style={{ width: 14, height: 14, fontSize: 0, border: isSelected ? "none" : "1.5px solid var(--border)", backgroundColor: isSelected ? "#a855f7" : "transparent" }}
          >
            {isSelected && <IcoCheck />}
          </span>
          <span
            className={`text-[12px] leading-tight truncate flex-1 font-medium ${isSelected ? "text-white" : dimmed ? "text-muted-foreground/60" : "text-foreground"}`}
            title={model.logical}
          >
            {model.displayName}
          </span>
        </button>

        {/* Provider rank dropdown — collapsed shows tiny color dots. */}
        {localOrder.length > 1 && !expanded && (
          <button
            onClick={onToggleExpand}
            className="touch-target inline-flex items-center gap-0.5 px-1.5 h-6 rounded-full border border-border/60 hover:bg-surface2/60 transition-colors shrink-0"
            title="Tap to expand provider rank dropdown"
            aria-label="Expand provider rank dropdown"
            aria-expanded={expanded}
          >
            {localOrder.slice(0, 5).map((h, i) => (
              <span
                key={`${h.provider}-${i}`}
                className="inline-block rounded-full"
                style={{
                  width: 6, height: 6,
                  backgroundColor: h.color,
                  opacity: h.hasApiKey ? 1 : 0.35,
                }}
              />
            ))}
            {localOrder.length > 5 && (
              <span className="text-[8px] text-muted-foreground ml-0.5">+{localOrder.length - 5}</span>
            )}
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" className="text-muted-foreground/60 ml-0.5">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
        )}
        {localOrder.length === 1 && !expanded && (
          <span className="inline-flex items-center gap-1 px-1.5 h-6 shrink-0">
            <span
              className="inline-block rounded-full"
              style={{
                width: 6, height: 6,
                backgroundColor: localOrder[0].color,
                opacity: localOrder[0].hasApiKey ? 1 : 0.35,
              }}
            />
          </span>
        )}

        {/* Model context — right-aligned. */}
        <span className="flex-shrink-0 text-[10px] text-muted-foreground tabular-nums ml-1">
          {fmtCtx(model.contextLength)}
        </span>
      </span>

      {/* ── Expanded: provider rank rows (drag-to-reorder + arrows) ───── */}
      {expanded && (
        <div className="flex flex-col gap-0.5 pl-5 pr-1 mt-1">
          {localOrder.map((host, i) => (
            <div
              key={`${host.provider}-${host.modelId}`}
              draggable
              onDragStart={(e) => onHostDragStart(i, e)}
              onDragEnd={onHostDragEnd}
              onDragOver={(e) => onHostDragOver(i, e)}
              onDrop={(e) => onHostDrop(i, e)}
              className={`flex items-center gap-1.5 py-1 px-1 min-h-[32px] max-h-[40px] rounded-md text-[11px] transition-colors cursor-grab active:cursor-grabbing ${
                dropIdx === i && dragIdx !== null && dragIdx !== i
                  ? "bg-accent/15 ring-1 ring-accent/40"
                  : dragIdx === i
                  ? "opacity-40"
                  : "hover:bg-surface2/40"
              }`}
              style={{ opacity: dragIdx === i ? 0.4 : (host.hasApiKey ? 1 : 0.5) }}
            >
              {/* Drag handle (desktop). Touch uses arrow buttons. */}
              <span className="text-muted-foreground/40 shrink-0">
                <IcoDrag />
              </span>
              {/* Rank number + provider NAME (per spec) */}
              <span
                className="inline-flex items-center justify-center size-4 rounded-full text-[8px] font-bold shrink-0"
                style={{ backgroundColor: `${host.color}30`, color: host.color }}
              >
                {i + 1}
              </span>
              <span
                className="inline-block w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: host.color }}
              />
              <span className="truncate text-foreground/90 flex-1 min-w-0 font-medium">
                {host.providerDisplayName}
              </span>
              <span className="truncate text-muted-foreground/70 text-[10px] min-w-0 max-w-[35%]">
                {host.modelId}
              </span>
              <span className="tabular-nums text-muted-foreground/60 shrink-0 text-[10px]">
                {fmtCtx(host.contextLength)}
              </span>
              {/* Arrow buttons (touch fallback). */}
              <div className="flex gap-px shrink-0">
                <button
                  onClick={(e) => { e.stopPropagation(); moveUp(i); }}
                  disabled={i === 0}
                  className="touch-target size-6 flex items-center justify-center rounded-md text-muted-foreground/50 hover:text-foreground hover:bg-surface2/60 transition-colors disabled:opacity-20 disabled:cursor-default"
                  title="Higher priority"
                  aria-label="Move provider up"
                >
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m18 15-6-6-6 6"/></svg>
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); moveDown(i); }}
                  disabled={i === localOrder.length - 1}
                  className="touch-target size-6 flex items-center justify-center rounded-md text-muted-foreground/50 hover:text-foreground hover:bg-surface2/60 transition-colors disabled:opacity-20 disabled:cursor-default"
                  title="Lower priority"
                  aria-label="Move provider down"
                >
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
                </button>
              </div>
            </div>
          ))}
          <button
            onClick={onToggleExpand}
            className="touch-target text-[9px] text-muted-foreground/60 hover:text-muted-foreground py-1 text-left"
          >
            Collapse
          </button>
        </div>
      )}

      {hasSub && !expanded && (
        <div className="flex flex-wrap gap-x-1 gap-y-px mt-0.5 pl-5 pr-1">
          {segs && segs.map((s, i) =>
            s.text === " \u00b7 " ? null : (
              <span key={i} className="text-[9px] leading-none px-1 py-px rounded-sm"
                style={s.color ? { color: dimmed ? undefined : s.color, backgroundColor: `${s.color}12` } : { color: dimmed ? undefined : "var(--muted-foreground)" }}>
                {s.text}
              </span>
            )
          )}
          {note && (
            <span className={`text-[9px] leading-none px-1 py-px rounded-sm ${note.startsWith("\u26a0") ? "text-amber-600 dark:text-amber-500 bg-amber-500/10" : "text-muted-foreground/70 bg-muted/30"}`}>
              {note}
            </span>
          )}
        </div>
      )}
    </div>
  );
});

// ── CondensedList ────────────────────────────────────────────────────────
function condensedModelMatchesFilters(model: CondensedModel, activeFilters: string[], contextMin: number): boolean {
  if (contextMin > 0 && model.contextLength < contextMin) return false;
  if (activeFilters.length === 0) return true;
  const caps = model.attributes?.capabilities ?? [];
  const bm = model.attributes?.benchmarks;
  return activeFilters.some((filter) => {
    if (filter === "reasoning") return isReasoningModel(model);
    if (filter === "intelligence") return (bm?.intelligence ?? 0) >= 20;
    if (filter === "code") return caps.includes("code") || (bm?.coding ?? 0) >= 20 || (bm?.aaCoding ?? 0) >= 20;
    if (filter === "agent") return (bm?.agentic ?? 0) >= 20;
    if (filter === "tools") return caps.includes("tools") || caps.includes("tool use") || (bm?.agentic ?? 0) >= 20;
    if (filter === "vision") return caps.includes("vision");
    if (filter === "speech") return caps.includes("speech") || caps.includes("audio");
    return false;
  });
}

function CondensedList({
  models,
  searchQuery,
  activeFilters,
  contextMin,
  hideUnavailable,
  selectedModelId,
  onSelect,
  expandedLogical,
  onToggleExpand,
}: {
  models: CondensedModel[];
  searchQuery: string;
  activeFilters: string[];
  contextMin: number;
  hideUnavailable: boolean;
  selectedModelId: string | null;
  onSelect: (model: CondensedModel) => void;
  expandedLogical: string | null;
  onToggleExpand: (logical: string | null) => void;
}) {
  const [showHidden, setShowHidden] = useState(false);
  const [showUnavailable, setShowUnavailable] = useState(false);

  const { visible, hiddenByFilter } = useMemo(() => {
    const searched = !searchQuery.trim()
      ? models
      : models.filter((m) => {
          const q = searchQuery.toLowerCase();
          return (
            m.displayName.toLowerCase().includes(q) ||
            m.logical.toLowerCase().includes(q) ||
            (m.family?.toLowerCase().includes(q) ?? false) ||
            m.hosts.some((h) => h.providerDisplayName.toLowerCase().includes(q))
          );
        });

    const filtMatch: CondensedModel[] = [];
    const filtMiss: CondensedModel[] = [];
    for (const m of searched) {
      if (condensedModelMatchesFilters(m, activeFilters, contextMin)) {
        filtMatch.push(m);
      } else {
        filtMiss.push(m);
      }
    }

    const sorted = activeFilters.length > 0 || contextMin > 0
      ? [...filtMatch].sort((a, b) => {
          const sa = bestFilterScore({ id: a.logical, displayName: a.displayName, contextLength: a.contextLength, attributes: a.attributes } as ProviderModel, activeFilters);
          const sb = bestFilterScore({ id: b.logical, displayName: b.displayName, contextLength: b.contextLength, attributes: b.attributes } as ProviderModel, activeFilters);
          return sb - sa;
        })
      : defaultSort(filtMatch.map((m) => ({ id: m.logical, displayName: m.displayName, contextLength: m.contextLength, attributes: m.attributes } as ProviderModel))).map((pm) => filtMatch.find((m) => m.logical === pm.id)!).filter(Boolean);

    return {
      visible: sorted,
      hiddenByFilter: filtMiss,
    };
  }, [models, searchQuery, activeFilters, contextMin]);

  const selectedLogical = useMemo(() => {
    if (!selectedModelId) return null;
    for (const model of models) {
      if (model.logical === selectedModelId) return model.logical;
      if (model.hosts.some((h) => h.modelId === selectedModelId)) return model.logical;
    }
    return null;
  }, [selectedModelId, models]);

  const available = useMemo(() => visible.filter((m) => m.hosts.some((h) => h.hasApiKey)), [visible]);
  const unavailable = useMemo(() => visible.filter((m) => !m.hosts.some((h) => h.hasApiKey)), [visible]);

  return (
    <div className="flex flex-col gap-0.5 p-1">
      {visible.length === 0 && hiddenByFilter.length === 0 && (
        <div className="flex items-center justify-center h-16 text-[10px] text-muted-foreground/50">
          no models match
        </div>
      )}

      {(hideUnavailable ? available : visible).map((m) => (
        <CondensedModelRow
          key={m.logical}
          model={m}
          isSelected={selectedLogical === m.logical}
          onSelect={() => onSelect(m)}
          expanded={expandedLogical === m.logical}
          onToggleExpand={() => onToggleExpand(expandedLogical === m.logical ? null : m.logical)}
          dimmed={!m.hosts.some((h) => h.hasApiKey)}
        />
      ))}

      {hideUnavailable && unavailable.length > 0 && (
        <>
          <button
            onClick={() => setShowUnavailable((v) => !v)}
            className="flex items-center gap-1.5 px-1.5 py-1 mt-1 border-t border-border/30 text-[9px] text-muted-foreground/50 hover:text-muted-foreground/80 transition-colors cursor-pointer select-none"
          >
            <span
              className="text-[10px] leading-none transition-transform duration-150"
              style={{ transform: showUnavailable ? "rotate(90deg)" : "rotate(0deg)" }}
            >
              {"\u25b8"}
            </span>
            <span className="text-[9px] leading-none">
              {showUnavailable ? `Hide unavailable (${unavailable.length})` : `Unavailable models (${unavailable.length})`}
            </span>
          </button>
          {showUnavailable && unavailable.map((m) => (
            <CondensedModelRow
              key={m.logical}
              model={m}
              isSelected={selectedLogical === m.logical}
              onSelect={() => onSelect(m)}
              expanded={expandedLogical === m.logical}
              onToggleExpand={() => onToggleExpand(expandedLogical === m.logical ? null : m.logical)}
              dimmed
            />
          ))}
        </>
      )}

      {hiddenByFilter.length > 0 && (
        <>
          <button
            onClick={() => setShowHidden((v) => !v)}
            className="flex items-center gap-1.5 px-1.5 py-1 mt-1 border-t border-border/30 text-[9px] text-muted-foreground/50 hover:text-muted-foreground/80 transition-colors cursor-pointer select-none"
          >
            <span
              className="text-[10px] leading-none transition-transform duration-150"
              style={{ transform: showHidden ? "rotate(90deg)" : "rotate(0deg)" }}
            >
              {"\u25b8"}
            </span>
            <span className="text-[9px] leading-none">
              {showHidden ? `Hide ${hiddenByFilter.length} hidden` : `Hidden models (${hiddenByFilter.length})`}
            </span>
          </button>
          {showHidden && hiddenByFilter.map((m) => (
            <CondensedModelRow
              key={m.logical}
              model={m}
              isSelected={selectedLogical === m.logical}
              onSelect={() => onSelect(m)}
              expanded={expandedLogical === m.logical}
              onToggleExpand={() => onToggleExpand(expandedLogical === m.logical ? null : m.logical)}
              dimmed
            />
          ))}
        </>
      )}
    </div>
  );
}

// ── Main overlay ─────────────────────────────────────────────────────────
export function ModelSelectOverlay() {
  const {
    providers,
    loading,
    refreshing,
    error,
    totalModels,
    syncedAt,
    syncStatus,
    selectedModelId,
    selectedProviderName,
    overlayOpen,
    searchQuery,
    activeFilters,
    contextMin,
    hideUnavailable,
    pricingFilter,
    condensedModels,
    condensedView,
    fetchProviders,
    fetchCondensedModels,
    refreshProviders,
    selectModel,
    condensedSelect,
    closeOverlay,
    setSearchQuery,
    toggleFilter,
    setHideUnavailable,
    setPricingFilter,
    setContextMin,
    openProvidersDialog,
    setCondensedView,
  } = useModelStore();

  const liveCount = syncStatus.filter((s) => s.live).length;
  const totalCount = syncStatus.length;
  const syncedLabel = syncedAgoLabel(syncedAt);

  const inputRef = useRef<HTMLInputElement>(null);
  const [expandedLogical, setExpandedLogical] = useState<string | null>(null);

  // ── Search bar collapse/expand (per spec — tap icon to toggle). ──────
  const [searchExpanded, setSearchExpanded] = useState<boolean>(false);
  useEffect(() => {
    if (overlayOpen && searchExpanded) {
      // Focus the input when expanded.
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [overlayOpen, searchExpanded]);

  // ── Persisted per-provider expanded state (providers tab). ──────────
  const [providerBoxExpanded, setProviderBoxExpanded] = useState<Record<string, boolean>>(() =>
    loadProviderBoxExpanded(),
  );
  function toggleProviderBox(name: string) {
    setProviderBoxExpanded((m) => {
      const next = { ...m, [name]: !m[name] };
      saveProviderBoxExpanded(next);
      return next;
    });
  }

  // ── Persisted provider box order (drag-and-drop reorder). ───────────
  const [providerBoxOrder, setProviderBoxOrder] = useState<string[] | null>(() =>
    loadProviderBoxOrder(),
  );
  const [draggingProvider, setDraggingProvider] = useState<string | null>(null);
  const [dropTargetProvider, setDropTargetProvider] = useState<string | null>(null);

  function commitProviderBoxOrder(next: string[]) {
    setProviderBoxOrder(next);
    saveProviderBoxOrder(next);
  }
  function moveProviderUp(name: string) {
    const order = effectiveProviderOrder;
    const idx = order.indexOf(name);
    if (idx <= 0) return;
    const next = [...order];
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
    commitProviderBoxOrder(next);
  }
  function moveProviderDown(name: string) {
    const order = effectiveProviderOrder;
    const idx = order.indexOf(name);
    if (idx < 0 || idx >= order.length - 1) return;
    const next = [...order];
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
    commitProviderBoxOrder(next);
  }
  function onProviderDragStart(name: string, e: React.DragEvent) {
    setDraggingProvider(name);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", name);
  }
  function onProviderDragEnd() {
    setDraggingProvider(null);
    setDropTargetProvider(null);
  }
  function onProviderDragOver(name: string, e: React.DragEvent) {
    if (!draggingProvider) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (dropTargetProvider !== name) setDropTargetProvider(name);
  }
  function onProviderDrop(name: string, e: React.DragEvent) {
    e.preventDefault();
    if (!draggingProvider || draggingProvider === name) {
      setDraggingProvider(null);
      setDropTargetProvider(null);
      return;
    }
    const order = [...effectiveProviderOrder];
    const fromIdx = order.indexOf(draggingProvider);
    const toIdx = order.indexOf(name);
    if (fromIdx < 0 || toIdx < 0) return;
    const [moved] = order.splice(fromIdx, 1);
    order.splice(toIdx, 0, moved);
    commitProviderBoxOrder(order);
    setDraggingProvider(null);
    setDropTargetProvider(null);
  }

  // Debounce the search input.
  const [debouncedQuery, setDebouncedQuery] = useState(searchQuery);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(searchQuery), 200);
    return () => clearTimeout(t);
  }, [searchQuery]);

  useEffect(() => {
    if (providers.length === 0 && !loading) fetchProviders();
  }, [providers.length, loading, fetchProviders]);

  useEffect(() => {
    if (!overlayOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (expandedLogical) { setExpandedLogical(null); return; }
        if (searchExpanded) { setSearchExpanded(false); return; }
        closeOverlay();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [overlayOpen, closeOverlay, expandedLogical, searchExpanded]);

  useEffect(() => {
    if (!overlayOpen) {
      setExpandedLogical(null);
      // Auto-collapse search when overlay closes so re-opening starts clean.
      setSearchExpanded(false);
    }
  }, [overlayOpen]);

  const handleSelect = useCallback(
    (model: ProviderModel, providerName: string) => {
      selectModel(model.id, providerName, model.slotId);
    },
    [selectModel],
  );

  const handleCondensedSelect = useCallback(
    (model: CondensedModel) => {
      condensedSelect(model.logical);
    },
    [condensedSelect],
  );

  // Filter providers by pricing tier, then honor drag-and-drop reorder.
  const filteredProviders = useMemo(() => {
    if (pricingFilter === "free")
      return providers.filter((p) => p.name !== "opencode-go");
    return providers;
  }, [providers, pricingFilter]);

  const effectiveProviderOrder = useMemo(() => {
    if (!providerBoxOrder || providerBoxOrder.length === 0) {
      return filteredProviders.map((p) => p.name);
    }
    // Reorder by persisted order; append any new providers not in the saved order.
    const known = new Set(filteredProviders.map((p) => p.name));
    const ordered = providerBoxOrder.filter((n) => known.has(n));
    for (const p of filteredProviders) {
      if (!ordered.includes(p.name)) ordered.push(p.name);
    }
    return ordered;
  }, [filteredProviders, providerBoxOrder]);

  const orderedProviders = useMemo(() => {
    return effectiveProviderOrder
      .map((name) => filteredProviders.find((p) => p.name === name))
      .filter(Boolean) as ProviderGroup[];
  }, [effectiveProviderOrder, filteredProviders]);

  const filteredCount = useMemo(() => {
    const target = pricingFilter === "free" ? filteredProviders : providers;
    if (!debouncedQuery.trim() && activeFilters.length === 0 && contextMin === 0)
      return target.reduce((s, p) => s + p.models.length, 0);
    let count = 0;
    const q = debouncedQuery.toLowerCase().trim();
    for (const p of target) {
      for (const m of p.models) {
        const matchesSearch =
          !q ||
          m.displayName.toLowerCase().includes(q) ||
          m.id.toLowerCase().includes(q) ||
          (m.family?.toLowerCase().includes(q) ?? false);
        if (!matchesSearch) continue;
        if (modelMatchesFilters(m, activeFilters, contextMin)) count++;
      }
    }
    return count;
  }, [providers, filteredProviders, debouncedQuery, totalModels, activeFilters, contextMin, pricingFilter]);

  const selectedDisplayName = useMemo(() => {
    if (!selectedModelId) return null;
    for (const p of providers) {
      const m = p.models.find((m) => m.id === selectedModelId);
      if (m) return m.displayName;
    }
    for (const m of condensedModels) {
      if (m.logical === selectedModelId) return m.displayName;
      if (m.hosts.some((h) => h.modelId === selectedModelId)) return m.displayName;
    }
    return null;
  }, [selectedModelId, providers, condensedModels]);

  const anyFilterActive = activeFilters.length > 0 || contextMin > 0;

  // ── Slide-down-to-close gesture on the header. ──────────────────────
  // Pointer-down near the top of the panel + drag-down past threshold →
  // close. This replaces the previous "page refresh" behaviour.
  const dragDownRef = useRef<{ startY: number; active: boolean } | null>(null);
  function onHeaderPointerDown(e: React.PointerEvent) {
    // Only react to primary pointer (left mouse / touch).
    if (e.button !== 0 && e.pointerType === "mouse") return;
    dragDownRef.current = { startY: e.clientY, active: true };
  }
  function onHeaderPointerMove(e: React.PointerEvent) {
    if (!dragDownRef.current?.active) return;
    const dy = e.clientY - dragDownRef.current.startY;
    if (dy > 60) {
      // Past threshold — close + reset.
      dragDownRef.current.active = false;
      closeOverlay();
    }
  }
  function onHeaderPointerUp() {
    if (dragDownRef.current) dragDownRef.current.active = false;
  }

  return (
    <AnimatePresence>
      {overlayOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
            onClick={() => {
              if (expandedLogical) setExpandedLogical(null);
              else if (searchExpanded) setSearchExpanded(false);
              else closeOverlay();
            }}
            aria-hidden="true"
          />

          {/* Sheet */}
          <motion.div
            initial={{ opacity: 0, y: "100%" }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: "100%" }}
            transition={{ duration: 0.25, ease: [0.19, 1, 0.22, 1] }}
            className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-6 pointer-events-none"
          >
            <div
              className="pointer-events-auto w-full sm:max-w-3xl flex flex-col bg-background/97 backdrop-blur-xl shadow-2xl shadow-black/40 overflow-hidden rounded-t-3xl sm:rounded-2xl border-t sm:border border-border/70 max-h-[92dvh] sm:max-h-[82vh]"
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-label="Select a model"
            >
              {/* Mobile drag-handle — also serves as the slide-down-to-close
                  affordance. Pointer handlers are on the header below. */}
              <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
                <span className="w-10 h-1 rounded-full bg-muted-foreground/30" />
              </div>

              {/* ── Header — title + view toggle + close.
                  Also the slide-down-to-close gesture target. ───────── */}
              <div
                onPointerDown={onHeaderPointerDown}
                onPointerMove={onHeaderPointerMove}
                onPointerUp={onHeaderPointerUp}
                onPointerCancel={onHeaderPointerUp}
                className="flex items-center gap-2 px-3 sm:px-4 py-2 shrink-0 border-b border-border/60 touch-none"
              >
                <span className="text-[14px] font-semibold text-foreground shrink-0 select-none">Select a model</span>
                <div className="flex items-center gap-1 ml-1">
                  <button
                    onClick={() => setCondensedView(false)}
                    className={`touch-target text-[11px] leading-none px-2.5 h-6 rounded-full border transition-colors shrink-0 ${!condensedView ? "bg-accent/15 border-accent/40 text-accent font-medium" : "border-border text-muted-foreground hover:text-foreground"}`}
                    aria-pressed={!condensedView}
                  >
                    Providers
                  </button>
                  <button
                    onClick={() => setCondensedView(true)}
                    className={`touch-target text-[11px] leading-none px-2.5 h-6 rounded-full border transition-colors shrink-0 ${condensedView ? "bg-accent/15 border-accent/40 text-accent font-medium" : "border-border text-muted-foreground hover:text-foreground"}`}
                    aria-pressed={condensedView}
                  >
                    Models
                  </button>
                </div>
                <div className="flex-1" />
                {/* Sync + privacy quick-actions (desktop only). */}
                {!loading && providers.length > 0 && (
                  <div className="hidden sm:flex items-center gap-1">
                    <button
                      onClick={() => openProvidersDialog()}
                      className="touch-target inline-flex items-center gap-1 px-2 h-7 rounded-xl text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
                      title="Providers & privacy settings"
                    >
                      <IcoSettings />
                      <span>Privacy</span>
                    </button>
                    <button
                      onClick={() => condensedView ? fetchCondensedModels(true) : refreshProviders()}
                      disabled={refreshing && !condensedView}
                      className="touch-target inline-flex items-center gap-1 px-2 h-7 rounded-xl text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-60 shrink-0"
                      title={condensedView ? "Refresh condensed catalog" : (refreshing ? "syncing" : `${syncedLabel} \u00b7 ${liveCount}/${totalCount} providers live \u00b7 click to re-sync`)}
                    >
                      <IcoRefresh spinning={refreshing && !condensedView} />
                      <span>{condensedView ? "refresh" : (refreshing ? "syncing" : syncedLabel || "sync")}</span>
                    </button>
                  </div>
                )}
                {/* Pricing pills — desktop only. */}
                {!loading && providers.length > 0 && (
                  <div className="hidden sm:flex items-center gap-1 ml-1">
                    <span className="w-px h-4 bg-border/40 mx-0.5 shrink-0" />
                    <button
                      onClick={() => setPricingFilter("free")}
                      className="touch-target text-[11px] leading-none px-2 h-6 rounded-full border transition-colors"
                      style={{
                        borderColor: pricingFilter === "free" ? "#22c55e50" : "var(--border)",
                        backgroundColor: pricingFilter === "free" ? "#22c55e15" : "transparent",
                        color: pricingFilter === "free" ? "#22c55e" : "var(--muted-foreground)",
                      }}
                      aria-pressed={pricingFilter === "free"}
                    >
                      Free
                    </button>
                    <button
                      onClick={() => setPricingFilter("paid")}
                      className="touch-target text-[11px] leading-none px-2 h-6 rounded-full border transition-colors"
                      style={{
                        borderColor: pricingFilter === "paid" ? "#f59e0b50" : "var(--border)",
                        backgroundColor: pricingFilter === "paid" ? "#f59e0b15" : "transparent",
                        color: pricingFilter === "paid" ? "#f59e0b" : "var(--muted-foreground)",
                      }}
                      aria-pressed={pricingFilter === "paid"}
                    >
                      Paid
                    </button>
                  </div>
                )}
                {/* Close */}
                <button
                  onClick={closeOverlay}
                  className="touch-target flex items-center justify-center size-9 rounded-xl text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
                  aria-label="Close model select"
                >
                  <IcoX />
                </button>
              </div>

              {/* ── Search bar — smaller height + collapse/expand icon. ─ */}
              <div className="px-3 sm:px-4 py-1.5 shrink-0 border-b border-border/60 bg-surface/40">
                <div className="flex items-center gap-1.5">
                  {/* Toggle button — expands/collapses the input. */}
                  <button
                    onClick={() => setSearchExpanded((v) => !v)}
                    className={`touch-target inline-flex items-center justify-center size-9 rounded-xl border transition-colors shrink-0 ${
                      searchExpanded
                        ? "bg-accent/15 border-accent/40 text-accent"
                        : "border-border text-muted-foreground hover:text-foreground hover:bg-surface2/60"
                    }`}
                    aria-label={searchExpanded ? "Collapse search" : "Expand search"}
                    aria-expanded={searchExpanded}
                    title={searchExpanded ? "Collapse search" : "Expand search"}
                  >
                    <IcoSearch />
                  </button>
                  {searchExpanded && (
                    <div className="relative flex-1">
                      <input
                        ref={inputRef}
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder="Search models, providers, capabilities…"
                        className="w-full pl-3 pr-7 h-9 text-[14px] sm:text-[13px] bg-surface2 border border-border rounded-xl outline-none focus:border-accent focus:bg-surface3 transition-colors placeholder:text-muted-foreground/60"
                        aria-label="Search models"
                      />
                      {searchQuery && (
                        <button
                          onClick={() => setSearchQuery("")}
                          className="touch-target absolute right-1 top-1/2 -translate-y-1/2 inline-flex items-center justify-center size-6 rounded-lg text-muted-foreground hover:text-foreground hover:bg-surface3 transition-colors"
                          aria-label="Clear search"
                        >
                          <IcoX />
                        </button>
                      )}
                    </div>
                  )}
                  {!searchExpanded && (
                    <span className="text-[11px] text-muted-foreground/60 truncate flex-1">
                      Tap search icon to filter
                    </span>
                  )}
                </div>
              </div>

              {/* ── Filter chip row — 2× smaller, horizontally scrollable. */}
              {providers.length > 0 && (
                <div className="flex items-center gap-1 px-3 sm:px-4 py-1.5 shrink-0 overflow-x-auto no-scrollbar border-b border-border/60">
                  {FILTER_PILLS.map((f) => (
                    <button
                      key={f.id}
                      onClick={() => toggleFilter(f.id)}
                      className="touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-full border transition-colors inline-flex items-center"
                      style={{
                        borderColor: activeFilters.includes(f.id) ? `${f.color}50` : "var(--border)",
                        backgroundColor: activeFilters.includes(f.id) ? `${f.color}15` : "transparent",
                        color: activeFilters.includes(f.id) ? f.color : "var(--muted-foreground)",
                      }}
                      aria-pressed={activeFilters.includes(f.id)}
                    >
                      {f.label}
                    </button>
                  ))}
                  <span className="w-px h-3 bg-border/40 mx-0.5 shrink-0" />
                  {CONTEXT_OPTIONS.map((c) => (
                    <button
                      key={c.value}
                      onClick={() => setContextMin(c.value)}
                      className="touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-full border transition-colors inline-flex items-center"
                      style={{
                        borderColor: contextMin === c.value ? "#14b8a680" : "var(--border)",
                        backgroundColor: contextMin === c.value ? "#14b8a615" : "transparent",
                        color: contextMin === c.value ? "#14b8a6" : "var(--muted-foreground)",
                      }}
                      aria-pressed={contextMin === c.value}
                    >
                      {c.label}
                    </button>
                  ))}
                  {/* Pricing pills — mobile-only here (desktop has them in header). */}
                  <span className="sm:hidden w-px h-3 bg-border/40 mx-0.5 shrink-0" />
                  <button
                    onClick={() => setPricingFilter("free")}
                    className="sm:hidden touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-full border transition-colors inline-flex items-center"
                    style={{
                      borderColor: pricingFilter === "free" ? "#22c55e50" : "var(--border)",
                      backgroundColor: pricingFilter === "free" ? "#22c55e15" : "transparent",
                      color: pricingFilter === "free" ? "#22c55e" : "var(--muted-foreground)",
                    }}
                    aria-pressed={pricingFilter === "free"}
                  >
                    Free
                  </button>
                  <button
                    onClick={() => setPricingFilter("paid")}
                    className="sm:hidden touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-full border transition-colors inline-flex items-center"
                    style={{
                      borderColor: pricingFilter === "paid" ? "#f59e0b50" : "var(--border)",
                      backgroundColor: pricingFilter === "paid" ? "#f59e0b15" : "transparent",
                      color: pricingFilter === "paid" ? "#f59e0b" : "var(--muted-foreground)",
                    }}
                    aria-pressed={pricingFilter === "paid"}
                  >
                    Paid
                  </button>
                  {anyFilterActive && (
                    <button
                      onClick={() => { activeFilters.forEach((f) => toggleFilter(f)); setContextMin(0); }}
                      className="touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-lg text-muted-foreground/70 hover:text-foreground hover:bg-muted/40 transition-colors ml-auto"
                    >
                      clear
                    </button>
                  )}
                  {condensedView && (
                    <>
                      <button
                        onClick={() => setHideUnavailable(!hideUnavailable)}
                        className={`touch-target shrink-0 text-[10px] leading-none px-2 h-5 rounded-full border transition-colors inline-flex items-center ${anyFilterActive ? "" : "ml-auto"}`}
                        style={{
                          borderColor: hideUnavailable ? "#22c55e50" : "var(--border)",
                          backgroundColor: hideUnavailable ? "#22c55e15" : "transparent",
                          color: hideUnavailable ? "#22c55e" : "var(--muted-foreground)",
                        }}
                        aria-pressed={hideUnavailable}
                      >
                        {hideUnavailable ? "Available" : "All"}
                      </button>
                      <span className="text-[9px] text-muted-foreground/60 shrink-0 tabular-nums">
                        {condensedModels.length} models
                      </span>
                    </>
                  )}
                  {!condensedView && !loading && (
                    <span className={`text-[9px] text-muted-foreground/60 shrink-0 tabular-nums ${anyFilterActive ? "" : "ml-auto"}`}>
                      {filteredCount} models
                    </span>
                  )}
                </div>
              )}

              {/* ── List area — flex-1 scroll. ─────────────────────────── */}
              <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain">
                {!condensedView && loading && (
                  <div className="flex items-center justify-center h-full py-12">
                    <span className="inline-block size-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
                    <span className="ml-2 text-[12px] text-muted-foreground">fetching providers…</span>
                  </div>
                )}

                {condensedView && loading && (
                  <div className="flex items-center justify-center h-full py-12">
                    <span className="inline-block size-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
                    <span className="ml-2 text-[12px] text-muted-foreground">fetching models…</span>
                  </div>
                )}

                {!condensedView && error && (
                  <div className="flex flex-col items-center justify-center h-full gap-3 py-12">
                    <span className="text-[13px] text-destructive">{error}</span>
                    <button
                      onClick={fetchProviders}
                      className="touch-target text-[12px] text-accent underline underline-offset-2 hover:text-accentLight px-3 h-9"
                    >
                      retry
                    </button>
                  </div>
                )}

                {!condensedView && !loading && !error && (
                  <div className="p-3 sm:p-4">
                    {/* Single-column list — fixed overlap issues from the
                        old `sm:columns-2` masonry layout. */}
                    <div className="flex flex-col gap-2">
                      {orderedProviders.map((p, idx) => {
                        const isExpanded = !!providerBoxExpanded[p.name];
                        return (
                          <ProviderBox
                            key={p.name}
                            provider={p}
                            selectedModelId={selectedModelId}
                            selectedProviderName={selectedProviderName}
                            condensedView={condensedView}
                            onSelect={handleSelect}
                            searchQuery={debouncedQuery}
                            activeFilters={activeFilters}
                            contextMin={contextMin}
                            expanded={isExpanded}
                            onToggleExpand={() => toggleProviderBox(p.name)}
                            dragHandleProps={{
                              draggable: true,
                              onDragStart: (e) => onProviderDragStart(p.name, e),
                              onDragEnd: onProviderDragEnd,
                            }}
                            onMoveUp={() => moveProviderUp(p.name)}
                            onMoveDown={() => moveProviderDown(p.name)}
                            isFirst={idx === 0}
                            isLast={idx === orderedProviders.length - 1}
                            isDragTarget={dropTargetProvider === p.name && draggingProvider !== null && draggingProvider !== p.name}
                            onDragOverBox={(e) => onProviderDragOver(p.name, e)}
                            onDropOnBox={(e) => onProviderDrop(p.name, e)}
                          />
                        );
                      })}
                    </div>
                  </div>
                )}

                {condensedView && !loading && (
                  <div className="p-3 sm:p-4 relative">
                    <CondensedList
                      models={condensedModels}
                      searchQuery={debouncedQuery}
                      activeFilters={activeFilters}
                      contextMin={contextMin}
                      hideUnavailable={hideUnavailable}
                      selectedModelId={selectedModelId}
                      onSelect={handleCondensedSelect}
                      expandedLogical={expandedLogical}
                      onToggleExpand={setExpandedLogical}
                    />
                  </div>
                )}
              </div>

              {/* ── Footer — selected model + esc hint ─────────────────── */}
              <div className="flex items-center justify-between gap-2 px-3 sm:px-4 py-2 shrink-0 border-t border-border/60 bg-surface/40 safe-bottom">
                <span className="text-[10px] sm:text-[11px] text-muted-foreground truncate">
                  <span className="hidden sm:inline">tap to select &nbsp;</span>
                  <kbd className="px-1 py-px rounded bg-muted border border-border text-[9px] font-mono">esc</kbd> to close
                  {totalCount > 0 ? ` &nbsp;${totalCount} provider${totalCount === 1 ? "" : "s"} &nbsp;synced ${liveCount}/${totalCount}` : ""}
                </span>
                {selectedDisplayName && (
                  <span className="text-[11px] sm:text-[12px] text-accent font-medium flex items-center gap-1 shrink-0">
                    <IcoDot />
                    <span className="truncate max-w-[40vw] sm:max-w-[200px]">{selectedDisplayName}</span>
                  </span>
                )}
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
