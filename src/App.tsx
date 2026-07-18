import { useEffect, useState } from "react";
import { useSettings, saveSettings, loadSettings } from "./state/settings";
import { AgentChat } from "./screens/AgentChat";
import { SettingsScreen } from "./screens/SettingsScreen";
import { OnboardingScreen } from "./screens/OnboardingScreen";
import { WorkspaceScreen } from "./screens/WorkspaceScreen";
// ConsciousScreen + MemoryScreen are no longer in the bottom nav (WORKSPACES-OVERHAUL):
//  - Minds opens as a per-workspace panel (WorkspaceMindsPanel) when a workspace card is clicked.
//  - Memory opens as a per-workspace panel (WorkspaceMemoryPanel) from the brain icon on a card.
// Both source files are still imported here so the bundler keeps them available for the panels.
import { ConsciousScreen } from "./screens/ConsciousScreen";
import { MemoryScreen } from "./screens/MemoryScreen";
import { DebugScreen } from "./screens/DebugScreen";
import { BenchmarksScreen } from "./screens/BenchmarksScreen";
import { ModelSelectOverlay } from "./components/ModelSelectOverlay";
import { ProvidersDialog } from "./components/ProvidersDialog";
import { useModelStore } from "./lib/model-store";

import { getJWTSub } from "./lib/jwt";
import { deriveToken } from "./api/token";
import type { Settings } from "./api/panel";

// NOTE: "providers" tab removed — ProvidersScreen is now embedded in
// SettingsScreen's "Providers" tab.
//
// WORKSPACES-OVERHAUL: "conscious" (Mind) and "memory" tabs removed from
// the bottom nav. Minds + Memory now live as per-workspace panels opened
// from workspace cards. The Tab union still includes them so they can be
// reached programmatically (e.g. WorkspaceMindsPanel wraps ConsciousScreen).
type Tab = "agentchat" | "conscious" | "workspaces" | "memory" | "benchmarks" | "settings" | "debug";

// Bottom-nav tabs — minimal, icon-focused: Chat · Workspaces · Models · Settings.
const NAV_TABS: Tab[] = ["agentchat", "workspaces", "benchmarks", "settings"];

export default function App() {
  const [settings, setSettings] = useSettings();
  const hasCredentials = !!(settings.token || settings.rotationSecret || settings.githubSessionId);
  const [manualSetup, setManualSetup] = useState(false);
  const [tab, setTab] = useState<Tab>(hasCredentials ? "agentchat" : "settings");

  const setBaseUrl = useModelStore((s) => s.setBaseUrl);
  const fetchProviders = useModelStore((s) => s.fetchProviders);
  const openOverlay = useModelStore((s) => s.openOverlay);
  const selectedModelId = useModelStore((s) => s.selectedModelId);
  const selectedProviderName = useModelStore((s) => s.selectedProviderName);
  const providers = useModelStore((s) => s.providers);
  const selectedProviderColor = (() => {
    if (!selectedProviderName) return "#a855f7";
    const p = providers.find((g) => g.name === selectedProviderName);
    return p?.color || "#a855f7";
  })();

  useEffect(() => {
    setBaseUrl(settings.baseUrl);
    fetchProviders();
  }, [settings.baseUrl]);

  // Handle OAuth callback hashes
  // GitHub: #github-connected=<id> | #github-grant=<token> (proxy) | #github-error=...
  // HF:     #hf-connected=<id>     | #hf-grant=<token> (proxy)     | #hf-error=...
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
    const githubGrant = params.get("github-grant");
    const githubError = params.get("github-error");
    const hfId = params.get("hf-connected");
    const hfGrant = params.get("hf-grant");
    const hfError = params.get("hf-error");

    // Clear hash immediately
    history.replaceState(null, "", window.location.pathname + window.location.search);

    if (githubId) {
      // Direct flow (main Space): session ID returned directly
      const ghSettings = { ...settings, githubSessionId: githubId };
      saveSettings(ghSettings);
      setSettings(ghSettings);
      setTab("workspaces");
    } else if (githubGrant) {
      // Proxy flow: claim identity grant from this Space's backend
      fetch(`${settings.baseUrl}/api/auth/github/claim-grant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: githubGrant }),
      }).then(r => r.json()).then((result) => {
        if (result.session_id) {
          const newSettings = { ...settings, githubSessionId: result.session_id };
          setSettings(newSettings);
          if (result.next === "hf") {
            saveSettings(newSettings);
            const MAIN_SPACE = import.meta.env.VITE_MAIN_SPACE || "";
            const thisSpace = window.location.origin;
            if (!MAIN_SPACE) {
              console.error("VITE_MAIN_SPACE not configured - cannot complete HF OAuth proxy flow");
              return;
            }
            const userId = getJWTSub(result.session_id) || result.session_id;
            window.location.href = `${MAIN_SPACE}/api/auth/hf/login?redirect_to=${encodeURIComponent(thisSpace)}&github_user_id=${encodeURIComponent(userId)}`;
          } else {
            saveSettings(newSettings);
            setTab("workspaces");
          }
        }
      }).catch((e) => {
        console.error("GitHub grant claim failed:", e);
      });
    } else if (githubError) {
      console.error("GitHub OAuth error:", githubError);
    } else if (hfId) {
      // Direct HF flow: session ID returned directly
      const hfSettings = { ...settings, githubSessionId: hfId };
      saveSettings(hfSettings);
      setSettings(hfSettings);
      setTab("workspaces");
    } else if (hfGrant) {
      // Proxy HF flow: claim HF identity grant from this Space's backend
      fetch(`${settings.baseUrl}/api/auth/hf/claim-grant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: hfGrant }),
      }).then(r => r.json()).then((result) => {
        if (result.session_id) {
          const newSettings = { ...settings, githubSessionId: result.session_id };
          saveSettings(newSettings);
          setSettings(newSettings);
          setTab("workspaces");
        }
      }).catch((e) => {
        console.error("HF grant claim failed:", e);
      });
    } else if (hfError) {
      console.error("HF OAuth error:", hfError);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const reportFrontendError = async (message: string, stack: any, severity: string = "ERROR") => {
      if (!settings.baseUrl) return;
      try {
        const token = settings.rotationSecret 
          ? await deriveToken(settings.rotationSecret) 
          : settings.token;
        if (!token) return;

        await fetch(`${settings.baseUrl}/api/debug/log`, {
          method: "POST",
          headers: { 
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`
          },
          body: JSON.stringify({
            level: severity,
            cat: "frontend",
            fn: "global_error_listener",
            msg: message,
            data: stack,
          }),
        });
      } catch (err) {
        console.error("Telemetry failed to push log:", err);
      }
    };

    const onErrorHandler = (e: ErrorEvent) => {
      reportFrontendError(`Browser Unhandled Exception: ${e.message}`, {
        filename: e.filename,
        line: e.lineno,
        column: e.colno,
        message: e.message,
        stack: e.error?.stack || ""
      });
    };

    const onUnhandledRejectionHandler = (e: PromiseRejectionEvent) => {
      reportFrontendError(`Browser Unhandled Rejection: ${e.reason}`, {
        reason: e.reason instanceof Error ? e.reason.message : String(e.reason),
        stack: e.reason instanceof Error ? e.reason.stack : ""
      });
    };

    window.addEventListener("error", onErrorHandler);
    window.addEventListener("unhandledrejection", onUnhandledRejectionHandler);
    
    return () => {
      window.removeEventListener("error", onErrorHandler);
      window.removeEventListener("unhandledrejection", onUnhandledRejectionHandler);
    };
  }, [settings]);

  if (!hasCredentials && !manualSetup) {
    return (
      <div className="flex flex-col h-full">
        <header className="flex items-center px-4 h-12 border-b border-border pt-[env(safe-area-inset-top)] box-content bg-surface/60 backdrop-blur">
          <span className="font-semibold tracking-tight text-foreground">doomalaysocreate</span>
          <span className="ml-2 text-[11px] text-muted">panel · agentic coder</span>
        </header>
        <main className="flex-1 min-h-0">
          <OnboardingScreen
            onComplete={(partial: Partial<Settings>) => {
              if (partial.rotationSecret || partial.token) {
                setSettings({ ...settings, ...partial });
                setTab("agentchat");
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
       <header className="flex items-center gap-2 px-4 h-12 border-b border-border pt-[env(safe-area-inset-top)] box-content bg-surface/60 backdrop-blur">
         <span className="font-semibold tracking-tight text-foreground">doomalaysocreate</span>
         <span className="text-[11px] text-muted hidden sm:inline">panel · agentic coder</span>
         <button
           onClick={openOverlay}
           className="touch-target ml-auto flex items-center gap-1.5 px-3 h-9 rounded-xl border border-border hover:border-accent/60 transition-colors text-[12px] bg-surface2/60"
           title="Select provider model"
           aria-label="Select provider model"
         >
           <span className="w-2.5 h-2.5 rounded-full ring-2 ring-background" style={{ background: selectedProviderColor || "#a855f7" }} />
           <span className="text-muted max-w-[80px] sm:max-w-[160px] truncate">{selectedModelId || selectedProviderName || "Model"}</span>
         </button>
       </header>

      <main className="flex-1 min-h-0">
        {tab === "agentchat" ? (
          <AgentChat settings={settings} />
        ) : tab === "conscious" ? (
          // Reachable programmatically (WorkspaceMindsPanel); not in the nav.
          <ConsciousScreen settings={settings} />
        ) : tab === "workspaces" ? (
          <WorkspaceScreen settings={settings} onChange={setSettings} />
        ) : tab === "memory" ? (
          // Reachable programmatically; not in the nav.
          <MemoryScreen settings={settings} />
        ) : tab === "benchmarks" ? (
          <BenchmarksScreen settings={settings} />
        ) : tab === "debug" ? (
          <DebugScreen settings={settings} />
        ) : (
          <SettingsScreen settings={settings} onChange={setSettings} onOpenTab={(t) => setTab(t as Tab)} />
        )}
      </main>

      {/* Minimal icon-focused bottom nav: Chat · Workspaces · Models · Settings. */}
      <nav className="flex border-t border-border">
        {NAV_TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            aria-label={t}
            aria-current={tab === t ? "page" : undefined}
            className={`flex-1 min-h-[56px] py-2 flex items-center justify-center transition-colors ${
              tab === t ? "text-accent" : "text-muted hover:text-foreground"
            }`}
          >
            {t === "agentchat" ? (
              /* Chat — icon ONLY (no text label) per WORKSPACES-OVERHAUL Task 5. */
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
            ) : t === "workspaces" ? (
              /* Workspaces — build/creative hammer icon. */
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
              </svg>
            ) : t === "benchmarks" ? (
              /* Models — chart icon. */
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 3v18h18"/>
                <path d="M7 14l4-4 4 4 5-5"/>
              </svg>
            ) : (
              /* Settings — gear icon. */
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="3"/>
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
              </svg>
            )}
          </button>
        ))}
      </nav>
      <ModelSelectOverlay />
      <ProvidersDialog />
    </div>
  );
}
