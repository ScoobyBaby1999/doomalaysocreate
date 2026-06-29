import { create } from "zustand";
import type { ProviderGroup, ProviderModel, SyncStatusEntry, CondensedModel } from "./providers/types";

export type { ProviderGroup, ProviderModel, ModelAttributes, ProviderPrivacy, SyncResult, SyncStatusEntry, CondensedModel, CondensedHost } from "./providers/types";

interface ModelSelectionState {
  providers: ProviderGroup[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  totalModels: number;
  syncedAt: string | null;
  syncStatus: SyncStatusEntry[];

  baseUrl: string;
  lastFetchedAt: number | null;
  cacheBaseUrl: string | null;

  selectedModelId: string | null;
  selectedProviderName: string | null;
  /** Full slot id "provider/model" for pinned routing (e.g. "nvidia/z-ai/glm-5.1") */
  selectedSlotId: string | null;

  focusedMode: boolean;

  overlayOpen: boolean;
  providersDialogOpen: boolean;
  providersDialogProvider: string | null;

  searchQuery: string;
  activeFilters: string[];
  contextMin: number;

  condensedModels: CondensedModel[];
  condensedLoading: boolean;
  condensedFetched: boolean;
  condensedFetchedAt: number;
  condensedView: boolean;
  providerPriorityOverrides: Record<string, string[]>;
  globalProviderPriority: string[] | null;

  setBaseUrl: (url: string) => void;
  fetchProviders: () => Promise<void>;
  refreshProviders: () => Promise<void>;
  selectModel: (modelId: string, providerName: string, slotId?: string) => void;
  clearSelection: () => void;
  toggleFocusedMode: () => void;
  openOverlay: () => void;
  closeOverlay: () => void;
  toggleOverlay: () => void;
  setSearchQuery: (q: string) => void;
  toggleFilter: (filter: string) => void;
  setContextMin: (min: number) => void;
  openProvidersDialog: (providerName?: string) => void;
  closeProvidersDialog: () => void;
  getSelectedModel: () => ProviderModel | null;
  getSelectedProvider: () => ProviderGroup | null;

  fetchCondensedModels: (force?: boolean) => Promise<void>;
  setCondensedView: (v: boolean) => void;
  setProviderPriority: (logical: string, orderedProviders: string[]) => void;
  setGlobalProviderPriority: (orderedProviders: string[] | null) => void;
  resetPriorityDefaults: () => void;
  condensedSelect: (logical: string) => void;
  getOrderedHosts: (logical: string, hosts: CondensedModel["hosts"]) => CondensedModel["hosts"];
}

function loadPersisted<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function persist(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

const PREFIX = "doomalaysocreate.model-store";

export const useModelStore = create<ModelSelectionState>((set, get) => ({
  providers: [],
  loading: false,
  refreshing: false,
  error: null,
  totalModels: 0,
  syncedAt: null,
  syncStatus: [],

  baseUrl: "",
  lastFetchedAt: null,
  cacheBaseUrl: null,

  selectedModelId: loadPersisted<string | null>(`${PREFIX}.selectedModelId`, null),
  selectedProviderName: loadPersisted<string | null>(`${PREFIX}.selectedProviderName`, null),
  selectedSlotId: loadPersisted<string | null>(`${PREFIX}.selectedSlotId`, null),
  focusedMode: loadPersisted<boolean>(`${PREFIX}.focusedMode`, false),

  overlayOpen: false,
  providersDialogOpen: false,
  providersDialogProvider: null,
  searchQuery: "",
  activeFilters: loadPersisted<string[]>(`${PREFIX}.activeFilters`, []),
  contextMin: loadPersisted<number>(`${PREFIX}.contextMin`, 0),

  condensedModels: [],
  condensedLoading: false,
  condensedFetched: false,
  condensedFetchedAt: 0,
  condensedView: loadPersisted<boolean>(`${PREFIX}.condensedView`, false),
  providerPriorityOverrides: loadPersisted<Record<string, string[]>>(`${PREFIX}.providerPriorityOverrides`, {}),
  globalProviderPriority: loadPersisted<string[] | null>(`${PREFIX}.globalProviderPriority`, null),

  setBaseUrl: (url: string) => set({ baseUrl: url }),

  fetchProviders: async () => {
    const { baseUrl, lastFetchedAt, cacheBaseUrl } = get();
    if (baseUrl == null) { set({ loading: false }); return; }
    const CACHE_TTL = 5 * 60 * 1000; // 5 min
    if (cacheBaseUrl === baseUrl && lastFetchedAt && Date.now() - lastFetchedAt < CACHE_TTL) {
      return; // cache fresh
    }
    set({ loading: true, error: null });
    try {
      const res = await fetch(`${baseUrl}/api/models`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      set({
        providers: data.providers,
        totalModels: data.totalModels,
        syncedAt: data.syncedAt ?? null,
        syncStatus: data.syncStatus ?? [],
        loading: false,
        lastFetchedAt: Date.now(),
        cacheBaseUrl: baseUrl,
      });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to fetch models",
        loading: false,
      });
    }
  },

  refreshProviders: async () => {
    const baseUrl = get().baseUrl;
    if (baseUrl == null) { set({ refreshing: false }); return; }
    set({ refreshing: true, lastFetchedAt: null }); // bypass cache
    try {
      const res = await fetch(`${baseUrl}/api/models?refresh=1`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      set({
        providers: data.providers,
        totalModels: data.totalModels,
        syncedAt: data.syncedAt ?? null,
        syncStatus: data.syncStatus ?? [],
        refreshing: false,
        error: null,
      });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to refresh models",
        refreshing: false,
      });
    }
  },

  selectModel: (modelId: string, providerName: string, slotId?: string) => {
    set({ selectedModelId: modelId, selectedProviderName: providerName, selectedSlotId: slotId ?? modelId, overlayOpen: false, focusedMode: true });
    persist(`${PREFIX}.selectedModelId`, modelId);
    persist(`${PREFIX}.selectedProviderName`, providerName);
    persist(`${PREFIX}.selectedSlotId`, slotId ?? modelId);
    persist(`${PREFIX}.focusedMode`, true);
  },

  clearSelection: () => {
    set({ selectedModelId: null, selectedProviderName: null, selectedSlotId: null, focusedMode: false });
    persist(`${PREFIX}.selectedModelId`, null);
    persist(`${PREFIX}.selectedProviderName`, null);
    persist(`${PREFIX}.selectedSlotId`, null);
    persist(`${PREFIX}.focusedMode`, false);
  },

  toggleFocusedMode: () => {
    const { focusedMode, selectedModelId } = get();
    if (!focusedMode && selectedModelId) {
      set({ focusedMode: true });
      persist(`${PREFIX}.focusedMode`, true);
    } else {
      set({ focusedMode: false });
      persist(`${PREFIX}.focusedMode`, false);
    }
  },

  openOverlay: () => set({ overlayOpen: true }),
  closeOverlay: () => set({ overlayOpen: false, searchQuery: "" }),
  toggleOverlay: () => {
    const { overlayOpen } = get();
    set({ overlayOpen: !overlayOpen, searchQuery: "" });
  },

  setSearchQuery: (q: string) => set({ searchQuery: q }),

  toggleFilter: (filter: string) => {
    const { activeFilters } = get();
    const next = activeFilters.includes(filter)
      ? activeFilters.filter((f) => f !== filter)
      : [...activeFilters, filter];
    set({ activeFilters: next });
    persist(`${PREFIX}.activeFilters`, next);
  },

  setContextMin: (min: number) => {
    set({ contextMin: min });
    persist(`${PREFIX}.contextMin`, min);
  },

  openProvidersDialog: (providerName?: string) =>
    set({ providersDialogOpen: true, providersDialogProvider: providerName ?? null }),

  closeProvidersDialog: () =>
    set({ providersDialogOpen: false, providersDialogProvider: null }),

  fetchCondensedModels: async (force?: boolean) => {
    const { baseUrl, condensedFetchedAt } = get();
    if (baseUrl == null) { set({ condensedLoading: false }); return; }
    const CACHE_TTL = 10 * 60 * 1000;
    if (!force && condensedFetchedAt && Date.now() - condensedFetchedAt < CACHE_TTL) return;
    set({ condensedLoading: true });
    try {
      const res = await fetch(`${baseUrl}/api/models/condensed`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      set({ condensedModels: data.models ?? [], condensedLoading: false, condensedFetched: true, condensedFetchedAt: Date.now() });
    } catch {
      set({ condensedLoading: false });
    }
  },

  setCondensedView: (v: boolean) => {
    set({ condensedView: v });
    persist(`${PREFIX}.condensedView`, v);
  },

  setProviderPriority: (logical: string, orderedProviders: string[]) => {
    const overrides = { ...get().providerPriorityOverrides, [logical]: orderedProviders };
    set({ providerPriorityOverrides: overrides });
    persist(`${PREFIX}.providerPriorityOverrides`, overrides);
  },

  setGlobalProviderPriority: (orderedProviders: string[] | null) => {
    set({ globalProviderPriority: orderedProviders });
    persist(`${PREFIX}.globalProviderPriority`, orderedProviders);
  },

  resetPriorityDefaults: () => {
    set({ providerPriorityOverrides: {}, globalProviderPriority: null });
    persist(`${PREFIX}.providerPriorityOverrides`, {});
    persist(`${PREFIX}.globalProviderPriority`, null);
  },

  condensedSelect: (logical: string) => {
    const { condensedModels } = get();
    const model = condensedModels.find((m) => m.logical === logical);
    if (!model) return;
    const ordered = get().getOrderedHosts(logical, model.hosts);
    const best =
      ordered.find((h) => h.hasApiKey && h.syncedLive) ??
      ordered.find((h) => h.hasApiKey) ??
      ordered.find((h) => h.syncedLive) ??
      ordered[0];
    if (best) {
      // store LOGICAL model ID (e.g. "glm-5.1") so it matches provider model
      // ids in the Providers view AND condensed model logical keys
      const slotId = `${best.provider}/${best.modelId}`;
      set({ selectedModelId: model.logical, selectedProviderName: best.provider, selectedSlotId: slotId, overlayOpen: false, focusedMode: false });
      persist(`${PREFIX}.selectedModelId`, model.logical);
      persist(`${PREFIX}.selectedProviderName`, best.provider);
      persist(`${PREFIX}.selectedSlotId`, slotId);
      persist(`${PREFIX}.focusedMode`, false);
    }
  },

  getOrderedHosts: (logical: string, hosts) => {
    const { providerPriorityOverrides, globalProviderPriority } = get();
    if (hosts.length <= 1) return [...hosts];
    let order: string[] | undefined;
    if (providerPriorityOverrides[logical]) {
      order = providerPriorityOverrides[logical];
    } else if (globalProviderPriority) {
      order = globalProviderPriority;
    }
    if (!order) return [...hosts].sort((a, b) => a.defaultPriority - b.defaultPriority);
    const ordered = order.map((name) => hosts.find((h) => h.provider === name)).filter(Boolean) as CondensedModel["hosts"];
    const remaining = hosts.filter((h) => !order.includes(h.provider));
    remaining.sort((a, b) => a.defaultPriority - b.defaultPriority);
    return [...ordered, ...remaining];
  },

  getSelectedModel: () => {
    const { selectedModelId, selectedSlotId, providers } = get();
    if (!selectedModelId) return null;
    // try slotId match first (pinned provider selection)
    if (selectedSlotId) {
      for (const p of providers) {
        const m = p.models.find((m) => m.slotId === selectedSlotId);
        if (m) return m;
      }
    }
    // fall back to logical model ID match
    for (const p of providers) {
      const m = p.models.find((m) => m.id === selectedModelId);
      if (m) return m;
    }
    return null;
  },

  getSelectedProvider: () => {
    const { selectedProviderName, providers } = get();
    if (!selectedProviderName) return null;
    return providers.find((p) => p.name === selectedProviderName) ?? null;
  },
}));
