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

function attributeLine(a: ModelAttributes | undefined): { text: string; warn?: boolean } | null {
  if (!a) return null;
  const parts: string[] = [];
  const b = a.benchmarks;
  if (b) {
    if (typeof b.intelligence === "number") parts.push(`AA ${b.intelligence}`);
    if (typeof b.coding === "number") parts.push(`code ${b.coding}`);
    if (typeof b.agentic === "number") parts.push(`agent ${b.agentic}`);
    if (typeof b.sweBench === "number") parts.push(`SWE ${b.sweBench}`);
    if (typeof b.aaCoding === "number" && typeof b.sweBench === "undefined") parts.push(`AA-coding ${b.aaCoding}`);
  }
  if (a.ranks && a.ranks.length > 0) {
    const top = a.ranks.slice(0, 3).map((r) => `${r.label} #${r.rank}`).join(" · ");
    parts.push(top);
  }
  if (a.capabilities && a.capabilities.length > 0) {
    parts.push(a.capabilities.join(" · "));
  }
  if (a.pricing) parts.push(a.pricing);

  const text = parts.join(" · ");
  if (!text && !a.note) return null;
  return { text, warn: a.note ? true : false };
}

function ModelRow({
  model,
  isSelected,
  onSelect,
}: {
  model: ProviderModel;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const attr = model.attributes ? attributeLine(model.attributes) : null;
  const note = model.attributes?.note;
  const hasSub = !!(attr && (attr.text || note));

  return (
    <button
      onClick={onSelect}
      className={`
        flex flex-col w-full text-left rounded px-1.5 transition-colors duration-100 cursor-pointer
        ${isSelected ? "bg-primary/10" : "hover:bg-muted/60"}
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
        <span className="flex items-center w-full pl-6 pr-1 mt-0.5 min-h-[14px]">
          {attr?.text && (
            <span className="text-[9px] leading-none text-muted-foreground/80 truncate" title={attr.text}>
              {attr.text}
            </span>
          )}
          {note && (
            <span
              className={`text-[9px] leading-none truncate ${attr?.text ? "ml-1.5" : ""} ${note.startsWith("⚠") ? "text-amber-600 dark:text-amber-500" : "text-muted-foreground/70"}`}
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
}: {
  provider: ProviderGroup;
  selectedModelId: string | null;
  onSelect: (model: ProviderModel) => void;
  searchQuery: string;
}) {
  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return provider.models;
    const q = searchQuery.toLowerCase();
    return provider.models.filter(
      (m) =>
        m.displayName.toLowerCase().includes(q) ||
        m.id.toLowerCase().includes(q) ||
        (m.family?.toLowerCase().includes(q) ?? false)
    );
  }, [provider.models, searchQuery]);

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
    fetchProviders,
    refreshProviders,
    selectModel,
    closeOverlay,
    setSearchQuery,
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

  const topRow = providers.slice(0, 2);
  const bottomRow = providers.slice(2, 4);

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

                <div className="flex items-center gap-2">
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
                    onClick={closeOverlay}
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

                {!loading && !error && (
                  <div className="flex flex-col gap-3 p-3">
                    {topRow.length > 0 && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {topRow.map((p) => (
                          <ProviderBox
                            key={p.name}
                            provider={p}
                            selectedModelId={selectedModelId}
                            onSelect={handleSelect}
                            searchQuery={searchQuery}
                          />
                        ))}
                      </div>
                    )}
                    {bottomRow.length > 0 && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {bottomRow.map((p) => (
                          <ProviderBox
                            key={p.name}
                            provider={p}
                            selectedModelId={selectedModelId}
                            onSelect={handleSelect}
                            searchQuery={searchQuery}
                          />
                        ))}
                      </div>
                    )}
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
