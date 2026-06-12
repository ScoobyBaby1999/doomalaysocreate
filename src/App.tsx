import { useState } from "react";
import { useSettings } from "./state/settings";
import { Chat } from "./screens/Chat";
import { SettingsScreen } from "./screens/SettingsScreen";

type Tab = "chat" | "settings";

// F0 shell: mobile-first, full-height, bottom tab bar (thumb-reachable). The agentic
// coder + sandbox surfaces (F2) become additional tabs/panes later.
export default function App() {
  const [settings, setSettings] = useSettings();
  const [tab, setTab] = useState<Tab>(settings.token || settings.rotationSecret ? "chat" : "settings");

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center px-4 h-12 border-b border-border pt-[env(safe-area-inset-top)] box-content">
        <span className="font-semibold tracking-tight">loom</span>
        <span className="ml-2 text-[11px] text-muted">panel · agentic coder</span>
      </header>

      <main className="flex-1 min-h-0">
        {tab === "chat" ? (
          <Chat settings={settings} />
        ) : (
          <SettingsScreen settings={settings} onChange={setSettings} />
        )}
      </main>

      <nav className="flex border-t border-border">
        {(["chat", "settings"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 text-sm capitalize ${
              tab === t ? "text-accent" : "text-muted"
            }`}
          >
            {t}
          </button>
        ))}
      </nav>
    </div>
  );
}
