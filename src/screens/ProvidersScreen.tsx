/**
 * ProvidersScreen — manage provider API keys with Free/Paid split.
 *
 * Redesigned (per PRODUCT-VISION-GAME-DESIGNER):
 *  - Description at the top explaining what providers unlock.
 *  - Two lists: Free (default above) and Paid (below), with a one-tap
 *    FLIP button to swap their vertical order. Flip state persists in
 *    localStorage.
 *  - Per-provider card: name + colored icon, description, gear icon
 *    (opens ProvidersDialog with usage limits + privacy detail), API-key
 *    input + Save, Active / Not configured status badge, external signup
 *    link.
 *  - For premium-eligible providers (cloudflare, openrouter, github-models):
 *    an "Up to Premium" button moves the provider to the paid list AND
 *    swaps the description to a paid motto. Premium state persists in
 *    localStorage.
 *  - Connected-provider detection: a provider is shown "Active" when the
 *    user has a saved local key, OR when `/api/models` reports
 *    `syncStatus[provider].modelCount > 0`, OR when any condensed-model
 *    host for that provider reports `hasApiKey: true`. This catches
 *    secrets set directly via HF Space settings before the screen existed.
 *
 * The screen is embedded in SettingsScreen (as the "Providers" tab). The
 * `embedded` prop omits the outer header since the Settings tab bar
 * already labels the section.
 */

import { useEffect, useMemo, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { Settings } from "../api/panel";
import { deriveToken } from "../api/token";
import {
  useModelStore,
  type ProviderGroup,
  type CondensedModel,
  type SyncStatusEntry,
} from "../lib/model-store";

// ── Per-provider config ──────────────────────────────────────────────────
interface ProviderKeyConfig {
  envKey: string;
  placeholder: string;
  /** Short hint under the input. */
  hint: string;
  /** Free-tier description shown on the card. */
  freeDescription: string;
  /** Paid-tier description (used after "Up to Premium"). */
  paidDescription?: string;
  /** Motto shown when user upgraded to premium. */
  premiumMotto?: string;
  /** Optional second field (e.g. Cloudflare Account ID). */
  extra?: {
    envKey: string;
    label: string;
    placeholder: string;
    hint: string;
  };
}

const PROVIDER_KEY_CONFIG: Record<string, ProviderKeyConfig> = {
  nvidia: {
    envKey: "NVIDIA_API_KEY",
    placeholder: "nvapi-…",
    hint: "Free credits on build.nvidia.com. Powers Llama, GLM, DeepSeek, etc.",
    freeDescription:
      "Free credits on build.nvidia.com — Llama, GLM, DeepSeek and more.",
    paidDescription:
      "Enterprise GPU-backed inference for production workloads (complex — not yet wired).",
  },
  cloudflare: {
    envKey: "CF_API_TOKEN",
    placeholder: "v1.0-…",
    hint: "Workers AI. Requires both an API token AND your Account ID.",
    freeDescription:
      "Workers AI free tier — limited daily requests on popular models.",
    paidDescription:
      "Workers AI paid tier — higher rate limits + neural-network cache.",
    premiumMotto:
      "Up to Premium — high-volume Workers AI inference, no daily caps.",
    extra: {
      envKey: "CF_ACCOUNT_ID",
      label: "Account ID",
      placeholder: "abcd1234…",
      hint: "Found at the top-right of your Cloudflare dashboard.",
    },
  },
  openrouter: {
    envKey: "OPENROUTER_API_KEY",
    placeholder: "sk-or-…",
    hint: "100+ models with free tiers. The default routing fallback.",
    freeDescription:
      "Free tier — ~50 free models; paid models require credits.",
    paidDescription:
      "Paid plan — all ~400 models including Claude, GPT, Gemini.",
    premiumMotto: "Up to Premium — unlock all ~400 models on OpenRouter.",
  },
  "github-models": {
    envKey: "GITHUB_TOKEN",
    placeholder: "ghp_…",
    hint: "GitHub Models — free preview tier with your GitHub PAT.",
    freeDescription:
      "Free preview tier — rate-limited access to GPT, Llama, Mistral.",
    paidDescription:
      "Copilot integration — models surfaced through your Copilot seat.",
    premiumMotto:
      "Up to Premium — Copilot-backed inference, no separate API key needed.",
  },
  privatemodeai: {
    envKey: "PRIVATEMODEAI_API_KEY",
    placeholder: "pmai-…",
    hint: "Privacy-first gateway for Kimi, GLM, and more.",
    freeDescription: "Privacy-first gateway — Kimi, GLM and more, zero retention.",
  },
  "opencode-zen": {
    envKey: "OPENCODE_ZEN_API_KEY",
    placeholder: "oczen-…",
    hint: "OpenCode Zen — curated open-weight models behind a free key.",
    freeDescription: "Free tier — open-weight models, no card required.",
  },
  "opencode-go": {
    envKey: "OPENCODE_GO_API_KEY",
    placeholder: "ocgo-…",
    hint: "OpenCode Go — premium high-throughput gateway.",
    freeDescription: "Paid gateway — high-throughput frontier model access.",
    paidDescription: "Premium gateway — enterprise SLAs and routing.",
  },
  groq: {
    envKey: "GROQ_API_KEY",
    placeholder: "gsk_…",
    hint: "Very fast inference, generous free tier.",
    freeDescription: "Very fast inference, generous free tier.",
  },
  google: {
    envKey: "GOOGLE_API_KEY",
    placeholder: "AIza…",
    hint: "Gemini Flash & Pro — free tier.",
    freeDescription: "Gemini Flash & Pro — free tier.",
  },
  anthropic: {
    envKey: "ANTHROPIC_API_KEY",
    placeholder: "sk-ant-…",
    hint: "Claude models (paid). Optional — unlocks the Claude tier.",
    freeDescription:
      "Claude models — paid (no free tier beyond trial credit).",
  },
  deepseek: {
    envKey: "DEEPSEEK_API_KEY",
    placeholder: "sk-…",
    hint: "DeepSeek V3 / R1 — very cheap, very strong on math.",
    freeDescription: "DeepSeek V3 / R1 — very cheap, very strong on math.",
  },
  mistral: {
    envKey: "MISTRAL_API_KEY",
    placeholder: "…",
    hint: "Mistral Large / Codestral — European hosting.",
    freeDescription: "Mistral Large / Codestral — European hosting.",
  },
};

function deriveKeyConfig(providerName: string): ProviderKeyConfig {
  const known = PROVIDER_KEY_CONFIG[providerName];
  if (known) return known;
  const env = `${providerName.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_API_KEY`;
  return {
    envKey: env,
    placeholder: "paste key…",
    hint: "Add a PROVIDER_KEY_CONFIG entry in ProvidersScreen.tsx for nicer UX.",
    freeDescription: "Add a PROVIDER_KEY_CONFIG entry for nicer UX.",
  };
}

// ── Free / Paid defaults + premium eligibility ──────────────────────────
const FREE_DEFAULT_ORDER = [
  "opencode-zen",
  "privatemodeai",
  "nvidia",
  "openrouter",
  "cloudflare",
  "github-models",
];
const PAID_DEFAULT_ORDER = ["opencode-go"];
const PREMIUM_ELIGIBLE = new Set(["cloudflare", "openrouter", "github-models"]);

// ── Local-storage helpers ────────────────────────────────────────────────
const LOCAL_KEY = "doomalaysocreate.providerKeys.v1";
const FLIP_KEY = "doomalaysocreate.providers.flipPaidFirst";
const PREMIUM_KEY = "doomalaysocreate.providers.premium";

type LocalKeyMap = Record<string, { key: string; extra?: string }>;

function loadLocalKeys(): LocalKeyMap {
  try {
    const raw = localStorage.getItem(LOCAL_KEY);
    return raw ? (JSON.parse(raw) as LocalKeyMap) : {};
  } catch {
    return {};
  }
}

function saveLocalKeys(map: LocalKeyMap) {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

function loadFlip(): boolean {
  try {
    return localStorage.getItem(FLIP_KEY) === "1";
  } catch {
    return false;
  }
}

function saveFlip(v: boolean) {
  try {
    localStorage.setItem(FLIP_KEY, v ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function loadPremium(): Record<string, true> {
  try {
    const raw = localStorage.getItem(PREMIUM_KEY);
    return raw ? (JSON.parse(raw) as Record<string, true>) : {};
  } catch {
    return {};
  }
}

function savePremium(map: Record<string, true>) {
  try {
    localStorage.setItem(PREMIUM_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

// ── Backend save attempts (same shape as before) ────────────────────────
async function tryBackendSave(
  settings: Settings,
  envKey: string,
  value: string,
): Promise<{ synced: boolean; message: string }> {
  const bearer = settings.rotationSecret
    ? await deriveToken(settings.rotationSecret)
    : settings.token;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  if (settings.githubSessionId) headers["X-JWT"] = settings.githubSessionId;

  try {
    const r = await fetch(`${settings.baseUrl}/api/keys`, {
      method: "POST",
      headers,
      body: JSON.stringify({ key_name: envKey, key_value: value }),
    });
    if (r.ok) return { synced: true, message: "Saved to backend" };
    if (r.status === 404 || r.status === 405) {
      // fall through
    } else {
      const j = await r.json().catch(() => ({}));
      return {
        synced: false,
        message: (j as { error?: string }).error || `HTTP ${r.status}`,
      };
    }
  } catch {
    /* network error — fall through */
  }
  return { synced: false, message: "Backend sync unavailable — saved locally" };
}

async function tryBackendDelete(
  settings: Settings,
  envKey: string,
): Promise<{ synced: boolean; message: string }> {
  const bearer = settings.rotationSecret
    ? await deriveToken(settings.rotationSecret)
    : settings.token;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  if (settings.githubSessionId) headers["X-JWT"] = settings.githubSessionId;

  try {
    const r = await fetch(
      `${settings.baseUrl}/api/keys/${encodeURIComponent(envKey)}`,
      { method: "DELETE", headers },
    );
    if (r.ok) return { synced: true, message: "Removed from backend" };
    if (r.status === 404 || r.status === 405) {
      return {
        synced: false,
        message: "Removed locally (backend sync unavailable)",
      };
    }
    return { synced: false, message: `HTTP ${r.status}` };
  } catch {
    return { synced: false, message: "Removed locally (network error)" };
  }
}

// ── Toast ────────────────────────────────────────────────────────────────
interface Toast {
  id: number;
  message: string;
  kind: "ok" | "err" | "info";
}

// ── Connected-provider detection ─────────────────────────────────────────
/**
 * A provider is "Active" if ANY of these are true:
 *   1. The user has a saved local key (env-var matches).
 *   2. The roster `/api/models` reports a non-zero modelCount for it, or
 *      marks it live.
 *   3. Any condensed-model host for that provider has `hasApiKey: true`
 *      (the backend has a key — set via HF Space secrets before this
 *      screen existed).
 */
function isProviderActive(
  providerName: string,
  cfg: ProviderKeyConfig,
  savedKeys: LocalKeyMap,
  syncStatus: SyncStatusEntry[],
  condensedModels: CondensedModel[],
): boolean {
  if (savedKeys[cfg.envKey]?.key) return true;
  const ss = syncStatus.find((s) => s.provider === providerName);
  if (ss && (ss.modelCount > 0 || ss.live)) return true;
  for (const m of condensedModels) {
    for (const h of m.hosts) {
      if (h.provider === providerName && h.hasApiKey) return true;
    }
  }
  return false;
}

// ── Component ────────────────────────────────────────────────────────────
export function ProvidersScreen({
  settings,
  embedded = false,
}: {
  settings: Settings;
  embedded?: boolean;
}) {
  const providers = useModelStore((s) => s.providers);
  const syncStatus = useModelStore((s) => s.syncStatus);
  const condensedModels = useModelStore((s) => s.condensedModels);
  const fetchProviders = useModelStore((s) => s.fetchProviders);
  const openProvidersDialog = useModelStore((s) => s.openProvidersDialog);

  const [savedKeys, setSavedKeys] = useState<LocalKeyMap>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Flip: when true, paid list shows ABOVE free list.
  const [flipPaidFirst, setFlipPaidFirst] = useState<boolean>(false);
  // Premium-upgraded providers (keyed by provider name).
  const [premium, setPremium] = useState<Record<string, true>>({});

  const pushToast = useCallback(
    (message: string, kind: Toast["kind"] = "info") => {
      const id = Date.now() + Math.random();
      setToasts((t) => [...t, { id, message, kind }]);
      setTimeout(() => {
        setToasts((t) => t.filter((x) => x.id !== id));
      }, 3200);
    },
    [],
  );

  // Load local keys + persisted UI state on mount + refresh roster.
  useEffect(() => {
    setSavedKeys(loadLocalKeys());
    setFlipPaidFirst(loadFlip());
    setPremium(loadPremium());
    fetchProviders();
  }, [fetchProviders]);

  // All unique providers from roster (deduped by canonical name).
  const allProviders = useMemo(() => {
    const seen = new Set<string>();
    const list: ProviderGroup[] = [];
    for (const p of providers) {
      if (!seen.has(p.name)) {
        seen.add(p.name);
        list.push(p);
      }
    }
    return list;
  }, [providers]);

  // Active-count for the subhead.
  const activeCount = useMemo(
    () =>
      allProviders.filter((p) =>
        isProviderActive(
          p.name,
          deriveKeyConfig(p.name),
          savedKeys,
          syncStatus,
          condensedModels,
        ),
      ).length,
    [allProviders, savedKeys, syncStatus, condensedModels],
  );

  // Split into free + paid. Premium-upgraded providers move from free to
  // paid. Providers not in either default list (e.g. groq, anthropic added
  // later by the roster) go into free as a fallback. Unknown providers
  // also fall back to free.
  const { freeList, paidList } = useMemo(() => {
    const free: ProviderGroup[] = [];
    const paid: ProviderGroup[] = [];
    const placed = new Set<string>();

    // 1. Place by default orders.
    for (const name of FREE_DEFAULT_ORDER) {
      const p = allProviders.find((x) => x.name === name);
      if (p && !premium[name]) {
        free.push(p);
        placed.add(name);
      } else if (p && premium[name]) {
        paid.push(p);
        placed.add(name);
      }
    }
    for (const name of PAID_DEFAULT_ORDER) {
      const p = allProviders.find((x) => x.name === name);
      if (p && !placed.has(name)) {
        paid.push(p);
        placed.add(name);
      }
    }
    // 2. Any provider not yet placed (extras from roster / future) → free.
    for (const p of allProviders) {
      if (placed.has(p.name)) continue;
      if (premium[p.name]) {
        paid.push(p);
      } else {
        free.push(p);
      }
      placed.add(p.name);
    }
    return { freeList: free, paidList: paid };
  }, [allProviders, premium]);

  // Stacks for rendering, honoring flip state.
  const stacks = useMemo(() => {
    const freeStack = {
      kind: "free" as const,
      label: "Free",
      items: freeList,
    };
    const paidStack = {
      kind: "paid" as const,
      label: "Paid",
      items: paidList,
    };
    return flipPaidFirst ? [paidStack, freeStack] : [freeStack, paidStack];
  }, [freeList, paidList, flipPaidFirst]);

  function toggleFlip() {
    const next = !flipPaidFirst;
    setFlipPaidFirst(next);
    saveFlip(next);
  }

  function upgradeToPremium(providerName: string) {
    const next = { ...premium, [providerName]: true as const };
    setPremium(next);
    savePremium(next);
    pushToast(`${providerName}: upgraded to Premium`, "ok");
  }

  function downgradeFromPremium(providerName: string) {
    const next = { ...premium };
    delete next[providerName];
    setPremium(next);
    savePremium(next);
    pushToast(`${providerName}: reverted to Free`, "info");
  }

  const handleSave = useCallback(
    async (
      providerName: string,
      cfg: ProviderKeyConfig,
      keyValue: string,
      extraValue: string,
    ) => {
      const trimmed = keyValue.trim();
      if (!trimmed) {
        pushToast("Paste a key first", "err");
        return;
      }
      const next = { ...savedKeys };
      next[cfg.envKey] = {
        key: trimmed,
        extra: cfg.extra ? extraValue.trim() : undefined,
      };
      setSavedKeys(next);
      saveLocalKeys(next);

      const r = await tryBackendSave(settings, cfg.envKey, trimmed);
      if (r.synced) {
        pushToast(`${providerName}: ${r.message}`, "ok");
      } else {
        if (cfg.extra && extraValue.trim()) {
          await tryBackendSave(settings, cfg.extra.envKey, extraValue.trim());
        }
        pushToast(`${providerName}: ${r.message}`, "info");
      }
      // Refresh roster so the Active badge reflects backend state.
      fetchProviders();
    },
    [savedKeys, settings, pushToast, fetchProviders],
  );

  const handleRemove = useCallback(
    async (providerName: string, cfg: ProviderKeyConfig) => {
      const next = { ...savedKeys };
      delete next[cfg.envKey];
      if (cfg.extra) delete next[cfg.extra.envKey];
      setSavedKeys(next);
      saveLocalKeys(next);
      const r = await tryBackendDelete(settings, cfg.envKey);
      pushToast(`${providerName}: ${r.message}`, "info");
      fetchProviders();
    },
    [savedKeys, settings, pushToast, fetchProviders],
  );

  function cfgExtra(providerName: string, sk: LocalKeyMap): string {
    const cfg = deriveKeyConfig(providerName);
    if (!cfg.extra) return "";
    return sk[cfg.extra.envKey]?.key || "";
  }

  return (
    <div className="flex flex-col h-full overflow-hidden relative">
      {/* Header — only rendered when not embedded (SettingsScreen already
          provides the tab bar header). */}
      {!embedded && (
        <div className="flex items-center gap-2 px-3 h-12 border-b border-border shrink-0 bg-surface/40">
          <span className="font-semibold text-foreground text-[14px]">
            Providers
          </span>
          <span className="text-[11px] text-muted-foreground">
            {allProviders.length} available · {activeCount} active
          </span>
          <button
            onClick={toggleFlip}
            className="touch-target ml-auto inline-flex items-center gap-1 px-2.5 h-8 rounded-xl border border-border text-[11px] hover:bg-surface2/60 transition-colors"
            title="Flip Free / Paid order"
            aria-label="Flip Free and Paid order"
            aria-pressed={flipPaidFirst}
          >
            <svg
              width="11"
              height="11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className={flipPaidFirst ? "rotate-180 transition-transform" : "transition-transform"}
            >
              <path d="m17 8 4 4-4 4" />
              <path d="M21 12H9" opacity="0.5" />
              <path d="m7 16-4-4 4-4" />
              <path d="M3 12h12" opacity="0.5" />
            </svg>
            Flip
          </button>
        </div>
      )}

      {/* Subhead — explanation. */}
      <div className="px-3 py-2.5 border-b border-border/50 text-[11.5px] text-muted-foreground leading-snug shrink-0 bg-surface/20">
        Sign up to providers and use their frontier models making your
        harness more capable.
        <br />
        <br />
        Sign up to a provider (most allow quick Google sign in) and paste
        your API key to get access.
      </div>

      {/* Lists — scrollable. */}
      <div className="flex-1 overflow-y-auto overscroll-contain">
        {allProviders.length === 0 ? (
          <div className="p-6 text-center text-[12px] text-muted-foreground">
            Loading providers… If this persists, check the connection in
            Settings.
          </div>
        ) : (
          <div className="flex flex-col gap-3 p-3 pb-8">
            {stacks.map((stack, stackIdx) => (
              <div key={stack.kind} className="flex flex-col gap-2">
                {/* Stack header — label + count + (flip button on embedded). */}
                <div className="flex items-center gap-2 px-1 sticky top-0 z-10 bg-background/80 backdrop-blur-sm py-1">
                  <span
                    className="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full"
                    style={{
                      color: stack.kind === "free" ? "#22c55e" : "#f59e0b",
                      backgroundColor:
                        stack.kind === "free" ? "#22c55e15" : "#f59e0b15",
                    }}
                  >
                    {stack.label}
                  </span>
                  <span className="text-[10px] text-muted-foreground/70 tabular-nums">
                    {stack.items.length} provider
                    {stack.items.length === 1 ? "" : "s"}
                  </span>
                  {/* Flip button — only on embedded (no header above) and only
                      once (on the first stack). */}
                  {embedded && stackIdx === 0 && (
                    <button
                      onClick={toggleFlip}
                      className="touch-target ml-auto inline-flex items-center gap-1 px-2 h-7 rounded-lg border border-border text-[10px] hover:bg-surface2/60 transition-colors"
                      title="Flip Free / Paid order"
                      aria-label="Flip Free and Paid order"
                      aria-pressed={flipPaidFirst}
                    >
                      <svg
                        width="10"
                        height="10"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        className={flipPaidFirst ? "rotate-180 transition-transform" : "transition-transform"}
                      >
                        <path d="m17 8 4 4-4 4" />
                        <path d="M21 12H9" opacity="0.5" />
                        <path d="m7 16-4-4 4-4" />
                        <path d="M3 12h12" opacity="0.5" />
                      </svg>
                      Flip
                    </button>
                  )}
                </div>

                {stack.items.length === 0 ? (
                  <div className="text-[11px] text-muted-foreground/50 px-2 py-3 italic">
                    No {stack.label.toLowerCase()} providers.
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">
                    {stack.items.map((p) => {
                      const cfg = deriveKeyConfig(p.name);
                      const isPremium = !!premium[p.name];
                      const isActive = isProviderActive(
                        p.name,
                        cfg,
                        savedKeys,
                        syncStatus,
                        condensedModels,
                      );
                      return (
                        <ProviderCard
                          key={p.name}
                          provider={p}
                          cfg={cfg}
                          savedKey={savedKeys[cfg.envKey]?.key || ""}
                          savedExtra={cfgExtra(p.name, savedKeys)}
                          expanded={expanded === p.name}
                          onToggle={() =>
                            setExpanded((cur) =>
                              cur === p.name ? null : p.name,
                            )
                          }
                          onSave={(k, x) =>
                            handleSave(
                              p.displayName || p.name,
                              cfg,
                              k,
                              x,
                            )
                          }
                          onRemove={() =>
                            handleRemove(p.displayName || p.name, cfg)
                          }
                          isActive={isActive}
                          isPremium={isPremium}
                          canUpgrade={PREMIUM_ELIGIBLE.has(p.name) && !isPremium}
                          onUpgrade={() => upgradeToPremium(p.name)}
                          onDowngrade={() => downgradeFromPremium(p.name)}
                          onOpenDetail={() => openProvidersDialog(p.name)}
                        />
                      );
                    })}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Toast stack */}
      <div className="pointer-events-none fixed inset-x-0 bottom-16 sm:bottom-4 z-50 flex flex-col items-center gap-1.5 px-3">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, y: 10, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 10, scale: 0.96 }}
              transition={{ duration: 0.18 }}
              className={`pointer-events-auto max-w-md w-fit text-[12px] px-3 py-1.5 rounded-xl border shadow-xl backdrop-blur ${
                t.kind === "ok"
                  ? "bg-emerald-500/15 border-emerald-500/40 text-emerald-200"
                  : t.kind === "err"
                  ? "bg-rose-500/15 border-rose-500/40 text-rose-200"
                  : "bg-surface/95 border-border text-foreground"
              }`}
              role="status"
            >
              {t.message}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

// ── ProviderCard ─────────────────────────────────────────────────────────
interface ProviderCardProps {
  provider: ProviderGroup;
  cfg: ProviderKeyConfig;
  savedKey: string;
  savedExtra: string;
  expanded: boolean;
  onToggle: () => void;
  onSave: (key: string, extra: string) => void;
  onRemove: () => void;
  isActive: boolean;
  isPremium: boolean;
  canUpgrade: boolean;
  onUpgrade: () => void;
  onDowngrade: () => void;
  onOpenDetail: () => void;
}

function ProviderCard({
  provider: p,
  cfg,
  savedKey,
  savedExtra,
  expanded,
  onToggle,
  onSave,
  onRemove,
  isActive,
  isPremium,
  canUpgrade,
  onUpgrade,
  onDowngrade,
  onOpenDetail,
}: ProviderCardProps) {
  const [keyInput, setKeyInput] = useState(savedKey);
  const [extraInput, setExtraInput] = useState(savedExtra);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setKeyInput(savedKey);
  }, [savedKey]);
  useEffect(() => {
    setExtraInput(savedExtra);
  }, [savedExtra]);

  const color = p.color || "#a855f7";
  const description = isPremium
    ? cfg.premiumMotto || cfg.paidDescription || cfg.freeDescription
    : cfg.freeDescription;

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave(keyInput, extraInput);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="rounded-xl border overflow-hidden bg-surface/40"
      style={{ borderColor: `${color}30` }}
    >
      {/* Header — name + status + gear + chevron. */}
      <div className="flex items-center gap-2 px-3 min-h-[44px]">
        <button
          onClick={onToggle}
          className="touch-target flex-1 flex items-center gap-2 min-w-0 text-left rounded-xl hover:bg-surface2/40 transition-colors -mx-1 px-1 py-1"
          aria-expanded={expanded}
        >
          <span
            className="inline-flex items-center justify-center w-7 h-7 rounded-lg shrink-0 font-semibold text-[11px]"
            style={{ backgroundColor: `${color}1f`, color }}
          >
            {(p.displayName || p.name).slice(0, 1).toUpperCase()}
          </span>
          <div className="flex-1 min-w-0">
            <div className="text-[12.5px] font-medium text-foreground truncate flex items-center gap-1.5">
              {p.displayName || p.name}
              {isPremium && (
                <span
                  className="text-[8.5px] px-1 py-px rounded-full font-semibold uppercase tracking-wide"
                  style={{
                    color: "#f59e0b",
                    backgroundColor: "#f59e0b15",
                  }}
                  title="Premium tier"
                >
                  Premium
                </span>
              )}
            </div>
            <div className="text-[10px] text-muted-foreground/70 truncate font-mono">
              {cfg.envKey}
              {p.models.length > 0 ? ` · ${p.models.length} models` : ""}
            </div>
          </div>
        </button>

        {/* Status badge */}
        {isActive ? (
          <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 font-medium shrink-0 flex items-center gap-1">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            Active
          </span>
        ) : (
          <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-surface2 text-muted-foreground/70 shrink-0">
            Not configured
          </span>
        )}

        {/* Settings gear → opens ProvidersDialog (privacy + usage limits). */}
        <button
          onClick={onOpenDetail}
          className="touch-target inline-flex items-center justify-center size-8 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2/60 transition-colors shrink-0"
          aria-label={`Open ${p.displayName} details — usage limits & privacy`}
          title="Usage limits & privacy"
        >
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        </button>

        {/* Expand chevron */}
        <button
          onClick={onToggle}
          className="touch-target inline-flex items-center justify-center size-7 rounded-xl text-muted-foreground hover:bg-surface2/60 transition-colors shrink-0"
          aria-label={expanded ? `Collapse ${p.displayName}` : `Expand ${p.displayName}`}
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`transition-transform ${expanded ? "rotate-180" : ""}`}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      </div>

      {/* Expanded body — description + inputs + actions. */}
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
            className="overflow-hidden border-t border-border/50"
          >
            <div className="flex flex-col gap-3 p-3">
              {/* Description — changes when user upgrades to premium. */}
              <p className="text-[11.5px] text-muted-foreground leading-snug">
                {description}
              </p>
              {p.privacy?.notice && (
                <p
                  className="text-[10px] text-muted-foreground/70 leading-snug border-l-2 pl-2 italic"
                  style={{ borderColor: `${color}40` }}
                >
                  {p.privacy.notice}
                </p>
              )}

              {/* API key input */}
              <div>
                <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                  API Key
                </label>
                <div className="flex items-stretch gap-1.5">
                  <input
                    type={showKey ? "text" : "password"}
                    value={keyInput}
                    onChange={(e) => setKeyInput(e.target.value)}
                    placeholder={cfg.placeholder}
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    className="flex-1 min-w-0 bg-surface2 border border-border rounded-xl px-3 py-2 text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/60 transition-colors font-mono"
                  />
                  <button
                    onClick={() => setShowKey((s) => !s)}
                    className="touch-target px-2.5 rounded-xl border border-border text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors text-[11px] shrink-0"
                    title={showKey ? "Hide" : "Show"}
                    aria-label={showKey ? "Hide key" : "Show key"}
                  >
                    {showKey ? "🙈" : "👁"}
                  </button>
                </div>
              </div>

              {/* Extra field (e.g. Cloudflare Account ID) */}
              {cfg.extra && (
                <div>
                  <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                    {cfg.extra.label}
                  </label>
                  <input
                    type="text"
                    value={extraInput}
                    onChange={(e) => setExtraInput(e.target.value)}
                    placeholder={cfg.extra.placeholder}
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    className="w-full bg-surface2 border border-border rounded-xl px-3 py-2 text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/60 transition-colors font-mono"
                  />
                  <p className="text-[10px] text-muted-foreground/60 mt-1 leading-snug">
                    {cfg.extra.hint}
                  </p>
                </div>
              )}

              {/* Action row */}
              <div className="flex items-center gap-1.5 flex-wrap pt-1">
                {/* Signup link */}
                <a
                  href={p.settingsUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="touch-target inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-[12px] font-medium text-white transition-opacity hover:opacity-90"
                  style={{ backgroundColor: color }}
                >
                  <svg
                    width="11"
                    height="11"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L21 5" />
                    <path d="m21 2-9.6 9.6" />
                    <circle cx="7.5" cy="15.5" r="5.5" />
                  </svg>
                  {p.manageLabel || "Get API Key"}
                  <svg
                    width="9"
                    height="9"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M15 3h6v6" />
                    <path d="M10 14 21 3" />
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                  </svg>
                </a>

                {/* Save / Update */}
                <button
                  onClick={handleSave}
                  disabled={saving || !keyInput.trim()}
                  className="touch-target inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-accent text-white text-[12px] font-medium hover:bg-accent/90 transition-colors disabled:opacity-40"
                >
                  {saving ? (
                    <svg
                      width="11"
                      height="11"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="animate-spin"
                    >
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                    </svg>
                  ) : (
                    <svg
                      width="11"
                      height="11"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                      <polyline points="17 21 17 13 7 13 7 21" />
                      <polyline points="7 3 7 8 15 8" />
                    </svg>
                  )}
                  {saving ? "Saving…" : isActive ? "Update" : "Save"}
                </button>

                {/* Premium upgrade / downgrade */}
                {canUpgrade && (
                  <button
                    onClick={onUpgrade}
                    className="touch-target inline-flex items-center gap-1 px-2.5 py-2 rounded-xl border border-amber-500/50 text-amber-400 hover:bg-amber-500/10 transition-colors text-[11.5px] font-medium"
                    title="Move to paid list with premium-tier description"
                  >
                    <svg
                      width="11"
                      height="11"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="m12 2 2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8 5.8 21.3l2.4-7.4L2 9.4h7.6z" />
                    </svg>
                    Up to Premium
                  </button>
                )}
                {isPremium && PREMIUM_ELIGIBLE.has(p.name) && (
                  <button
                    onClick={onDowngrade}
                    className="touch-target inline-flex items-center gap-1 px-2.5 py-2 rounded-xl border border-border text-muted-foreground hover:text-foreground transition-colors text-[11px]"
                    title="Revert to free tier"
                  >
                    Revert to Free
                  </button>
                )}

                {/* Remove saved key */}
                {isActive && savedKey && (
                  <button
                    onClick={onRemove}
                    className="touch-target inline-flex items-center gap-1 px-2.5 py-2 rounded-xl border border-border text-muted-foreground hover:text-rose-400 hover:border-rose-400/40 transition-colors text-[11.5px]"
                    title="Remove saved key"
                  >
                    <svg
                      width="11"
                      height="11"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6" />
                      <path d="M10 11v6M14 11v6" />
                    </svg>
                    Remove
                  </button>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
