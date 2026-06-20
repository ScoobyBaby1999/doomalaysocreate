import { useEffect, useState } from "react";
import { useSettings, saveSettings, loadSettings } from "./state/settings";
import { Chat } from "./screens/Chat";
import { SettingsScreen } from "./screens/SettingsScreen";
import { OnboardingScreen } from "./screens/OnboardingScreen";
import { AgentScreen } from "./screens/AgentScreen";
import { WorkspaceScreen } from "./screens/WorkspaceScreen";
import { ConsciousScreen } from "./screens/ConsciousScreen";
import { DebugScreen } from "./screens/DebugScreen";
import { exchangeGitHubCode, exchangeHFCode } from "./api/github";
import { getJWTSub } from "./lib/jwt";
import type { Settings } from "./api/panel";

type Tab = "chat" | "agent" | "conscious" | "workspaces" | "settings" | "debug";

export default function App() {
  const [settings, setSettings] = useSettings();
  const hasCredentials = !!(settings.token || settings.rotationSecret || settings.githubSessionId);
  const [manualSetup, setManualSetup] = useState(false);
  const [tab, setTab] = useState<Tab>(hasCredentials ? "chat" : "settings");

  // Handle OAuth callback hashes
  // GitHub: #github-connected=<id> | #github-code=<code>&state=<state> (proxy) | #github-error=...
  // HF:     #hf-connected=<id>     | #hf-code=<code>&state=<state> (proxy)     | #hf-error=...
  // Chain:  GitHub result.next="hf" triggers automatic HF OAuth redirect
  useEffect(() => {
    // Re-read localStorage on bfcache restore (back/forward nav) so React
    // state matches what saveSettings() wrote before navigation.
    const onPageShow = (e: PageTransitionEvent) => {
      if (e.persisted) {
        setSettings(loadSettings());
      }
    };
    window.addEventListener("pageshow", onPageShow);

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
      const ghSettings = { ...settings, githubSessionId: githubId };
      saveSettings(ghSettings);
      setSettings(ghSettings);
      setTab("workspaces");
    } else if (githubCode && stateParam) {
      // Proxy flow: exchange code for token via this Space's backend
      exchangeGitHubCode(githubCode, stateParam, settings.baseUrl).then((result) => {
        if (result.session_id) {
          const newSettings = { ...settings, githubSessionId: result.session_id };
          setSettings(newSettings);
          if (result.next === "hf") {
            // Persist to localStorage BEFORE navigation — React's useEffect
            // may not flush before window.location.href takes effect.
            saveSettings(newSettings);
            const MAIN_SPACE = "https://scoobybaby1999-loom.hf.space";
            const thisSpace = window.location.origin;
            const userId = getJWTSub(result.session_id) || result.session_id;
            window.location.href = `${MAIN_SPACE}/api/auth/hf/login?redirect_to=${encodeURIComponent(thisSpace)}&github_user_id=${encodeURIComponent(userId)}`;
          } else {
            saveSettings(newSettings);
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
      const hfSettings = { ...settings, githubSessionId: hfId };
      saveSettings(hfSettings);
      setSettings(hfSettings);
      setTab("workspaces");
    } else if (hfCode && stateParam) {
      // Proxy HF flow: exchange code
      exchangeHFCode(hfCode, stateParam, settings.baseUrl).then((result) => {
        if (result.session_id) {
          const newSettings = { ...settings, githubSessionId: result.session_id };
          saveSettings(newSettings);
          setSettings(newSettings);
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
        ) : tab === "conscious" ? (
          <ConsciousScreen settings={settings} />
        ) : tab === "workspaces" ? (
          <WorkspaceScreen settings={settings} onChange={setSettings} />
        ) : tab === "debug" ? (
          <DebugScreen settings={settings} />
        ) : (
          <SettingsScreen settings={settings} onChange={setSettings} />
        )}
      </main>

      <nav className="flex border-t border-border overflow-x-auto">
        {(["chat", "agent", "conscious", "workspaces", "settings", "debug"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 text-sm capitalize flex flex-col items-center gap-0.5 min-w-[55px] ${
              tab === t ? "text-accent" : "text-muted"
            }`}
          >
            {t === "conscious" ? (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
                  <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
                </svg>
                <span className="text-[10px]">Mind</span>
              </>
            ) : t === "debug" ? (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/>
                  <path d="M12 16v-4M12 8h.01"/>
                </svg>
                <span className="text-[10px]">Debug</span>
              </>
            ) : (
              t
            )}
          </button>
        ))}
      </nav>
    </div>
  );
}
