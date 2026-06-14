import { useEffect, useState } from "react";
import { useSettings } from "./state/settings";
import { Chat } from "./screens/Chat";
import { SettingsScreen } from "./screens/SettingsScreen";
import { OnboardingScreen } from "./screens/OnboardingScreen";
import { AgentScreen } from "./screens/AgentScreen";
import { WorkspaceScreen } from "./screens/WorkspaceScreen";
import type { Settings } from "./api/panel";

type Tab = "chat" | "agent" | "workspaces" | "settings";

export default function App() {
  const [settings, setSettings] = useSettings();
  const hasCredentials = !!(settings.token || settings.rotationSecret);
  const [manualSetup, setManualSetup] = useState(false);
  const [tab, setTab] = useState<Tab>(hasCredentials ? "chat" : "settings");

  // Handle GitHub OAuth callback hash (#github-connected=<id>&state=<state> or #github-error=...)
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    const params = new URLSearchParams(hash);
    const githubId = params.get("github-connected");
    const githubError = params.get("github-error");
    const state = params.get("state");

    // Clear hash immediately
    history.replaceState(null, "", window.location.pathname + window.location.search);

    if (githubId) {
      // Verify OAuth state parameter to prevent session fixation
      const expectedState = sessionStorage.getItem("github_oauth_state");
      sessionStorage.removeItem("github_oauth_state");
      if (!expectedState || state !== expectedState) {
        console.error("GitHub OAuth state mismatch — possible CSRF attack");
        return;
      }
      setSettings({ ...settings, githubSessionId: githubId });
      setTab("workspaces");
    } else if (githubError) {
      console.error("GitHub OAuth error:", githubError);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!hasCredentials && !manualSetup) {
    return (
      <div className="flex flex-col h-full">
        <header className="flex items-center px-4 h-12 border-b border-border pt-[env(safe-area-inset-top)] box-content">
          <span className="font-semibold tracking-tight">loom</span>
          <span className="ml-2 text-[11px] text-muted">panel · agentic coder</span>
        </header>
        <main className="flex-1 min-h-0">
          <OnboardingScreen
            onComplete={(partial: Partial<Settings>) => {
              if (partial.rotationSecret || partial.token) {
                setSettings({ ...settings, ...partial });
                setTab("chat");
              } else {
                setManualSetup(true);
                setTab("settings");
              }
            }}
          />
        </main>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center px-4 h-12 border-b border-border pt-[env(safe-area-inset-top)] box-content">
        <span className="font-semibold tracking-tight">loom</span>
        <span className="ml-2 text-[11px] text-muted">panel · agentic coder</span>
      </header>

      <main className="flex-1 min-h-0">
        {tab === "chat" ? (
          <Chat settings={settings} />
        ) : tab === "agent" ? (
          <AgentScreen settings={settings} />
        ) : tab === "workspaces" ? (
          <WorkspaceScreen settings={settings} onChange={setSettings} />
        ) : (
          <SettingsScreen settings={settings} onChange={setSettings} />
        )}
      </main>

      <nav className="flex border-t border-border">
        {(["chat", "agent", "workspaces", "settings"] as Tab[]).map((t) => (
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
