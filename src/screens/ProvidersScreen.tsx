/**
 * ProvidersScreen — full-screen management of provider API keys.
 *
 * For each provider in the roster (NVIDIA, Cloudflare, OpenRouter, GitHub
 * Models, PrivateMode AI, etc.), the user can:
 *   - Open the provider's signup page (external link).
 *   - Paste an API key into a masked input.
 *   - Save the key (sent to the backend `/api/keys` if available; falls
 *     back to local-only storage with a clear toast).
 *   - See an "Active" / "Not configured" status badge.
 *   - For Cloudflare: an extra "Account ID" input (CF requires both).
 *
 * Mobile-first:
 *   - Full-screen on mobile (no padding).
 *   - Each provider is a collapsible card.
 *   - Input fields use 16px font on mobile to prevent iOS Safari zoom.
 *   - Toast feedback for save success/failure.
 *
 * Navigation: added to App.tsx bottom nav between Settings and Debug.
 */

import { useEffect, useMemo, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { Settings } from "../api/panel";
import { deriveToken } from "../api/token";
import { useModelStore, type ProviderGroup } from "../lib/model-store";

// ── Per-provider config ──────────────────────────────────────────────────
// The roster's ProviderGroup gives us displayName / settingsUrl / color, but
// we need to know which env-var name(s) the backend expects for each key
// (e.g. Cloudflare needs CF_API_TOKEN + CF_ACCOUNT_ID). This map is keyed
// on the canonical provider name from the backend roster.

interface ProviderKeyConfig {
  /** The env var name the backend reads (e.g. "NVIDIA_API_KEY"). */
  envKey: string;
  /** Placeholder for the API key input (e.g. "nvapi-…"). */
  placeholder: string;
  /** Short hint shown under the input. */
  hint: string;
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
  },
  cloudflare: {
    envKey: "CF_API_TOKEN",
    placeholder: "v1.0-…",
    hint: "Workers AI. Requires both an API token AND your Account ID.",
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
  },
  "github-models": {
    envKey: "GITHUB_TOKEN",
    placeholder: "ghp_…",
    hint: "GitHub Models — free preview tier with your GitHub PAT.",
  },
  privatemodeai: {
    envKey: "PRIVATEMODEAI_API_KEY",
    placeholder: "pmai-…",
    hint: "Privacy-first gateway for Kimi, GLM, and more.",
  },
  groq: {
    envKey: "GROQ_API_KEY",
    placeholder: "gsk_…",
    hint: "Very fast inference, generous free tier.",
  },
  google: {
    envKey: "GOOGLE_API_KEY",
    placeholder: "AIza…",
    hint: "Gemini Flash & Pro — free tier.",
  },
  anthropic: {
    envKey: "ANTHROPIC_API_KEY",
    placeholder: "sk-ant-…",
    hint: "Claude models (paid). Optional — unlocks the Claude tier.",
  },
  deepseek: {
    envKey: "DEEPSEEK_API_KEY",
    placeholder: "sk-…",
    hint: "DeepSeek V3 / R1 — very cheap, very strong on math.",
  },
  mistral: {
    envKey: "MISTRAL_API_KEY",
    placeholder: "…",
    hint: "Mistral Large / Codestral — European hosting.",
  },
};

// Fallback for providers without explicit config — derive an env-var name
// from the provider's canonical name (e.g. "foo-bar" → "FOO_BAR_API_KEY").
function deriveKeyConfig(providerName: string): ProviderKeyConfig {
  const known = PROVIDER_KEY_CONFIG[providerName];
  if (known) return known;
  const env = `${providerName.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_API_KEY`;
  return {
    envKey: env,
    placeholder: "paste key…",
    hint: "Add a PROVIDER_KEY_CONFIG entry in ProvidersScreen.tsx for nicer UX.",
  };
}

// ── Local-storage fallback ───────────────────────────────────────────────
// Used when the backend doesn't expose /api/keys (yet). Keys saved here are
// never sent to the backend in this fallback path — they only persist on
// the device. The toast makes this explicit.
const LOCAL_KEY = "doomalaysocreate.providerKeys.v1";

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

// ── Backend save attempts ────────────────────────────────────────────────
// Try POST /api/keys first (the "official" path). If the backend doesn't
// have it, fall back to /oauth/set-provider-key (only useful when the user
// has an OAuth-provisioned HF Space — but harmless to try otherwise).

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

  // Attempt 1: POST /api/keys
  try {
    const r = await fetch(`${settings.baseUrl}/api/keys`, {
      method: "POST",
      headers,
      body: JSON.stringify({ key_name: envKey, key_value: value }),
    });
    if (r.ok) {
      return { synced: true, message: "Saved to backend" };
    }
    if (r.status === 404 || r.status === 405) {
      // Endpoint doesn't exist — fall through to attempt 2.
    } else {
      const j = await r.json().catch(() => ({}));
      return { synced: false, message: (j as { error?: string }).error || `HTTP ${r.status}` };
    }
  } catch {
    /* network error — fall through */
  }

  // Attempt 2: POST /oauth/set-provider-key (HF Space OAuth flow).
  // Requires oauth_token + repo. We can't know those here without the
  // provision result, so we just bail to local storage.
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
    const r = await fetch(`${settings.baseUrl}/api/keys/${encodeURIComponent(envKey)}`, {
      method: "DELETE",
      headers,
    });
    if (r.ok) return { synced: true, message: "Removed from backend" };
    if (r.status === 404 || r.status === 405) {
      return { synced: false, message: "Removed locally (backend sync unavailable)" };
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

// ── Component ────────────────────────────────────────────────────────────
export function ProvidersScreen({ settings }: { settings: Settings }) {
  const providers = useModelStore((s) => s.providers);
  const fetchProviders = useModelStore((s) => s.fetchProviders);

  // Saved keys (local fallback + status source of truth). Keyed by env-var
  // name (one entry per env var, so Cloudflare has 2 entries).
  const [savedKeys, setSavedKeys] = useState<LocalKeyMap>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Push a transient toast.
  const pushToast = useCallback((message: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, message, kind }]);
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id));
    }, 3200);
  }, []);

  // Load local keys on mount + refresh the provider roster (so the screen
  // reflects current provider availability even if it was opened before
  // the roster sync completed).
  useEffect(() => {
    setSavedKeys(loadLocalKeys());
    fetchProviders();
  }, [fetchProviders]);

  // Group providers so the same logical provider doesn't appear twice if
  // the roster has duplicates. We also include any PROVIDER_KEY_CONFIG
  // providers that aren't in the roster (so the user can pre-set keys for
  // providers not yet synced).
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
      // Optimistically persist locally.
      const next = { ...savedKeys };
      next[cfg.envKey] = { key: trimmed, extra: cfg.extra ? extraValue.trim() : undefined };
      setSavedKeys(next);
      saveLocalKeys(next);

      // Try the backend.
      const r = await tryBackendSave(settings, cfg.envKey, trimmed);
      if (r.synced) {
        pushToast(`${providerName}: ${r.message}`, "ok");
      } else {
        // For Cloudflare's extra field (Account ID), also send to backend.
        if (cfg.extra && extraValue.trim()) {
          await tryBackendSave(settings, cfg.extra.envKey, extraValue.trim());
        }
        pushToast(`${providerName}: ${r.message}`, "info");
      }
    },
    [savedKeys, settings, pushToast],
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
    },
    [savedKeys, settings, pushToast],
  );

  return (
    <div className="flex flex-col h-full overflow-hidden relative">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-12 border-b border-border shrink-0 bg-surface/40">
        <span className="font-semibold text-foreground text-[14px]">Providers</span>
        <span className="text-[11px] text-muted-foreground">
          {allProviders.length} available · {Object.keys(savedKeys).length} key{Object.keys(savedKeys).length === 1 ? "" : "s"} set
        </span>
      </div>

      {/* Subhead — explanation + privacy note. */}
      <div className="px-3 py-2 border-b border-border/50 text-[11px] text-muted-foreground leading-snug shrink-0">
        Paste your API keys to enable models from each provider. Keys are sent
        to your backend over the wire token; if no <code className="text-accent/80 font-mono">/api/keys</code> endpoint is
        configured, they fall back to local-only storage (with a clear toast).
      </div>

      {/* Provider list — collapsible cards. */}
      <div className="flex-1 overflow-y-auto">
        {allProviders.length === 0 ? (
          <div className="p-6 text-center text-[12px] text-muted-foreground">
            Loading providers… If this persists, check the connection in Settings.
          </div>
        ) : (
          <div className="flex flex-col gap-2 p-3">
            {allProviders.map((p) => (
              <ProviderCard
                key={p.name}
                provider={p}
                cfg={deriveKeyConfig(p.name)}
                savedKey={savedKeys[deriveKeyConfig(p.name).envKey]?.key || ""}
                savedExtra={
                  cfgExtra(p.name, savedKeys)
                }
                expanded={expanded === p.name}
                onToggle={() =>
                  setExpanded((cur) => (cur === p.name ? null : p.name))
                }
                onSave={(k, x) => handleSave(p.displayName || p.name, deriveKeyConfig(p.name), k, x)}
                onRemove={() => handleRemove(p.displayName || p.name, deriveKeyConfig(p.name))}
              />
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

function cfgExtra(providerName: string, savedKeys: LocalKeyMap): string {
  const cfg = deriveKeyConfig(providerName);
  if (!cfg.extra) return "";
  return savedKeys[cfg.extra.envKey]?.key || "";
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
}: ProviderCardProps) {
  // Local input state — seeded from savedKey so saved keys are visible.
  const [keyInput, setKeyInput] = useState(savedKey);
  const [extraInput, setExtraInput] = useState(savedExtra);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);

  // Re-seed when savedKey changes externally.
  useEffect(() => { setKeyInput(savedKey); }, [savedKey]);
  useEffect(() => { setExtraInput(savedExtra); }, [savedExtra]);

  const isConfigured = !!savedKey;
  const color = p.color || "#a855f7";

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
      {/* Header — always visible. Click to expand/collapse. */}
      <button
        onClick={onToggle}
        className="touch-target w-full flex items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-surface2/40"
        aria-expanded={expanded}
      >
        <span
          className="inline-flex items-center justify-center w-7 h-7 rounded-lg shrink-0 font-semibold text-[11px]"
          style={{ backgroundColor: `${color}1f`, color }}
        >
          {(p.displayName || p.name).slice(0, 1).toUpperCase()}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-[12.5px] font-medium text-foreground truncate">
            {p.displayName || p.name}
          </div>
          <div className="text-[10px] text-muted-foreground/70 truncate font-mono">
            {cfg.envKey}
            {p.region ? ` · ${p.region}` : ""}
            {p.models.length > 0 ? ` · ${p.models.length} models` : ""}
          </div>
        </div>
        {/* Status badge */}
        {isConfigured ? (
          <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 font-medium shrink-0 flex items-center gap-1">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            Active
          </span>
        ) : (
          <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-surface2 text-muted-foreground/70 shrink-0">
            Not configured
          </span>
        )}
        {/* Expand chevron */}
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`text-muted-foreground shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {/* Expanded body — input fields + actions. */}
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
              {/* Description */}
              <p className="text-[11px] text-muted-foreground leading-snug">
                {cfg.hint}
              </p>
              {p.privacy?.notice && (
                <p className="text-[10px] text-muted-foreground/70 leading-snug border-l-2 pl-2 italic" style={{ borderColor: `${color}40` }}>
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
                <a
                  href={p.settingsUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="touch-target inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-[12px] font-medium text-white transition-opacity hover:opacity-90"
                  style={{ backgroundColor: color }}
                >
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L21 5" />
                    <path d="m21 2-9.6 9.6" />
                    <circle cx="7.5" cy="15.5" r="5.5" />
                  </svg>
                  {p.manageLabel || "Get API Key"}
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M15 3h6v6" />
                    <path d="M10 14 21 3" />
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                  </svg>
                </a>
                <button
                  onClick={handleSave}
                  disabled={saving || !keyInput.trim()}
                  className="touch-target inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-accent text-white text-[12px] font-medium hover:bg-accent/90 transition-colors disabled:opacity-40"
                >
                  {saving ? (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                    </svg>
                  ) : (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                      <polyline points="17 21 17 13 7 13 7 21" />
                      <polyline points="7 3 7 8 15 8" />
                    </svg>
                  )}
                  {saving ? "Saving…" : isConfigured ? "Update" : "Save"}
                </button>
                {isConfigured && (
                  <button
                    onClick={onRemove}
                    className="touch-target inline-flex items-center gap-1 px-2.5 py-2 rounded-xl border border-border text-muted-foreground hover:text-rose-400 hover:border-rose-400/40 transition-colors text-[11.5px]"
                    title="Remove saved key"
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
