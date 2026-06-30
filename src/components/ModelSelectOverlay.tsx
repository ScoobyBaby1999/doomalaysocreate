import { useEffect, useMemo, useCallback, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useModelStore, type ProviderGroup, type ProviderModel, type ModelAttributes, type CondensedHost, type CondensedModel } from "../lib/model-store";

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
  { id: "reasoning", label: "Reasoning", color: "#f97316" },
  { id: "intelligence", label: "Intelligence", color: "#a855f7" },
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

function ModelRow({
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
      className={`
        flex flex-col w-full text-left rounded px-1.5 transition-colors duration-100 cursor-pointer
        ${isSelected ? "bg-primary/10" : dimmed ? "" : "hover:bg-muted/60"}
        ${dimmed && !isSelected ? "opacity-35" : ""}
      `}
      style={{ paddingTop: 3, paddingBottom: hasSub ? 3 : 3 }}
    >
      <span className="flex items-center w-full" style={{ height: 20 }}>
        <span
          className={`flex-shrink-0 flex items-center justify-center rounded-full mr-2 ${isSelected ? "text-primary-foreground" : "text-transparent"}`}
          style={{
            width: 14,
            height: 14,
            fontSize: 0,
            border: isSelected ? "none" : "1.5px solid var(--border)",
            backgroundColor: isSelected ? "var(--primary)" : "transparent",
          }}
        >
          {isSelected && <IcoCheck />}
        </span>

        <span
          className={`text-[11px] leading-none truncate flex-1 ${isSelected ? "text-primary font-medium" : "text-foreground"}`}
          title={model.id}
        >
          {model.displayName}
        </span>

        <span className="flex-shrink-0 ml-1.5 text-[9px] text-muted-foreground tabular-nums">
          {fmtCtx(model.contextLength)}
        </span>
      </span>

      {hasSub && (
        <div className="flex flex-wrap gap-x-1 gap-y-px mt-0.5 pl-6 pr-1">
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
}

function ProviderBox({
  provider,
  selectedModelId,
  selectedProviderName,
  condensedView,
  onSelect,
  searchQuery,
  activeFilters,
  contextMin,
}: {
  provider: ProviderGroup;
  selectedModelId: string | null;
  selectedProviderName: string | null;
  condensedView: boolean;
  onSelect: (model: ProviderModel, providerName: string) => void;
  searchQuery: string;
  activeFilters: string[];
  contextMin: number;
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

  const openSettings = useCallback(() => {
    useModelStore.getState().openProvidersDialog(provider.name);
  }, [provider.name]);

  const hasDimmed = dimmed.length > 0;
  const empty = matched.length === 0 && !hasDimmed;

  return (
    <div
      className="flex flex-col rounded-lg border overflow-hidden"
      style={{ borderColor: `${provider.color}30` }}
    >
      <div
        className="flex items-center gap-2 px-2.5 shrink-0"
        style={{ height: 26, backgroundColor: `${provider.color}0d`, borderBottom: `1px solid ${provider.color}1f` }}
      >
        <span className="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: provider.color }} />
        <span className="text-[11px] font-semibold text-foreground leading-none truncate">{provider.displayName}</span>
        <span
          className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
          style={{ backgroundColor: provider.syncedLive ? "#22c55e" : "#f59e0b" }}
          title={provider.syncedLive ? "synced live from provider API" : "config-sourced (no public live API)"}
        />
        <span className="text-[10px] text-muted-foreground leading-none ml-auto tabular-nums">{provider.models.length}</span>
        <button
          onClick={openSettings}
          className="flex items-center justify-center size-5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
          aria-label={`Manage ${provider.displayName} privacy & keys`}
          title={`${provider.manageLabel} \u2197`}
        >
          <IcoSettings />
        </button>
      </div>

      <button
        onClick={openSettings}
        className="flex items-center gap-1.5 px-2.5 shrink-0 text-left hover:brightness-95 dark:hover:brightness-110 transition-all"
        style={{
          height: 24,
          borderBottom: `1px solid ${confidenceDotColor(provider.privacy.confidence)}25`,
          backgroundColor: `${confidenceDotColor(provider.privacy.confidence)}0d`,
        }}
        title={provider.privacy.notice}
      >
        <span className="shrink-0" style={{ color: confidenceDotColor(provider.privacy.confidence) }}>
          <IcoShield />
        </span>
        <span className="text-[9px] leading-none text-muted-foreground truncate flex-1">
          {provider.privacy.notice}
        </span>
      </button>

      <div className="flex flex-col gap-px p-1 max-h-[340px] overflow-y-auto">
        {empty ? (
          <div className="flex items-center justify-center h-10 text-[10px] text-muted-foreground/50">
            no models synced
          </div>
        ) : (
          <>
{matched.map((m) => (
              <ModelRow key={m.id} model={m} isSelected={selectedModelId === m.id && (!condensedView ? selectedProviderName === provider.name : true)} onSelect={() => onSelect(m, provider.name)} />
            ))}
            {hasDimmed && (
              <>
                <button
                  onClick={() => setShowDimmed((v) => !v)}
                  className="flex items-center gap-1.5 px-1.5 py-1 mt-0.5 border-t border-border/30 text-[9px] text-muted-foreground/50 hover:text-muted-foreground/80 transition-colors cursor-pointer select-none"
                >
                  <span
                    className="text-[10px] leading-none transition-transform duration-150"
                    style={{ transform: showDimmed ? "rotate(90deg)" : "rotate(0deg)" }}
                  >
                    {"\u25b8"}
                  </span>
                  <span className="text=[9px] leading-none">
                    {showDimmed ? `Hide ${dimmed.length} dimmed` : `Show ${dimmed.length} dimmed`}
                  </span>
                </button>
                {showDimmed && dimmed.map((m) => (
                  <ModelRow key={m.id} model={m} isSelected={selectedModelId === m.id && (!condensedView ? selectedProviderName === provider.name : true)} onSelect={() => onSelect(m, provider.name)} dimmed />
                ))}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function CondensedModelRow({
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

  function moveUp(idx: number) {
    if (idx <= 0) return;
    const next = [...localOrder];
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
    setLocalOrder(next);
    useModelStore.getState().setProviderPriority(model.logical, next.map((h) => h.provider));
  }

  function moveDown(idx: number) {
    if (idx >= localOrder.length - 1) return;
    const next = [...localOrder];
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
    setLocalOrder(next);
    useModelStore.getState().setProviderPriority(model.logical, next.map((h) => h.provider));
  }

  const topHost = localOrder[0];

  return (
    <div className="flex flex-col w-full text-left rounded px-1.5 transition-colors duration-100"
      style={{ paddingTop: 3, paddingBottom: hasSub ? 3 : 3 }}
    >
      <span className="flex items-center w-full" style={{ height: 20 }}>
        <button onClick={onSelect} className="flex items-center flex-1 min-w-0 cursor-pointer text-left">
          <span
            className={`flex-shrink-0 flex items-center justify-center rounded-full mr-2 ${isSelected ? "text-primary-foreground" : "text-transparent"}`}
            style={{ width: 14, height: 14, fontSize: 0, border: isSelected ? "none" : "1.5px solid var(--border)", backgroundColor: isSelected ? "var(--primary)" : "transparent" }}
          >
            {isSelected && <IcoCheck />}
          </span>
          <span
            className={`text-[11px] leading-none truncate flex-1 ${isSelected ? "text-primary font-medium" : dimmed ? "text-muted-foreground/60" : "text-foreground"}`}
            title={model.logical}
          >
            {model.displayName}
          </span>
          <span className="flex-shrink-0 mr-2 text-[9px] text-muted-foreground tabular-nums">
            {fmtCtx(model.contextLength)}
          </span>
        </button>
      </span>

      {topHost && !expanded && (
        <button onClick={onToggleExpand}
          className="flex items-center gap-1.5 w-full pl-6 pr-1 py-0.5 rounded text-[10px] transition-colors hover:bg-muted/30 cursor-pointer text-left"
          title={localOrder.length > 1 ? "Click to show all provider options" : "Only one provider available"}
        >
          <span className="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: topHost.color }} />
          <span className="truncate text-foreground/80 min-w-0">{topHost.modelId}</span>
          <span className="tabular-nums text-muted-foreground/60 shrink-0">{fmtCtx(topHost.contextLength)}</span>
          <span className="text-muted-foreground/50 shrink-0">{topHost.providerDisplayName}</span>
          {localOrder.length > 1 && <span className="ml-auto text-muted-foreground/40 text-[9px] shrink-0">{"\u25be"}</span>}
        </button>
      )}

      {expanded && (
        <div className="flex flex-col gap-0.5 pl-6 pr-1 mt-0.5">
          {localOrder.map((host, i) => (
            <div key={`${host.provider}-${host.modelId}`}
              className="flex items-center gap-1.5 py-0.5 rounded text-[10px]"
              style={{ opacity: host.hasApiKey ? 1 : 0.4 }}
            >
              <span className="inline-flex items-center justify-center size-3.5 rounded-full text-[7px] font-bold shrink-0"
                style={{ backgroundColor: `${host.color}30`, color: host.color }}>
                {i + 1}
              </span>
              <span className="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: host.color }} />
              <span className="truncate text-foreground/80 flex-1 min-w-0">{host.modelId}</span>
              <span className="tabular-nums text-muted-foreground/60 shrink-0 mr-1">{fmtCtx(host.contextLength)}</span>
              <span className="text-muted-foreground/50 shrink-0">{host.providerDisplayName}</span>
              <div className="flex gap-px ml-1 shrink-0">
                <button
                  onClick={(e) => { e.stopPropagation(); moveUp(i); }}
                  disabled={i === 0}
                  className="size-4 flex items-center justify-center rounded text-muted-foreground/50 hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-20 disabled:cursor-default"
                  title="Higher priority"
                >
                  <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m18 15-6-6-6 6"/></svg>
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); moveDown(i); }}
                  disabled={i === localOrder.length - 1}
                  className="size-4 flex items-center justify-center rounded text-muted-foreground/50 hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-20 disabled:cursor-default"
                  title="Lower priority"
                >
                  <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"><path d="m6 9 6 6 6-6"/></svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {hasSub && (
        <div className="flex flex-wrap gap-x-1 gap-y-px mt-0.5 pl-6 pr-1">
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
}

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
    <div className="flex flex-col gap-px p-1">
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
    setContextMin,
    openProvidersDialog,
    setCondensedView,
  } = useModelStore();

  const liveCount = syncStatus.filter((s) => s.live).length;
  const totalCount = syncStatus.length;
  const syncedLabel = syncedAgoLabel(syncedAt);

  const inputRef = useRef<HTMLInputElement>(null);
  const [expandedLogical, setExpandedLogical] = useState<string | null>(null);

  useEffect(() => {
    if (providers.length === 0 && !loading) fetchProviders();
  }, [providers.length, loading, fetchProviders]);

  useEffect(() => {
    if (!overlayOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (expandedLogical) { setExpandedLogical(null); return; }
        closeOverlay();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [overlayOpen, closeOverlay, expandedLogical]);

  useEffect(() => {
    if (!overlayOpen) setExpandedLogical(null);
  }, [overlayOpen]);

  const handleSelect = useCallback(
    (model: ProviderModel, providerName: string) => {
      selectModel(model.id, providerName, model.slotId);
    },
    [selectModel]
  );

  const handleCondensedSelect = useCallback(
    (model: CondensedModel) => {
      condensedSelect(model.logical);
    },
    [condensedSelect]
  );

  const filteredCount = useMemo(() => {
    if (!searchQuery.trim() && activeFilters.length === 0 && contextMin === 0) return totalModels;
    let count = 0;
    const q = searchQuery.toLowerCase().trim();
    for (const p of providers) {
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
  }, [providers, searchQuery, totalModels, activeFilters, contextMin]);

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

  return (
    <AnimatePresence>
      {overlayOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm"
            onClick={() => { if (expandedLogical) setExpandedLogical(null); else closeOverlay(); }}
            aria-hidden="true"
          />

          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.19, 1, 0.22, 1] }}
            className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6 pointer-events-none"
          >
            <div
              className="pointer-events-auto w-full max-w-3xl flex flex-col rounded-xl border border-border/70 bg-background/95 backdrop-blur-xl shadow-2xl shadow-black/20 overflow-hidden"
              style={{ height: "min(82vh, 640px)" }}
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-label="Select a model"
            >
              <div className="flex items-center justify-between gap-3 px-3.5 shrink-0" style={{ height: 40, borderBottom: "1px solid var(--border)" }}>
                <div className="flex items-center gap-1.5 min-w-0">
                  <span className="text-[12px] font-semibold text-foreground shrink-0">Select</span>
                  <button
                    onClick={() => setCondensedView(false)}
                    className={`text-[10px] leading-none px-2 py-0.5 rounded-full border transition-colors shrink-0 ${!condensedView ? "bg-foreground/10 border-foreground/30 text-foreground font-medium" : "border-transparent text-muted-foreground hover:text-foreground"}`}
                  >
                    Providers
                  </button>
                  <button
                    onClick={() => setCondensedView(true)}
                    className={`text-[10px] leading-none px-2 py-0.5 rounded-full border transition-colors shrink-0 ${condensedView ? "bg-foreground/10 border-foreground/30 text-foreground font-medium" : "border-transparent text-muted-foreground hover:text-foreground"}`}
                  >
                    Models
                  </button>
                  {!condensedView && !loading && providers.length > 0 && (
                    <>
                      <button
                        onClick={() => openProvidersDialog()}
                        className="inline-flex items-center gap-1 ml-0.5 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
                        title="Providers & privacy settings"
                      >
                        <IcoSettings />
                        <span className="hidden sm:inline">Privacy</span>
                      </button>
                      <button
                        onClick={() => refreshProviders()}
                        disabled={refreshing}
                        className="inline-flex items-center gap-1 ml-0.5 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-60 shrink-0"
                         title={refreshing ? "syncing" : `${syncedLabel} \u00b7 ${liveCount}/${totalCount} providers live \u00b7 click to re-sync`}
                      >
                        <IcoRefresh spinning={refreshing} />
                        <span className="hidden md:inline">{refreshing ? "syncing" : syncedLabel || "sync"}</span>
                      </button>
                    </>
                  )}
                  {condensedView && (
                    <>
                      <button
                        onClick={() => openProvidersDialog()}
                        className="inline-flex items-center gap-1 ml-0.5 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
                        title="Providers & privacy settings"
                      >
                        <IcoSettings />
                        <span className="hidden sm:inline">Privacy</span>
                      </button>
                      <button
                        onClick={() => fetchCondensedModels(true)}
                        className="inline-flex items-center gap-1 ml-0.5 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors shrink-0"
                        title="Refresh condensed catalog"
                      >
                        <IcoRefresh />
                        <span className="hidden md:inline">refresh</span>
                      </button>
                    </>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <div className="relative">
                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground/60">
                      <IcoSearch />
                    </span>
                    <input
                      ref={inputRef}
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      placeholder="search"
                      className="pl-8 pr-3 h-7 w-32 sm:w-44 text-[12px] bg-muted/20 border border-border/50 rounded-lg outline-none focus:border-ring/40 focus:bg-muted/40 transition-colors placeholder:text-muted-foreground/50"
                    />
                  </div>

                  <button
                    onClick={closeOverlay}
                    className="flex items-center justify-center size-6 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                    aria-label="Close"
                  >
                    <IcoX />
                  </button>
                </div>
              </div>

              {providers.length > 0 && (
                <div className="flex items-center gap-1.5 px-3.5 py-1 shrink-0 overflow-x-auto border-b border-border/30" style={{ height: 30 }}>
                  {FILTER_PILLS.map((f) => (
                    <button
                      key={f.id}
                      onClick={() => toggleFilter(f.id)}
                      className="shrink-0 text-[10px] leading-none px-2 py-0.5 rounded-full border transition-colors"
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
                      className="shrink-0 text-[10px] leading-none px-2 py-0.5 rounded-full border transition-colors"
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
                  {anyFilterActive && (
                    <button
                      onClick={() => { activeFilters.forEach((f) => toggleFilter(f)); setContextMin(0); }}
                      className="shrink-0 text-[9px] leading-none px-1.5 py-0.5 rounded text-muted-foreground/60 hover:text-foreground hover:bg-muted/40 transition-colors ml-auto"
                    >
                      clear
                    </button>
                  )}
                  {condensedView && (
                    <>
                      <button
                        onClick={() => setHideUnavailable(!hideUnavailable)}
                        className="shrink-0 text-[10px] leading-none px-2 py-0.5 rounded-full border transition-colors"
                        style={{
                          borderColor: hideUnavailable ? "#22c55e50" : "var(--border)",
                          backgroundColor: hideUnavailable ? "#22c55e15" : "transparent",
                          color: hideUnavailable ? "#22c55e" : "var(--muted-foreground)",
                        }}
                        aria-pressed={hideUnavailable}
                      >
                        {hideUnavailable ? "Available" : "All models"}
                      </button>
                      <span className="text-[9px] text-muted-foreground/50 shrink-0 tabular-nums ml-auto">
                        {condensedModels.length} models
                      </span>
                    </>
                  )}
                  {!condensedView && !loading && (
                    <span className="text-[9px] text-muted-foreground/50 ml-auto shrink-0 tabular-nums">
                      {filteredCount} models
                    </span>
                  )}
                </div>
              )}

              <div className="flex-1 min-h-0 overflow-y-auto">
                {!condensedView && loading && (
                  <div className="flex items-center justify-center h-full">
                    <span className="inline-block size-4 border-2 border-muted-foreground/30 border-t-muted-foreground rounded-full animate-spin" />
                    <span className="ml-2 text-[11px] text-muted-foreground">fetching</span>
                  </div>
                )}

                {condensedView && loading && (
                  <div className="flex items-center justify-center h-full">
                    <span className="inline-block size-4 border-2 border-muted-foreground/30 border-t-muted-foreground rounded-full animate-spin" />
                    <span className="ml-2 text-[11px] text-muted-foreground">fetching</span>
                  </div>
                )}

                {!condensedView && error && (
                  <div className="flex flex-col items-center justify-center h-full gap-2">
                    <span className="text-[11px] text-destructive">{error}</span>
                    <button
                      onClick={fetchProviders}
                      className="text-[11px] text-foreground underline underline-offset-2 hover:text-primary"
                    >
                      retry
                    </button>
                  </div>
                )}

                {!condensedView && !loading && !error && (
                  <div className="p-3 overflow-y-auto max-h-full">
                    <div className="sm:columns-2 columns-1" style={{ columnGap: '0.75rem' }}>
                      {providers.map((p) => (
                        <div key={p.name} className="break-inside-avoid mb-3 min-w-0">
                          <ProviderBox
                            provider={p}
                            selectedModelId={selectedModelId}
                            selectedProviderName={selectedProviderName}
                            condensedView={condensedView}
                            onSelect={handleSelect}
                            searchQuery={searchQuery}
                            activeFilters={activeFilters}
                            contextMin={contextMin}
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {condensedView && !loading && (
                  <div className="p-3 relative">
                    <CondensedList
                      models={condensedModels}
                      searchQuery={searchQuery}
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

              <div className="flex items-center justify-between px-3.5 shrink-0" style={{ height: 30, borderTop: "1px solid var(--border)" }}>
                <span className="text-[10px] text-muted-foreground truncate">
                  click to select &nbsp;<kbd className="px-1 py-px rounded bg-muted border border-border text-[9px] font-mono">esc</kbd> to close{totalCount > 0 ? ` &nbsp;${totalCount} provider${totalCount === 1 ? "" : "s"} &nbsp;synced ${liveCount}/${totalCount}` : ""}
                </span>
                {selectedDisplayName && (
                  <span className="text-[10px] text-primary font-medium flex items-center gap-1">
                    <IcoDot />
                    {selectedDisplayName}
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
