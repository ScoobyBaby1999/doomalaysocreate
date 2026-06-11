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

  const field = "w-full bg-surface border border-border rounded-xl px-3 py-2 text-[15px] outline-none focus:border-accent";
  return (
    <div className="p-4 space-y-5 max-w-xl mx-auto">
      <div>
        <h2 className="text-lg font-semibold mb-1">Connection</h2>
        <p className="text-sm text-muted">Point the app at your panel space and paste a token.</p>
      </div>
      <label className="block space-y-1">
        <span className="text-sm text-muted">Gateway URL</span>
        <input
          className={field}
          value={draft.baseUrl}
          onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
          placeholder="https://<your-space>.hf.space"
          autoCapitalize="none"
          autoCorrect="off"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-sm text-muted">Bearer token</span>
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
          Stored only on this device. Prefer a short-lived token from <code>tools/gen_token.py</code>.
        </span>
      </label>
      <button onClick={test} className="px-4 py-2 rounded-xl bg-accent text-white font-medium">
        Save & test
      </button>
      {status && <p className="text-sm">{status}</p>}
      {frontier && <p className="text-sm text-muted">{frontier}</p>}
    </div>
  );
}
