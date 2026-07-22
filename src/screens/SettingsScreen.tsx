import { useEffect, useState } from "react";
import { PanelClient, type Settings } from "../api/panel";
import { ProvidersScreen } from "./ProvidersScreen";

/**
 * Settings — top-level hub with Windows-start-bar-style tabs.
 *
 * Tabs (left→right):
 *   - General    → backend connection (gateway URL, rotation secret, token).
 *   - Providers  → embedded ProvidersScreen (free/paid split, keys, premium).
 *   - Appearance → theme/accent controls (placeholder for now).
 *
 * The tab bar is sticky at the top so the user can switch without scrolling.
 * The Providers tab carries the providers-logo icon (a stacked-server glyph)
 * per the spec.
 */

type SettingsTab = "general" | "providers" | "appearance";

const TAB_BAR_KEY = "doomalaysocreate.settings.tab";

function loadInitialTab(): SettingsTab {
  try {
    const t = localStorage.getItem(TAB_BAR_KEY);
    if (t === "general" || t === "providers" || t === "appearance") return t;
  } catch {
    /* ignore */
  }
  return "general";
}

function saveTab(t: SettingsTab) {
  try {
    localStorage.setItem(TAB_BAR_KEY, t);
  } catch {
    /* ignore */
  }
}

export function SettingsScreen({
  settings,
  onChange,
  onOpenTab,
}: {
  settings: Settings;
  onChange: (s: Settings) => void;
  /** Switch the App-level tab (e.g. open the Debug screen). Optional. */
  onOpenTab?: (tab: string) => void;
}) {
  const [tab, setTab] = useState<SettingsTab>(loadInitialTab);

  function switchTab(t: SettingsTab) {
    setTab(t);
    saveTab(t);
  }

  return (
    <div className="flex flex-col h-full max-w-2xl mx-auto w-full overflow-hidden">
      {/* Tab bar — Windows-start-bar-like strip at the top. */}
      <div className="flex items-stretch gap-1 px-2 pt-2 border-b border-border bg-surface/40 shrink-0 overflow-x-auto no-scrollbar">
        <TabButton
          active={tab === "general"}
          onClick={() => switchTab("general")}
          label="General"
        >
          {/* Sliders icon */}
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="4" x2="4" y1="21" y2="14" />
            <line x1="4" x2="4" y1="10" y2="3" />
            <line x1="12" x2="12" y1="21" y2="12" />
            <line x1="12" x2="12" y1="8" y2="3" />
            <line x1="20" x2="20" y1="21" y2="16" />
            <line x1="20" x2="20" y1="12" y2="3" />
            <line x1="2" x2="6" y1="14" y2="14" />
            <line x1="10" x2="14" y1="8" y2="8" />
            <line x1="18" x2="22" y1="16" y2="16" />
          </svg>
        </TabButton>
        <TabButton
          active={tab === "providers"}
          onClick={() => switchTab("providers")}
          label="Providers"
        >
          {/* Stacked-server / providers-logo icon */}
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="2" y="3" width="20" height="7" rx="2" />
            <rect x="2" y="14" width="20" height="7" rx="2" />
            <line x1="6" y1="6.5" x2="6.01" y2="6.5" />
            <line x1="6" y1="17.5" x2="6.01" y2="17.5" />
          </svg>
        </TabButton>
        <TabButton
          active={tab === "appearance"}
          onClick={() => switchTab("appearance")}
          label="Appearance"
        >
          {/* Palette icon */}
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="13.5" cy="6.5" r=".5" fill="currentColor" />
            <circle cx="17.5" cy="10.5" r=".5" fill="currentColor" />
            <circle cx="8.5" cy="7.5" r=".5" fill="currentColor" />
            <circle cx="6.5" cy="12.5" r=".5" fill="currentColor" />
            <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
          </svg>
        </TabButton>
      </div>

      {/* Tab content — fills remaining height. */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "general" ? (
          <GeneralTab settings={settings} onChange={onChange} onOpenTab={onOpenTab} />
        ) : tab === "providers" ? (
          <ProvidersScreen settings={settings} embedded />
        ) : (
          <AppearanceTab />
        )}
      </div>
    </div>
  );
}

// ── Tab button ───────────────────────────────────────────────────────────
function TabButton({
  active,
  onClick,
  label,
  children,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      className={`touch-target inline-flex items-center gap-1.5 px-3 h-10 rounded-t-xl text-[12.5px] font-medium transition-colors shrink-0 border-b-2 -mb-px ${
        active
          ? "text-accent border-accent bg-surface2/40"
          : "text-muted-foreground border-transparent hover:text-foreground hover:bg-surface2/30"
      }`}
    >
      {children}
      <span>{label}</span>
    </button>
  );
}

// ── General tab (the old SettingsScreen body) ────────────────────────────
function GeneralTab({
  settings,
  onChange,
  onOpenTab,
}: {
  settings: Settings;
  onChange: (s: Settings) => void;
  onOpenTab?: (tab: string) => void;
}) {
  const [draft, setDraft] = useState<Settings>(settings);
  const [status, setStatus] = useState<string>("");
  const [frontier, setFrontier] = useState<string>("");
  const [confirmLogout, setConfirmLogout] = useState(false);

  useEffect(() => setDraft(settings), [settings]);

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

  function handleLogout() {
    if (!confirmLogout) {
      setConfirmLogout(true);
      return;
    }
    localStorage.removeItem("doomalaysocreate.settings.v1");
    localStorage.removeItem("doomalaysocreate.settings.expiry");
    sessionStorage.clear();
    window.location.reload();
  }

  const field =
    "w-full bg-surface border border-border rounded-xl px-3 py-2 text-[15px] outline-none focus:border-accent transition-colors";

  return (
    <div className="h-full overflow-y-auto p-4 space-y-5">
      <div>
        <h2 className="text-lg font-semibold mb-1">Connection</h2>
        <p className="text-sm text-muted">
          Point the app at your panel space and paste your rotation secret
          (or a token).
        </p>
      </div>

      <div className="space-y-3">
        <label className="block space-y-1">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Gateway URL
          </span>
          <input
            className={field}
            value={draft.baseUrl}
            onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
            placeholder="empty = this Space"
            autoCapitalize="none"
            autoCorrect="off"
          />
          <span className="text-[11px] text-muted/70">
            Leave empty when the app is served by your Space itself.
          </span>
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Rotation secret
          </span>
          <input
            className={field}
            type="password"
            value={draft.rotationSecret}
            onChange={(e) =>
              setDraft({ ...draft, rotationSecret: e.target.value })
            }
            placeholder="CRITIQUE_ROTATION_SECRET"
            autoCapitalize="none"
            autoCorrect="off"
          />
          <span className="text-[11px] text-muted/70">
            Stored only on this device; the hourly wire token is derived from
            it per call.
          </span>
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            Static token (fallback)
          </span>
          <input
            className={field}
            type="password"
            value={draft.token}
            onChange={(e) => setDraft({ ...draft, token: e.target.value })}
            placeholder="CRITIQUE_TOKEN"
            autoCapitalize="none"
            autoCorrect="off"
          />
          <span className="text-[11px] text-muted/70">
            Only used when no rotation secret is set.
          </span>
        </label>
      </div>

      <button
        onClick={test}
        className="px-4 py-2 rounded-xl bg-accent text-white font-medium hover:bg-accent/80 transition-colors w-full"
      >
        Save &amp; test
      </button>

      {status && (
        <div
          className={`text-sm px-3 py-2 rounded-xl ${
            status.startsWith("✗")
              ? "bg-rose-500/10 text-rose-300"
              : "bg-emerald-500/10 text-emerald-300"
          }`}
        >
          {status}
        </div>
      )}
      {frontier && (
        <div className="text-xs text-muted px-3 py-2 rounded-xl bg-surface/50 border border-border/50">
          {frontier}
        </div>
      )}

      {draft.githubSessionId && (
        <div className="px-3 py-2 rounded-xl bg-surface/50 border border-border/50">
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-1">
            GitHub
          </div>
          <div className="text-sm text-foreground flex items-center gap-2">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            Connected
            {draft.githubUsername && (
              <span className="text-muted">as {draft.githubUsername}</span>
            )}
          </div>
        </div>
      )}

      <div className="border-t border-border pt-5 mt-5">
        <h3 className="text-sm font-medium text-rose-300 mb-2">Account</h3>
        <p className="text-[11px] text-muted mb-3">
          Sign out and clear all stored credentials from this device.
        </p>
        {confirmLogout ? (
          <div className="space-y-2">
            <p className="text-sm text-rose-300">
              Are you sure? This will reload the app.
            </p>
            <div className="flex gap-2">
              <button
                onClick={handleLogout}
                className="flex-1 py-1.5 rounded-xl bg-rose-600 text-white text-sm hover:bg-rose-700 transition-colors"
              >
                Yes, log out
              </button>
              <button
                onClick={() => setConfirmLogout(false)}
                className="flex-1 py-1.5 rounded-xl border border-border text-sm hover:bg-surface/40 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={handleLogout}
            className="w-full py-1.5 rounded-xl border border-rose-500/40 text-rose-300 text-sm hover:bg-rose-500/10 transition-colors"
          >
            Log out &amp; clear credentials
          </button>
        )}
      </div>

      {/* Developer tools — lets users reach the (now nav-hidden) Debug screen. */}
      {onOpenTab && (
        <div className="border-t border-border pt-5 mt-5">
          <h3 className="text-sm font-medium text-muted-foreground mb-2">Developer</h3>
          <button
            onClick={() => onOpenTab("debug")}
            className="w-full py-1.5 rounded-xl border border-border text-sm hover:border-accent/60 hover:text-accent transition-colors flex items-center justify-center gap-2"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/>
              <path d="M12 16v-4M12 8h.01"/>
            </svg>
            Open Debug Console
          </button>
        </div>
      )}
    </div>
  );
}

// ── Appearance tab (placeholder — theme/accent controls land here) ───────
function AppearanceTab() {
  return (
    <div className="h-full overflow-y-auto p-4 space-y-5">
      <div>
        <h2 className="text-lg font-semibold mb-1">Appearance</h2>
        <p className="text-sm text-muted">
          Theme + accent customization. Coming soon — the purple/black theme
          is currently hardcoded.
        </p>
      </div>
      <div className="rounded-xl border border-dashed border-border/60 bg-surface2/30 px-3 py-6 text-center">
        <span className="text-[12px] text-muted-foreground">
          🎨 Theme controls land here in a future iteration.
        </span>
      </div>
    </div>
  );
}
