export interface ModelAttributes {
  capabilities?: string[];
  pricing?: string;
  benchmarks?: {
    intelligence?: number;
    coding?: number;
    agentic?: number;
    sweBench?: number;
    aaCoding?: number;
  };
  ranks?: { label: string; rank: number }[];
  note?: string;
}

export interface ProviderModel {
  id: string;
  displayName: string;
  contextLength: number;
  family?: string;
  attributes?: ModelAttributes;
}

export interface ProviderPrivacy {
  notice: string;
  confidence: "high" | "medium" | "low";
  retention?: string;
  training?: string;
  sources: string[];
}

export interface ProviderGroup {
  name: string;
  displayName: string;
  pool: string;
  region: string;
  icon: string;
  color: string;
  models: ProviderModel[];
  privacy: ProviderPrivacy;
  usageLimits?: string;
  settingsUrl: string;
  manageLabel: string;
  syncedLive?: boolean;
}

export interface SyncStatusEntry {
  provider: string;
  live: boolean;
  error?: string;
  modelCount: number;
}

export interface SyncResult {
  providers: ProviderGroup[];
  totalModels: number;
  syncedAt: string;
  syncStatus: SyncStatusEntry[];
}
