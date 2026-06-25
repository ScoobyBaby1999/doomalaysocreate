import { create } from "zustand";
import type { ProviderGroup, ProviderModel, SyncStatusEntry } from "./providers/types";

export type { ProviderGroup, ProviderModel, ModelAttributes, ProviderPrivacy, SyncResult, SyncStatusEntry } from "./providers/types";

interface ModelSelectionState {
  providers: ProviderGroup[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  totalModels: number;
  syncedAt: string | null;
  syncStatus: SyncStatusEntry[];

  baseUrl: string;

  selectedModelId: string | null;
  selectedProviderName: string | null;

  focusedMode: boolean;

  overlayOpen: boolean;
  providersDialogOpen: boolean;
  providersDialogProvider: string | null;

  searchQuery: string;

  setBaseUrl: (url: string) => void;
  fetchProviders: () => Promise<void>;
  refreshProviders: () => Promise<void>;
  selectModel: (modelId: string, providerName: string) => void;
  clearSelection: () => void;
  toggleFocusedMode: () => void;
  openOverlay: () => void;
  closeOverlay: () => void;
  toggleOverlay: () => void;
  setSearchQuery: (q: string) => void;
  openProvidersDialog: (providerName?: string) => void;
  closeProvidersDialog: () => void;
  getSelectedModel: () => ProviderModel | null;
  getSelectedProvider: () => ProviderGroup | null;
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

  selectedModelId: loadPersisted<string | null>(`${PREFIX}.selectedModelId`, null),
  selectedProviderName: loadPersisted<string | null>(`${PREFIX}.selectedProviderName`, null),
  focusedMode: loadPersisted<boolean>(`${PREFIX}.focusedMode`, false),

  overlayOpen: false,
  providersDialogOpen: false,
  providersDialogProvider: null,
  searchQuery: "",

  setBaseUrl: (url: string) => set({ baseUrl: url }),

  fetchProviders: async () => {
    const baseUrl = get().baseUrl;
    if (!baseUrl) { set({ loading: false }); return; }
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
    if (!baseUrl) { set({ refreshing: false }); return; }
    set({ refreshing: true });
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

  selectModel: (modelId: string, providerName: string) => {
    set({ selectedModelId: modelId, selectedProviderName: providerName, overlayOpen: false, focusedMode: true });
    persist(`${PREFIX}.selectedModelId`, modelId);
    persist(`${PREFIX}.selectedProviderName`, providerName);
    persist(`${PREFIX}.focusedMode`, true);
  },

  clearSelection: () => {
    set({ selectedModelId: null, selectedProviderName: null, focusedMode: false });
    persist(`${PREFIX}.selectedModelId`, null);
    persist(`${PREFIX}.selectedProviderName`, null);
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

  openProvidersDialog: (providerName?: string) =>
    set({ providersDialogOpen: true, providersDialogProvider: providerName ?? null }),

  closeProvidersDialog: () =>
    set({ providersDialogOpen: false, providersDialogProvider: null }),

  getSelectedModel: () => {
    const { selectedModelId, providers } = get();
    if (!selectedModelId) return null;
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
