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
function IcoSort() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="8" y1="12" x2="20" y2="12"/><line x1="12" y1="18" x2="20" y2="18"/></svg>
  );
}
function IcoArrowUp() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="18 15 12 9 6 15"/></svg>
  );
}

function fmtCtx(k: number): string {
  if (!k) return "—";
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

function scoreColor(v: number): string {
  if (v >= 70) return "#22c55e";
  if (v >= 40) return "#f59e0b";
  return "#ef4444";
}

function scoreBg(v: number): string {
  if (v >= 70) return "#22c55e18";
  if (v >= 40) return "#f59e0b18";
  return "#ef444418";
}

const CAP_COLORS: Record<string, string> = {
  vision: "#a855f7",
  tools: "#3b82f6",
  reasoning: "#f97316",
  audio: "#06b6d4",
  video: "#ec4899",
};

function capColor(c: string): string {
  return CAP_COLORS[c] || "#8b95a3";
}

interface Badge {
  text: string;
  color: string;
  bg: string;
  title?: string;
}

function attributeBadges(a: ModelAttributes | undefined): Badge[] {
  if (!a) return [];
  const badges: Badge[] = [];
  const b = a.benchmarks;
  if (b) {
    if (typeof b.intelligence === "number") {
      badges.push({ text: `AA ${b.intelligence}`, color: scoreColor(b.intelligence), bg: scoreBg(b.intelligence), title: "Artificial Analysis intelligence index" });
    }
    if (typeof b.coding === "number") {
      badges.push({ text: `code ${b.coding}`, color: scoreColor(b.coding), bg: scoreBg(b.coding), title: "Artificial Analysis coding index" });
    }
    if (typeof b.agentic === "number") {
      badges.push({ text: `agent ${b.agentic}`, color: scoreColor(b.agentic), bg: scoreBg(b.agentic), title: "Artificial Analysis agentic index" });
    }
    if (typeof b.sweBench === "number") {
      badges.push({ text: `SWE ${b.sweBench}`, color: scoreColor(b.sweBench), bg: scoreBg(b.sweBench), title: "SWE-bench" });
    }
    if (typeof b.aaCoding === "number" && typeof b.sweBench === "undefined") {
      badges.push({ text: `AA-code ${b.aaCoding}`, color: scoreColor(b.aaCoding), bg: scoreBg(b.aaCoding), title: "Artificial Analysis coding" });
    }
  }
  if (a.ranks && a.ranks.length > 0) {
    const top = a.ranks[0];
    badges.push({ text: `${top.label} #${top.rank}`, color: "#8b95a3", bg: "#8b95a318", title: "Usage/spend popularity rank" });
  }
  if (a.capabilities && a.capabilities.length > 0) {
    for (const c of a.capabilities) {
      badges.push({ text: c, color: capColor(c), bg: `${capColor(c)}18`, title: `Capability: ${c}` });
    }
  }
  if (a.pricing) {
    const isFree = a.pricing.toLowerCase().includes("free");
    badges.push({ text: a.pricing, color: isFree ? "#22c55e" : "#8b95a3", bg: isFree ? "#22c55e18" : "#8b95a318", title: "Pricing per million tokens" });
  }
  return badges;
}

function attachSortKey(model: ProviderModel, sortBy: string): number {
  if (sortBy === "intelligence") return model.attributes?.benchmarks?.intelligence ?? -1;
  if (sortBy === "coding") return model.attributes?.benchmarks?.coding ?? -1;
  if (sortBy === "agentic") return model.attributes?.benchmarks?.agentic ?? -1;
  if (sortBy === "context") return model.contextLength ?? 0;
  return 0;
}

function sortModels(models: ProviderModel[], sortBy: string): ProviderModel[] {
  if (sortBy === "default" || sortBy === "name") {
    const s = [...models];
    if (sortBy === "name") s.sort((a, b) => a.displayName.localeCompare(b.displayName));
    return s;
  }
  return [...models].sort((a, b) => attachSortKey(b, sortBy) - attachSortKey(a, sortBy));
}

const SORT_OPTIONS = [
  { key: "default", label: "Default" },
  { key: "intelligence", label: "AA Intel" },
  { key: "coding", label: "Code" },
  { key: "agentic", label: "Agent" },
  { key: "context", label: "Context" },
  { key: "name", label: "Name" },
];

function ModelRow({
  model,
  isSelected,
  onSelect,
}: {
  model: ProviderModel;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const badges = attributeBadges(model.attributes);
  const note = model.attributes?.note;

  return (
    <button
      onClick={onSelect}
      className={`
        flex flex-col w-full text-left rounded px-1.5 transition-colors duration-100 cursor-pointer
        ${isSelected ? "bg-primary/10" : "hover:bg-muted/60"}
      `}
      style={{ paddingTop: 4, paddingBottom: 4 }}
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

      {(badges.length > 0 || note) && (
        <span className="flex flex-wrap items-center gap-1 pl-6 pr-1 mt-1">
          {badges.map((b, i) => (
            <span
              key={i}
              className="inline-flex items-center px-1 py-px rounded text-[9px] font-medium leading-none"
              style={{ color: b.color, backgroundColor: b.bg }}
              title={b.title || b.text}
            >
              {b.text}
            </span>
          ))}
          {note && (
            <span
              className={`inline-flex items-center px-1 py-px rounded text-[9px] leading-none ${note.startsWith("⚠") ? "text-amber-600 dark:text-amber-500 bg-amber-500/10" : "text-muted-foreground/70 bg-muted/30"}`}
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
  sortBy,
}: {
  provider: ProviderGroup;
  selectedModelId: string | null;
  onSelect: (model: ProviderModel) => void;
  searchQuery: string;
  sortBy: string;
}) {
  const filtered = useMemo(() => {
    let ms = provider.models;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      ms = ms.filter(
        (m) =>
          m.displayName.toLowerCase().includes(q) ||
          m.id.toLowerCase().includes(q) ||
          (m.family?.toLowerCase().includes(q) ?? false)
      );
    }
    return sortModels(ms, sortBy);
  }, [provider.models, searchQuery, sortBy]);

  const openSettings = useCallback(() => {
    useModelStore.getState().openProvidersDialog(provider.name);
  }, [provider.name]);

  if (filtered.length === 0) return null;

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
          title={`${provider.manageLabel} ↗`}
        >
          <IcoSettings />
        </button>
      </div>

      <button
        onClick={openSettings}
        className="flex items-center gap-1.5 px-2.5 shrink-0 text-left hover:bg-muted/40 transition-colors"
        style={{ height: 24, borderBottom: `1px solid ${provider.color}12`, backgroundColor: `${provider.color}06` }}
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
          {filtered.map((m) => (
            <ModelRow key={m.id} model={m} isSelected={selectedModelId === m.id} onSelect={() => onSelect(m)} />
          ))}
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
    sortBy,
    fetchProviders,
    refreshProviders,
    selectModel,
    closeOverlay,
    setSearchQuery,
    setSortBy,
    openProvidersDialog,
  } = useModelStore();

  const liveCount = syncStatus.filter((s) => s.live).length;
  const totalCount = syncStatus.length;
  const syncedLabel = syncedAgoLabel(syncedAt);

  const inputRef = useRef<HTMLInputElement>(null);
  const [showSort, setShowSort] = useState(false);

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
      if (e.key === "Escape") { closeOverlay(); setShowSort(false); }
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
    if (!searchQuery.trim()) return totalModels;
    const q = searchQuery.toLowerCase();
    return providers.reduce(
      (s, p) =>
        s +
        p.models.filter(
          (m) =>
            m.displayName.toLowerCase().includes(q) ||
            m.id.toLowerCase().includes(q) ||
            (m.family?.toLowerCase().includes(q) ?? false)
        ).length,
      0
    );
  }, [providers, searchQuery, totalModels]);

  const selectedDisplayName = useMemo(() => {
    if (!selectedModelId) return null;
    for (const p of providers) {
      const m = p.models.find((m) => m.id === selectedModelId);
      if (m) return m.displayName;
    }
    return null;
  }, [selectedModelId, providers]);

  const sortLabel = SORT_OPTIONS.find((o) => o.key === sortBy)?.label || "Default";

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
            onClick={() => { closeOverlay(); setShowSort(false); }}
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
                    {loading ? "loading…" : error ? `failed: ${error}` : providers.length === 0 ? "no provider data" : `${filteredCount} models · ${providers.length} providers`}
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
                      title={refreshing ? "syncing…" : `${syncedLabel} · ${liveCount}/${totalCount} providers live · click to re-sync`}
                    >
                      <IcoRefresh spinning={refreshing} />
                      <span className="hidden md:inline">{refreshing ? "syncing…" : syncedLabel || "sync"}</span>
                    </button>
                  )}
                </div>

                <div className="flex items-center gap-2 relative">
                  <button
                    onClick={() => setShowSort(!showSort)}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                    title={`Sort by: ${sortLabel}`}
                  >
                    <IcoSort />
                    <span className="hidden sm:inline text-[10px]">{sortLabel}</span>
                    <IcoArrowUp />
                  </button>
                  {showSort && (
                    <>
                      <div className="fixed inset-0 z-30" onClick={() => setShowSort(false)} />
                      <div className="absolute top-full right-12 mt-1 w-36 bg-surface2 border border-border rounded-lg shadow-xl z-40 overflow-hidden">
                        {SORT_OPTIONS.map((o) => (
                          <button
                            key={o.key}
                            onClick={() => { setSortBy(o.key); setShowSort(false); }}
                            className={`w-full text-left px-3 py-2 text-[11px] flex items-center gap-2 transition-colors ${sortBy === o.key ? "text-primary bg-primary/10 font-medium" : "text-foreground hover:bg-muted/40"}`}
                          >
                            {sortBy === o.key && <IcoDot />}
                            <span className={sortBy === o.key ? "" : "ml-4"}>{o.label}</span>
                          </button>
                        ))}
                      </div>
                    </>
                  )}

                  <div className="relative">
                    <span className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground">
                      <IcoSearch />
                    </span>
                    <input
                      ref={inputRef}
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      placeholder="search…"
                      className="pl-7 pr-2 h-6 w-36 sm:w-48 text-[11px] bg-muted/40 border border-border/60 rounded-md outline-none focus:border-ring/50 placeholder:text-muted-foreground/60"
                    />
                  </div>

                  <button
                    onClick={() => { closeOverlay(); setShowSort(false); }}
                    className="flex items-center justify-center size-6 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                    aria-label="Close"
                  >
                    <IcoX />
                  </button>
                </div>
              </div>

              <div className="flex-1 min-h-0 overflow-y-auto">
                {loading && (
                  <div className="flex items-center justify-center h-full">
                    <span className="inline-block size-4 border-2 border-muted-foreground/30 border-t-muted-foreground rounded-full animate-spin" />
                    <span className="ml-2 text-[11px] text-muted-foreground">fetching…</span>
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

                {!loading && !error && providers.length > 0 && (
                  <div className="p-3">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {providers.map((p) => (
                        <ProviderBox
                          key={p.name}
                          provider={p}
                          selectedModelId={selectedModelId}
                          onSelect={handleSelect}
                          searchQuery={searchQuery}
                          sortBy={sortBy}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between px-3.5 shrink-0" style={{ height: 30, borderTop: "1px solid var(--border)" }}>
                <span className="text-[10px] text-muted-foreground truncate">
                  click to select · <kbd className="px-1 py-px rounded bg-muted border border-border text-[9px] font-mono">esc</kbd> to close · <span className="text-green-600">●</span> live / <span className="text-amber-600">●</span> config · {liveCount}/{totalCount} synced
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

import { useState } from "react";
