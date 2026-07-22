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
  // BATCH-3 Task 5 — per-model effort levels returned by the roster
  // endpoint. Each model can have a different set of effort variants
  // (e.g. ["low","medium","high"] for some models, ["low","mid","ultra",
  // "max"] for others). When empty/missing, the model doesn't support
  // effort and the effort button is hidden.
  // Both camelCase + snake_case are accepted because the backend may return
  // either form (JSON APIs typically use snake_case).
  effortLevels?: string[];
  effort_levels?: string[];
}

export interface ProviderModel {
  id: string;
  displayName: string;
  contextLength: number;
  family?: string;
  attributes?: ModelAttributes;
  /** Full slot id "provider/model" for pinned routing (e.g. "nvidia/z-ai/glm-5.1") */
  slotId?: string;
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

export interface CondensedHost {
  provider: string;
  providerDisplayName: string;
  icon: string;
  color: string;
  modelId: string;
  contextLength: number;
  hasApiKey: boolean;
  syncedLive: boolean;
  defaultPriority: number;
}

export interface CondensedModel {
  logical: string;
  displayName: string;
  family: string;
  contextLength: number;
  attributes?: ModelAttributes;
  hosts: CondensedHost[];
}

export interface SyncResult {
  providers: ProviderGroup[];
  totalModels: number;
  syncedAt: string;
  syncStatus: SyncStatusEntry[];
  logical: CondensedModel[];
}
