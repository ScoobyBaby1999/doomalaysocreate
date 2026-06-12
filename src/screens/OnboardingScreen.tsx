import { useEffect, useState } from "react";
import type { Settings } from "../api/panel";
import { ProviderKeyWizard } from "../components/ProviderKeyWizard";

type Phase =
  | "landing"        // no credentials, show sign-in button
  | "exchanging"     // fetching /oauth/result/<token>
  | "wizard"         // provision done, adding provider keys
  | "building"       // waiting for the user's Space to go live
  | "error";         // something went wrong

interface ProvisionResult {
  space_url: string;
  space_repo: string;
  rotation_secret: string;
  username: string;
  oauth_token: string;
}

export function OnboardingScreen({ onComplete }: { onComplete: (s: Partial<Settings>) => void }) {
  const [phase, setPhase] = useState<Phase>("landing");
  const [provision, setProvision] = useState<ProvisionResult | null>(null);
  const [errorMsg, setErrorMsg] = useState("");

  // On mount: check URL hash for OAuth callback results
  useEffect(() => {
    const hash = window.location.hash.slice(1); // strip leading #
    if (!hash) return;
    const params = new URLSearchParams(hash);

    const token = params.get("provision-token");
    const error = params.get("provision-error");

    // Clear the hash immediately so it doesn't persist in browser history
    history.replaceState(null, "", window.location.pathname + window.location.search);

    if (error) {
      setErrorMsg(decodeURIComponent(error).replace(/_/g, " "));
      setPhase("error");
      return;
    }
    if (token) {
      setPhase("exchanging");
      fetch(`/oauth/result/${token}`)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json() as Promise<ProvisionResult>;
        })
        .then((result) => {
          setProvision(result);
          setPhase("wizard");
        })
        .catch((e) => {
          setErrorMsg(`Provision token exchange failed: ${e.message}`);
          setPhase("error");
        });
    }

    // Handle #setup=<base64json> — written by the original Space when redirecting here
    const setup = params.get("setup");
    if (setup) {
      try {
        const decoded = JSON.parse(atob(setup));
        if (decoded.rotationSecret) {
          onComplete({ rotationSecret: decoded.rotationSecret });
        }
      } catch {
        // malformed; ignore and show landing
      }
    }
  }, []);

  if (phase === "exchanging") {
    return <CenteredMessage>Setting up your Space…</CenteredMessage>;
  }

  if (phase === "error") {
    return (
      <div className="p-6 space-y-4 max-w-sm mx-auto text-center">
        <p className="text-red-400 text-sm">Setup failed: {errorMsg}</p>
        <button
          onClick={() => setPhase("landing")}
          className="px-4 py-2 rounded-xl bg-surface border border-border text-sm"
        >
          Try again
        </button>
      </div>
    );
  }

  if (phase === "wizard" && provision) {
    return (
      <ProviderKeyWizard
        provision={provision}
        onDone={() => {
          // Build the #setup hash and redirect to the user's Space
          const setup = btoa(JSON.stringify({ rotationSecret: provision.rotation_secret }));
          window.location.href = `${provision.space_url}/#setup=${setup}`;
        }}
      />
    );
  }

  if (phase === "building") {
    return <CenteredMessage>Your Space is building — this takes about 2 minutes.</CenteredMessage>;
  }

  // Landing
  return (
    <div className="flex flex-col items-center justify-center h-full p-6 space-y-6 text-center">
      <div className="space-y-2">
        <h1 className="text-2xl font-bold tracking-tight">loom</h1>
        <p className="text-sm text-muted max-w-xs">
          Multi-model judge panel in your pocket. Sign in to get your own
          private Space — your keys, your metrics, your URL.
        </p>
      </div>

      <a
        href="/oauth/login"
        className="flex items-center gap-2 px-5 py-3 rounded-xl bg-accent text-white font-medium text-sm"
      >
        <HFIcon />
        Sign in with Hugging Face
      </a>

      <p className="text-[11px] text-muted max-w-xs">
        We'll create a free private Space for you and set up your credentials
        automatically. You keep full ownership — we never see your keys.
      </p>

      <div className="pt-4 border-t border-border w-full max-w-xs">
        <p className="text-[11px] text-muted mb-2">Already have a Space?</p>
        <button
          onClick={() => onComplete({})}
          className="text-xs text-accent underline"
        >
          Configure manually
        </button>
      </div>
    </div>
  );
}

function CenteredMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-center h-full p-6">
      <p className="text-sm text-muted">{children}</p>
    </div>
  );
}

function HFIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 95 88" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
      <path d="M47.2 0C21.1 0 0 19.7 0 44c0 14.5 7.4 27.4 18.8 35.4L10.4 88h73.5l-8.4-8.6C86.9 71.4 94.3 58.5 94.3 44 94.3 19.7 73.2 0 47.2 0z"/>
      <path d="M47.2 8c-20.4 0-36.9 16.1-36.9 36 0 8.9 3.2 17 8.5 23.3l-5.7 5.8h68.2l-5.7-5.8c5.3-6.3 8.5-14.4 8.5-23.3C84.1 24.1 67.6 8 47.2 8z" fill="white" opacity="0.15"/>
    </svg>
  );
}
