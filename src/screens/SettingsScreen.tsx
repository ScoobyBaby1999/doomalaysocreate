import { useEffect, useState } from "react";
import { PanelClient, type Settings } from "../api/panel";

/** Backend connection + assisted BYOK. The token is stored only on-device (localStorage,
 *  and in a Tauri build the OS keychain later). Validates against /health + /api/roster. */
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
    // Clear all stored credentials and reload
    localStorage.removeItem("doomalaysocreate.settings.v1");
    localStorage.removeItem("doomalaysocreate.settings.expiry");
    sessionStorage.clear();
    window.location.reload();
  }

  const field = "w-full bg-surface border border-border rounded-xl px-3 py-2 text-[15px] outline-none focus:border-accent transition-colors";
  return (
    <div className="p-4 space-y-5 max-w-xl mx-auto overflow-y-auto">
      <div>
        <h2 className="text-lg font-semibold mb-1">Connection</h2>
        <p className="text-sm text-muted">
          Point the app at your panel space and paste your rotation secret (or a token).
        </p>
      </div>

      {/* Connection fields */}
      <div className="space-y-3">
        <label className="block space-y-1">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Gateway URL</span>
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
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Rotation secret</span>
          <input
            className={field}
            type="password"
            value={draft.rotationSecret}
            onChange={(e) => setDraft({ ...draft, rotationSecret: e.target.value })}
            placeholder="CRITIQUE_ROTATION_SECRET"
            autoCapitalize="none"
            autoCorrect="off"
          />
          <span className="text-[11px] text-muted/70">
            Stored only on this device; the hourly wire token is derived from it per call.
          </span>
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Static token (fallback)</span>
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
        Save & test
      </button>

      {/* Status */}
      {status && (
        <div className={`text-sm px-3 py-2 rounded-lg ${status.startsWith("✗") ? "bg-rose-500/10 text-rose-300" : "bg-emerald-500/10 text-emerald-300"}`}>
          {status}
        </div>
      )}
      {frontier && (
        <div className="text-xs text-muted px-3 py-2 rounded-lg bg-surface/50 border border-border/50">
          {frontier}
        </div>
      )}

      {/* GitHub status */}
      {draft.githubSessionId && (
        <div className="px-3 py-2 rounded-lg bg-surface/50 border border-border/50">
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-1">GitHub</div>
          <div className="text-sm text-foreground flex items-center gap-2">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            Connected
            {draft.githubUsername && <span className="text-muted">as {draft.githubUsername}</span>}
          </div>
        </div>
      )}

      {/* Logout / Danger Zone */}
      <div className="border-t border-border pt-5 mt-5">
        <h3 className="text-sm font-medium text-rose-300 mb-2">Account</h3>
        <p className="text-[11px] text-muted mb-3">
          Sign out and clear all stored credentials from this device.
        </p>
        {confirmLogout ? (
          <div className="space-y-2">
            <p className="text-sm text-rose-300">Are you sure? This will reload the app.</p>
            <div className="flex gap-2">
              <button
                onClick={handleLogout}
                className="flex-1 py-1.5 rounded-lg bg-rose-600 text-white text-sm hover:bg-rose-700 transition-colors"
              >
                Yes, log out
              </button>
              <button
                onClick={() => setConfirmLogout(false)}
                className="flex-1 py-1.5 rounded-lg border border-border text-sm hover:bg-surface/40 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={handleLogout}
            className="w-full py-1.5 rounded-lg border border-rose-500/40 text-rose-300 text-sm hover:bg-rose-500/10 transition-colors"
          >
            Log out & clear credentials
          </button>
        )}
      </div>
    </div>
  );
}
