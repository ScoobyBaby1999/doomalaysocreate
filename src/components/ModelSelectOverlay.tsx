import { useEffect, useMemo, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useModelStore, type ProviderGroup, type ProviderModel, type ModelAttributes } from "../lib/model-store";

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

function bestFilterScore(model: ProviderModel, activeFilters: string[]): number {
  const bm = model.attributes?.benchmarks;
  let best = 0;
  for (const filter of activeFilters) {
    if (filter === "reasoning") best = Math.max(best, bm?.intelligence ?? 0);
    if (filter === "code") best = Math.max(best, bm?.coding ?? 0, bm?.aaCoding ?? 0);
    if (filter === "tools") best = Math.max(best, bm?.agentic ?? 0);
  }
  return best;
}

function sortByFilterScore(models: ProviderModel[], activeFilters: string[]): ProviderModel[] {
  if (activeFilters.length === 0) return models;
  return [...models].sort((a, b) => bestFilterScore(b, activeFilters) - bestFilterScore(a, activeFilters));
}

function defaultSort(models: ProviderModel[]): ProviderModel[] {
  return [...models].sort((a, b) => {
    const aCode = a.attributes?.benchmarks?.coding ?? 0;
    const bCode = b.attributes?.benchmarks?.coding ?? 0;
    const aIntel = a.attributes?.benchmarks?.intelligence ?? 0;
    const bIntel = b.attributes?.benchmarks?.intelligence ?? 0;

    const aTier = aCode >= 20 ? 0 : aIntel >= 20 ? 1 : 2;
    const bTier = bCode >= 20 ? 0 : bIntel >= 20 ? 1 : 2;

    if (aTier !== bTier) return aTier - bTier;
    if (aTier === 0) return bCode - aCode;
    if (aTier === 1) return bIntel - aIntel;
    return 0;
  });
}

const FILTER_PILLS = [
  { id: "reasoning", label: "Reasoning", color: "#f97316" },
  { id: "code", label: "Code", color: "#3b82f6" },
  { id: "tools", label: "Tools", color: "#8b5cf6" },
  { id: "vision", label: "Vision", color: "#22c55e" },
  { id: "speech", label: "Speech", color: "#ec4899" },
] as const;

const CONTEXT_OPTIONS = [
  { value: 0, label: "Any" },
  { value: 32768, label: "32K" },
  { value: 131072, label: "128K" },
  { value: 1048576, label: "1M" },
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
        <span className="flex items-center w-full pl-6 pr-1 mt-0.5 min-h-[14px] overflow-hidden">
          {segs && (
            <span className="text-[9px] leading-none truncate" title={segs.map((s) => s.text).join("")}>
              {segs.map((s, i) => (
                <span key={i} style={s.color ? { color: dimmed ? undefined : s.color } : undefined}>
                  {s.text}
                </span>
              ))}
            </span>
          )}
          {note && (
            <span
              className={`text-[9px] leading-none truncate shrink-0 ${segs ? "ml-1.5" : ""} ${note.startsWith("\u26a0") ? "text-amber-600 dark:text-amber-500" : "text-muted-foreground/70"}`}
              title={note}
            >
              {note}
            </span>
          )}
        </span>
      )}
    </button>
  );
}

function ProviderBox({
  provider,
  selectedModelId,
  onSelect,
  searchQuery,
  activeFilters,
  contextMin,
}: {
  provider: ProviderGroup;
  selectedModelId: string | null;
  onSelect: (model: ProviderModel) => void;
  searchQuery: string;
  activeFilters: string[];
  contextMin: number;
}) {
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

  if (matched.length === 0 && dimmed.length === 0) return null;

  return (
    <div
      className="flex flex-col rounded-lg border overflow-hidden"
      style={{ borderColor: `${provider.color}30`, height: 300 }}
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

      <div className="overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-col gap-px p-1">
          {matched.map((m) => (
            <ModelRow key={m.id} model={m} isSelected={selectedModelId === m.id} onSelect={() => onSelect(m)} />
          ))}
          {dimmed.length > 0 && (
            <>
              <div className="flex items-center gap-2 px-1.5 py-1 mt-0.5 border-t border-border/30">
                <span className="text-[9px] text-muted-foreground/50 leading-none">{dimmed.length} dimmed</span>
              </div>
              {dimmed.map((m) => (
                <ModelRow key={m.id} model={m} isSelected={selectedModelId === m.id} onSelect={() => onSelect(m)} dimmed />
              ))}
            </>
          )}
        </div>
      </div>
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
    overlayOpen,
    searchQuery,
    activeFilters,
    contextMin,
    fetchProviders,
    refreshProviders,
    selectModel,
    closeOverlay,
    setSearchQuery,
    toggleFilter,
    setContextMin,
    openProvidersDialog,
  } = useModelStore();

  const liveCount = syncStatus.filter((s) => s.live).length;
  const totalCount = syncStatus.length;
  const syncedLabel = syncedAgoLabel(syncedAt);

  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (providers.length === 0 && !loading) fetchProviders();
  }, [providers.length, loading, fetchProviders]);

  useEffect(() => {
    if (overlayOpen) {
      const t = setTimeout(() => inputRef.current?.focus(), 200);
      return () => clearTimeout(t);
    }
  }, [overlayOpen]);

  useEffect(() => {
    if (!overlayOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeOverlay();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [overlayOpen, closeOverlay]);

  const handleSelect = useCallback(
    (model: ProviderModel) => {
      const provider = providers.find((p) => p.models.some((m) => m.id === model.id));
      selectModel(model.id, provider?.name ?? "");
    },
    [providers, selectModel]
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
    return null;
  }, [selectedModelId, providers]);

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
            onClick={closeOverlay}
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
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[12px] font-semibold text-foreground">Select Model</span>
                  <span className="text-[10px] text-muted-foreground">
                    {loading ? "loading\u2026" : `${filteredCount} models \u00b7 ${providers.length} providers`}
                  </span>
                  {!loading && providers.length > 0 && (
                    <button
                      onClick={() => openProvidersDialog()}
                      className="inline-flex items-center gap-1 ml-1 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                      title="Providers & privacy settings"
                    >
                      <IcoSettings />
                      <span className="hidden sm:inline">Providers</span>
                    </button>
                  )}
                  {!loading && providers.length > 0 && (
                    <button
                      onClick={() => refreshProviders()}
                      disabled={refreshing}
                      className="inline-flex items-center gap-1 ml-0.5 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-60"
                      title={refreshing ? "syncing\u2026" : `${syncedLabel} \u00b7 ${liveCount}/${totalCount} providers live \u00b7 click to re-sync`}
                    >
                      <IcoRefresh spinning={refreshing} />
                      <span className="hidden md:inline">{refreshing ? "syncing\u2026" : syncedLabel || "sync"}</span>
                    </button>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <div className="relative">
                    <span className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground">
                      <IcoSearch />
                    </span>
                    <input
                      ref={inputRef}
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      placeholder="search\u2026"
                      className="pl-7 pr-2 h-6 w-36 sm:w-48 text-[11px] bg-muted/40 border border-border/60 rounded-md outline-none focus:border-ring/50 placeholder:text-muted-foreground/60"
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
                        borderColor: contextMin === c.value ? "var(--primary)" : "var(--border)",
                        backgroundColor: contextMin === c.value ? "color-mix(in srgb, var(--primary) 10%, transparent)" : "transparent",
                        color: contextMin === c.value ? "var(--primary)" : "var(--muted-foreground)",
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
                </div>
              )}

              <div className="flex-1 min-h-0 overflow-y-auto">
                {loading && (
                  <div className="flex items-center justify-center h-full">
                    <span className="inline-block size-4 border-2 border-muted-foreground/30 border-t-muted-foreground rounded-full animate-spin" />
                    <span className="ml-2 text-[11px] text-muted-foreground">fetching\u2026</span>
                  </div>
                )}

                {error && (
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

                {!loading && !error && (
                  <div className="p-3">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {providers.map((p) => (
                        <ProviderBox
                          key={p.name}
                          provider={p}
                          selectedModelId={selectedModelId}
                          onSelect={handleSelect}
                          searchQuery={searchQuery}
                          activeFilters={activeFilters}
                          contextMin={contextMin}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between px-3.5 shrink-0" style={{ height: 30, borderTop: "1px solid var(--border)" }}>
                <span className="text-[10px] text-muted-foreground truncate">
                  click to select \u00b7 <kbd className="px-1 py-px rounded bg-muted border border-border text-[9px] font-mono">esc</kbd> to close \u00b7 <span className="text-green-600">\u25cf</span> live / <span className="text-amber-600">\u25cf</span> config \u00b7 {liveCount}/{totalCount} synced
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
