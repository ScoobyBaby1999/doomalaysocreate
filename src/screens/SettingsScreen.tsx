import { useEffect, useState, useCallback } from "react";
import { PanelClient, type Settings } from "../api/panel";
import { deriveToken } from "../api/token";

/** Settings screen — connection config + provider API key management.
 *  Shows a visual indicator (green/gray dot) for each provider key, fetched
 *  from /api/provider-keys/status. Users can set/update keys directly from
 *  this screen via /oauth/set-provider-key (JWT-authed, no onboarding token
 *  needed). */

// --- Provider definitions --------------------------------------------------
// Each provider: display label, env var name, where to get the key, hint.
// Groq is intentionally excluded (best model is GPT-120B-OSS — underperforms).
interface Provider {
  envKey: string;
  label: string;
  hint: string;
  keyUrl: string;
  placeholder: string;
  free?: boolean;
  paired?: string; // paired key (e.g. CF_API_TOKEN pairs with CF_ACCOUNT_ID)
}

const PROVIDERS: Provider[] = [
  // Free tier providers (shown first)
  { envKey: "PUTER_API_TOKEN", label: "Puter (GLM 5.2)", hint: "FREE GLM 5.2 — no phone, no payment",
    keyUrl: "https://puter.com/dashboard", placeholder: "eyJ...", free: true },
  { envKey: "NVIDIA_API_KEY", label: "NVIDIA AI", hint: "Free credits — GLM-5.1, Kimi, Llama",
    keyUrl: "https://build.nvidia.com", placeholder: "nvapi-…", free: true },
  { envKey: "SILICONFLOW_API_KEY", label: "SiliconFlow", hint: "Free tier — GLM models, GitHub login",
    keyUrl: "https://siliconflow.com", placeholder: "sf-…", free: true },
  // Paid providers
  { envKey: "ZAI_API_KEY", label: "Z.ai (GLM 5.2)", hint: "Real GLM 5.2 — limited-time free input",
    keyUrl: "https://z.ai", placeholder: "…" },
  { envKey: "MOONSHOT_API_KEY", label: "Moonshot (Kimi)", hint: "Kimi K2 — agentic coding",
    keyUrl: "https://platform.moonshot.cn/console/api-keys", placeholder: "sk-…" },
  { envKey: "OPENROUTER_API_KEY", label: "OpenRouter", hint: "100+ models, $1 free credit",
    keyUrl: "https://openrouter.ai/keys", placeholder: "sk-or-…" },
  { envKey: "CEREBRAS_API_KEY", label: "Cerebras", hint: "Ultra-fast Qwen3 inference",
    keyUrl: "https://cloud.cerebras.ai", placeholder: "…" },
  { envKey: "GEMINI_API_KEY", label: "Google AI (Gemini)", hint: "Gemini 2.5 Flash — free tier",
    keyUrl: "https://aistudio.google.com/apikey", placeholder: "AIza…" },
  { envKey: "ANTHROPIC_API_KEY", label: "Anthropic (Claude)", hint: "Claude agent tier — paid",
    keyUrl: "https://console.anthropic.com/settings/keys", placeholder: "sk-ant-…" },
  // Cloudflare (needs two keys)
  { envKey: "CF_API_TOKEN", label: "Cloudflare API Token", hint: "For Workers AI inference",
    keyUrl: "https://dash.cloudflare.com/profile/api-tokens", placeholder: "…", paired: "CF_ACCOUNT_ID" },
  { envKey: "CF_ACCOUNT_ID", label: "Cloudflare Account ID", hint: "Paired with CF_API_TOKEN",
    keyUrl: "https://dash.cloudflare.com", placeholder: "abc123…" },
  // Other tools
  { envKey: "TAVILY_API_KEY", label: "Tavily (Web Search)", hint: "For agent web search",
    keyUrl: "https://tavily.com", placeholder: "tvly-…" },
  { envKey: "GITHUB_TOKEN", label: "GitHub Token", hint: "For git operations (optional)",
    keyUrl: "https://github.com/settings/tokens", placeholder: "ghp_…" },
];

// --- Key status type -------------------------------------------------------
interface KeyStatus {
  set: boolean;
  preview: string;
}

// --- Helper: bearer for API calls -----------------------------------------
async function bearer(settings: Settings, windowsBack = 0): Promise<string> {
  if (settings.rotationSecret) return deriveToken(settings.rotationSecret, windowsBack);
  return settings.token;
}

// --- Main component --------------------------------------------------------
export function SettingsScreen({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (s: Settings) => void;
}) {
  const [draft, setDraft] = useState<Settings>(settings);
  const [status, setStatus] = useState<string>("");
  const [frontier, setFrontier] = useState<string>("");
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [keyStatuses, setKeyStatuses] = useState<Record<string, KeyStatus>>({});
  const [keyValues, setKeyValues] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [keyErrors, setKeyErrors] = useState<Record<string, string>>({});
  const [activeSection, setActiveSection] = useState<"connection" | "providers">("connection");

  useEffect(() => setDraft(settings), [settings]);

  // Fetch provider key status on mount + when settings change
  const fetchKeyStatuses = useCallback(async () => {
    if (!settings.token && !settings.rotationSecret) return;
    try {
      const token = await bearer(settings);
      const r = await fetch(`${settings.baseUrl}/api/provider-keys/status`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (r.ok) {
        const data = await r.json();
        setKeyStatuses(data.keys || {});
      }
    } catch {
      // silent — status just won't show
    }
  }, [settings]);

  useEffect(() => {
    fetchKeyStatuses();
  }, [fetchKeyStatuses]);

  async function test() {
    setStatus("checking…");
    setFrontier("");
    onChange(draft);
    const client = new PanelClient(draft);
    try {
      const h = (await client.health()) as Record<string, unknown>;
      const ok = (h.frontier_ok as boolean) ? "✅" : "⚠️";
      setStatus(`connected ${ok}`);
      const r = await client.roster();
      setFrontier(
        `${r.frontier_privacy_safe_available}/${r.frontier_total} privacy-safe frontier models available`,
      );
    } catch (e) {
      setStatus("✗ " + (e instanceof Error ? e.message : String(e)));
    }
  }

  async function saveProviderKey(provider: Provider) {
    const val = (keyValues[provider.envKey] || "").trim();
    if (!val) return;
    setSavingKey(provider.envKey);
    setKeyErrors((e) => ({ ...e, [provider.envKey]: "" }));
    try {
      const token = await bearer(settings);
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      };
      // Send JWT in X-JWT so the backend can look up the stored HF token
      if (settings.githubSessionId) {
        headers["X-JWT"] = settings.githubSessionId;
      }
      const r = await fetch(`${settings.baseUrl}/oauth/set-provider-key`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          key_name: provider.envKey,
          key_value: val,
          // No oauth_token/repo — backend uses JWT to look up stored HF token
        }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j as { error?: string }).error || `HTTP ${r.status}`);
      }
      // Success — update the visual indicator
      setKeyStatuses((s) => ({
        ...s,
        [provider.envKey]: { set: true, preview: val.slice(0, 4) + "..." + val.slice(-4) },
      }));
      setKeyValues((v) => ({ ...v, [provider.envKey]: "" }));
    } catch (e) {
      setKeyErrors((er) => ({
        ...er,
        [provider.envKey]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setSavingKey(null);
    }
  }

  function handleLogout() {
    if (!confirmLogout) {
      setConfirmLogout(true);
      return;
    }
    localStorage.removeItem("loom.settings.v1");
    localStorage.removeItem("loom.settings.expiry");
    localStorage.removeItem("loom.agent.session");
    localStorage.removeItem("loom.agent.model");
    sessionStorage.clear();
    window.location.reload();
  }

  const field =
    "w-full bg-surface border border-border rounded-xl px-3 py-2 text-[15px] outline-none focus:border-accent";

  const setCount = Object.values(keyStatuses).filter((s) => s.set).length;

  return (
    <div className="p-4 space-y-4 max-w-xl mx-auto">
      {/* Section tabs */}
      <div className="flex gap-2 border-b border-border">
        <button
          onClick={() => setActiveSection("connection")}
          className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeSection === "connection"
              ? "border-accent text-accent"
              : "border-transparent text-muted"
          }`}
        >
          Connection
        </button>
        <button
          onClick={() => setActiveSection("providers")}
          className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors flex items-center gap-1.5 ${
            activeSection === "providers"
              ? "border-accent text-accent"
              : "border-transparent text-muted"
          }`}
        >
          Provider Keys
          {setCount > 0 && (
            <span className="text-[10px] bg-green-500/20 text-green-400 px-1.5 py-0.5 rounded-full">
              {setCount} set
            </span>
          )}
        </button>
      </div>

      {/* Connection section */}
      {activeSection === "connection" && (
        <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold mb-1">Connection</h2>
            <p className="text-sm text-muted">
              Point the app at your panel space and paste your rotation secret (or a token).
            </p>
          </div>
          <label className="block space-y-1">
            <span className="text-sm text-muted">Gateway URL</span>
            <input
              className={field}
              value={draft.baseUrl}
              onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
              placeholder="empty = this Space · or https://<your-space>.hf.space"
              autoCapitalize="none"
              autoCorrect="off"
            />
            <span className="text-[11px] text-muted">
              Leave empty when the app is served by your Space itself.
            </span>
          </label>
          <label className="block space-y-1">
            <span className="text-sm text-muted">Rotation secret (recommended)</span>
            <input
              className={field}
              type="password"
              value={draft.rotationSecret}
              onChange={(e) => setDraft({ ...draft, rotationSecret: e.target.value })}
              placeholder="CRITIQUE_ROTATION_SECRET"
              autoCapitalize="none"
              autoCorrect="off"
            />
            <span className="text-[11px] text-muted">
              Stored only on this device; the 6-hour wire token is derived from it per call.
            </span>
          </label>
          <label className="block space-y-1">
            <span className="text-sm text-muted">Static bearer token (fallback)</span>
            <input
              className={field}
              type="password"
              value={draft.token}
              onChange={(e) => setDraft({ ...draft, token: e.target.value })}
              placeholder="CRITIQUE_TOKEN or gen_token.py output"
              autoCapitalize="none"
              autoCorrect="off"
            />
            <span className="text-[11px] text-muted">
              Only used when no rotation secret is set.
            </span>
          </label>
          <button
            onClick={test}
            className="px-4 py-2 rounded-xl bg-accent text-white font-medium"
          >
            Save & test
          </button>
          {status && <p className="text-sm">{status}</p>}
          {frontier && <p className="text-sm text-muted">{frontier}</p>}
        </div>
      )}

      {/* Provider Keys section */}
      {activeSection === "providers" && (
        <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold mb-1">Provider API Keys</h2>
            <p className="text-sm text-muted">
              Set API keys for AI providers. Green dot = key is set on this Space.
              Keys are stored as HF Space Secrets (never in the frontend).
              {setCount > 0 && (
                <span className="text-green-400"> {setCount} key{setCount !== 1 ? "s" : ""} currently set.</span>
              )}
            </p>
          </div>

          {/* Free tier providers first */}
          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-green-400 uppercase tracking-wide">
              Free Tier
            </h3>
            {PROVIDERS.filter((p) => p.free).map((p) => (
              <ProviderRow
                key={p.envKey}
                provider={p}
                status={keyStatuses[p.envKey]}
                value={keyValues[p.envKey] || ""}
                saving={savingKey === p.envKey}
                error={keyErrors[p.envKey]}
                onValueChange={(v) => setKeyValues((s) => ({ ...s, [p.envKey]: v }))}
                onSave={() => saveProviderKey(p)}
              />
            ))}
          </div>

          {/* Paid providers */}
          <div className="space-y-3 pt-2">
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wide">
              Paid / BYOK
            </h3>
            {PROVIDERS.filter((p) => !p.free).map((p) => (
              <ProviderRow
                key={p.envKey}
                provider={p}
                status={keyStatuses[p.envKey]}
                value={keyValues[p.envKey] || ""}
                saving={savingKey === p.envKey}
                error={keyErrors[p.envKey]}
                onValueChange={(v) => setKeyValues((s) => ({ ...s, [p.envKey]: v }))}
                onSave={() => saveProviderKey(p)}
              />
            ))}
          </div>

          <p className="text-[11px] text-muted pt-2 border-t border-border">
            After setting a key, your Space restarts briefly to apply it.
            Keys never expire and are stored encrypted in HF Space Secrets.
          </p>
        </div>
      )}

      {/* Logout / Danger Zone — always visible */}
      <div className="border-t border-border pt-5 mt-5">
        <h3 className="text-sm font-medium text-rose-300 mb-2">Account</h3>
        <p className="text-[11px] text-muted mb-3">
          Sign out and clear all stored credentials from this device. You'll need to
          reconnect GitHub and re-enter your settings.
        </p>
        {confirmLogout ? (
          <div className="space-y-2">
            <p className="text-sm text-rose-300">Are you sure? This will reload the app.</p>
            <div className="flex gap-2">
              <button
                onClick={handleLogout}
                className="flex-1 py-1.5 rounded-lg bg-rose-600 text-white text-sm"
              >
                Yes, log out
              </button>
              <button
                onClick={() => setConfirmLogout(false)}
                className="flex-1 py-1.5 rounded-lg border border-border text-sm"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={handleLogout}
            className="w-full py-1.5 rounded-lg border border-rose-500/40 text-rose-300 text-sm hover:bg-rose-500/10"
          >
            Log out & clear credentials
          </button>
        )}
      </div>
    </div>
  );
}

// --- Provider row component (visual indicator + input + save) -------------
function ProviderRow({
  provider,
  status,
  value,
  saving,
  error,
  onValueChange,
  onSave,
}: {
  provider: Provider;
  status: KeyStatus | undefined;
  value: string;
  saving: boolean;
  error: string | undefined;
  onValueChange: (v: string) => void;
  onSave: () => void;
}) {
  const isSet = status?.set ?? false;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        {/* Visual indicator: green dot = set, gray dot = not set */}
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${
            isSet ? "bg-green-400" : "bg-gray-500"
          }`}
          title={isSet ? `Set (${status?.preview})` : "Not set"}
        />
        <span className="text-sm font-medium">{provider.label}</span>
        <span className="text-[11px] text-muted">{provider.hint}</span>
        {isSet && (
          <span className="ml-auto text-[10px] text-green-400/70 font-mono">
            {status?.preview}
          </span>
        )}
      </div>
      <div className="flex gap-2">
        <input
          className="flex-1 bg-surface border border-border rounded-xl px-3 py-2 text-[13px] outline-none focus:border-accent min-w-0"
          type="password"
          placeholder={isSet ? "•••••••• (enter new to replace)" : provider.placeholder}
          value={value}
          onChange={(e) => onValueChange(e.target.value)}
          autoCapitalize="none"
          autoCorrect="off"
        />
        <a
          href={provider.keyUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="px-3 py-2 rounded-xl border border-border text-[12px] text-muted whitespace-nowrap hover:border-accent"
        >
          Get key
        </a>
        <button
          onClick={onSave}
          disabled={saving || !value.trim()}
          className="px-3 py-2 rounded-xl bg-accent text-white text-[12px] disabled:opacity-40"
        >
          {saving ? "…" : isSet ? "Update" : "Set"}
        </button>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
    </div>
  );
}
