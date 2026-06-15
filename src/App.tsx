import { useEffect, useState } from "react";
import { useSettings } from "./state/settings";
import { Chat } from "./screens/Chat";
import { SettingsScreen } from "./screens/SettingsScreen";
import { OnboardingScreen } from "./screens/OnboardingScreen";
import { AgentScreen } from "./screens/AgentScreen";
import { WorkspaceScreen } from "./screens/WorkspaceScreen";
import { exchangeGitHubCode, exchangeHFCode } from "./api/github";
import type { Settings } from "./api/panel";

type Tab = "chat" | "agent" | "workspaces" | "settings";

export default function App() {
  const [settings, setSettings] = useSettings();
  const hasCredentials = !!(settings.token || settings.rotationSecret);
  const [manualSetup, setManualSetup] = useState(false);
  const [tab, setTab] = useState<Tab>(hasCredentials ? "chat" : "settings");

  // Handle OAuth callback hashes
  // GitHub: #github-connected=<id> | #github-code=<code>&state=<state> (proxy) | #github-error=...
  // HF:     #hf-connected=<id>     | #hf-code=<code>&state=<state> (proxy)     | #hf-error=...
  // Chain:  GitHub result.next="hf" triggers automatic HF OAuth redirect
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    const params = new URLSearchParams(hash);
    const githubId = params.get("github-connected");
    const githubCode = params.get("github-code");
    const githubError = params.get("github-error");
    const stateParam = params.get("state");
    const hfId = params.get("hf-connected");
    const hfCode = params.get("hf-code");
    const hfError = params.get("hf-error");

    // Clear hash immediately
    history.replaceState(null, "", window.location.pathname + window.location.search);

    if (githubId) {
      // Direct flow (main Space): session ID returned directly
      setSettings({ ...settings, githubSessionId: githubId });
      setTab("workspaces");
    } else if (githubCode && stateParam) {
      // Proxy flow: exchange code for token via this Space's backend
      exchangeGitHubCode(githubCode, stateParam, settings.baseUrl).then((result) => {
        if (result.session_id) {
          setSettings({ ...settings, githubSessionId: result.session_id });
          // Chain HF OAuth if needed
          if (result.next === "hf") {
            const MAIN_SPACE = "https://scoobybaby1999-loom.hf.space";
            const thisSpace = window.location.origin;
            window.location.href = `${MAIN_SPACE}/api/auth/hf/login?redirect_to=${encodeURIComponent(thisSpace)}`;
          } else {
            setTab("workspaces");
          }
        }
      }).catch((e) => {
        console.error("GitHub code exchange failed:", e);
      });
    } else if (githubError) {
      console.error("GitHub OAuth error:", githubError);
    } else if (hfId) {
      // Direct HF flow: session ID returned directly
      setSettings({ ...settings, githubSessionId: hfId });
      setTab("workspaces");
    } else if (hfCode && stateParam) {
      // Proxy HF flow: exchange code
      exchangeHFCode(hfCode, stateParam, settings.baseUrl).then((result) => {
        if (result.session_id) {
          setSettings({ ...settings, githubSessionId: result.session_id });
          setTab("workspaces");
        }
      }).catch((e) => {
        console.error("HF code exchange failed:", e);
      });
    } else if (hfError) {
      console.error("HF OAuth error:", hfError);
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
