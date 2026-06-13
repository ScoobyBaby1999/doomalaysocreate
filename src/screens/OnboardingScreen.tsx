import { useEffect, useState } from "react";
import type { Settings } from "../api/panel";
import { ProviderKeyWizard } from "../components/ProviderKeyWizard";

type Phase =
  | "landing"        // no credentials, show sign-in button
  | "exchanging"     // fetching /oauth/result/<token>
  | "wizard"         // provision done, adding provider keys
  | "waiting"        // cross-origin Space still building; polling its /health
  | "error";         // something went wrong

interface ProvisionResult {
  space_url: string;
  space_repo: string;
  rotation_secret: string;
  username: string;
  oauth_token: string;
  existing?: boolean;
}

// Map raw error codes from the OAuth callback redirect to messages a human
// can act on. Every error path must leave the user a way forward.
const ERROR_MESSAGES: Record<string, string> = {
  invalid_state: "The sign-in link expired (they're valid for 10 minutes). Please sign in again.",
  token_exchange_failed: "Hugging Face didn't accept the sign-in. This is usually temporary — try again.",
  whoami_failed: "Signed in, but we couldn't read your username from Hugging Face. Try again.",
  duplicate_failed: "We couldn't create your Space. Check that your HF account is verified, then retry.",
  set_secret_failed: "Your Space exists, but we couldn't store its access secret. Sign in again to retry — or configure manually.",
  access_denied: "You cancelled the sign-in. No problem — try again whenever you're ready.",
};

export function OnboardingScreen({ onComplete }: { onComplete: (s: Partial<Settings>) => void }) {
  const [phase, setPhase] = useState<Phase>("landing");
  const [provision, setProvision] = useState<ProvisionResult | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [errorDetail, setErrorDetail] = useState("");

  // On mount: check URL hash for OAuth callback results
  useEffect(() => {
    const hash = window.location.hash.slice(1); // strip leading #
    if (!hash) return;
    const params = new URLSearchParams(hash);

    const token = params.get("provision-token");
    const error = params.get("provision-error");
    const detail = params.get("provision-detail");
    const setup = params.get("setup");

    // Clear the hash immediately so it doesn't persist in browser history
    history.replaceState(null, "", window.location.pathname + window.location.search);

    if (error) {
      setErrorMsg(ERROR_MESSAGES[error] ?? `Unexpected error: ${error.replace(/_/g, " ")}`);
      if (detail) setErrorDetail(decodeURIComponent(detail));
      setPhase("error");
      return;
    }

    if (token) {
      setPhase("exchanging");
      fetch(`/oauth/result/${token}`)
        .then((r) => {
          if (!r.ok) throw new Error(r.status === 404
            ? "This sign-in link was already used or expired. Please sign in again."
            : `HTTP ${r.status}`);
          return r.json() as Promise<ProvisionResult>;
        })
        .then((result) => {
          setProvision(result);
          setPhase("wizard");
        })
        .catch((e) => {
          setErrorMsg((e as Error).message);
          setPhase("error");
        });
      return;
    }

    // #setup=<base64json> — written by the gateway when redirecting to this
    // (the user's own) Space. Saves the rotation secret and enters the app.
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
    return <CenteredMessage>Linking your Space…</CenteredMessage>;
  }

  if (phase === "waiting") {
    return (
      <CenteredMessage>
        Your Space is building — this usually takes 1–3 minutes. We'll take
        you there automatically.
      </CenteredMessage>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex flex-col items-center justify-center h-full p-6 space-y-5 text-center">
        <div className="space-y-2 max-w-sm">
          <p className="text-sm font-medium">Sign-in didn't finish</p>
          <p className="text-sm text-muted">{errorMsg}</p>
          {errorDetail && (
            <p className="text-[10px] text-muted/70 break-all">({errorDetail})</p>
          )}
        </div>
        <a
          href="/oauth/login"
          className="flex items-center gap-2 px-5 py-3 rounded-xl bg-accent text-white font-medium text-sm"
        >
          <HFIcon />
          Try signing in again
        </a>
        <button onClick={() => onComplete({})} className="text-xs text-accent underline">
          Configure manually instead
        </button>
      </div>
    );
  }

  if (phase === "wizard" && provision) {
    return (
      <ProviderKeyWizard
        provision={provision}
        onDone={async () => {
          let sameOrigin = false;
          try {
            sameOrigin = new URL(provision.space_url).origin === window.location.origin;
          } catch {
            /* malformed URL — fall through to redirect attempt */
          }
          if (sameOrigin) {
            // Navigating to the same URL with only a hash change does NOT
            // reload the page — hand the secret to the app directly instead.
            onComplete({ rotationSecret: provision.rotation_secret });
            return;
          }
          // Cross-origin: the user's Space may still be building. Poll its
          // /health (CORS errors / non-200 = not ready) before redirecting,
          // so they land on a working app instead of HF's build page.
          setPhase("waiting");
          const deadline = Date.now() + 5 * 60_000;
          while (Date.now() < deadline) {
            try {
              const r = await fetch(`${provision.space_url}/health`, { cache: "no-store" });
              if (r.ok) break;
            } catch {
              /* still building — keep waiting */
            }
            await new Promise((res) => setTimeout(res, 5000));
          }
          // On timeout we redirect anyway; the Space landing page finishes the job.
          const setup = btoa(JSON.stringify({ rotationSecret: provision.rotation_secret }));
          window.location.href = `${provision.space_url}/#setup=${setup}`;
        }}
      />
    );
  }

  // Landing — this IS the front door, for new and returning users alike.
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
        New here? We'll create a free private Space for you automatically.
        Already have one? The same button re-links it — your keys and data stay put.
      </p>

      <div className="pt-4 border-t border-border w-full max-w-xs">
        <p className="text-[11px] text-muted mb-2">Prefer to paste credentials yourself?</p>
        <button onClick={() => onComplete({})} className="text-xs text-accent underline">
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
