import { useState } from "react";

interface ProvisionResult {
  space_url: string;
  space_repo: string;
  rotation_secret: string;
  username: string;
  oauth_token: string;
  existing?: boolean;
}

interface Provider {
  id: string;
  label: string;
  envKey: string;
  keyUrl: string;
  hint: string;
  placeholder: string;
}

const PROVIDERS: Provider[] = [
  {
    id: "puter",
    label: "Puter (GLM 5.2) — FREE",
    envKey: "PUTER_API_TOKEN",
    keyUrl: "https://puter.com/dashboard",
    hint: "Free GLM 5.2, no phone, no payment. Recommended.",
    placeholder: "eyJ…",
  },
  {
    id: "nvidia",
    label: "NVIDIA AI",
    envKey: "NVIDIA_API_KEY",
    keyUrl: "https://build.nvidia.com",
    hint: "Free credits — GLM-5.1, Kimi, Llama.",
    placeholder: "nvapi-…",
  },
  {
    id: "zai",
    label: "Z.ai (GLM 5.2)",
    envKey: "ZAI_API_KEY",
    keyUrl: "https://z.ai",
    hint: "Real GLM 5.2 — limited-time free input.",
    placeholder: "…",
  },
  {
    id: "moonshot",
    label: "Moonshot (Kimi)",
    envKey: "MOONSHOT_API_KEY",
    keyUrl: "https://platform.moonshot.cn/console/api-keys",
    hint: "Kimi K2 — agentic coding.",
    placeholder: "sk-…",
  },
  {
    id: "google",
    label: "Google AI Studio",
    envKey: "GEMINI_API_KEY",
    keyUrl: "https://aistudio.google.com/apikey",
    hint: "Gemini Flash — free tier.",
    placeholder: "AIza…",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    envKey: "OPENROUTER_API_KEY",
    keyUrl: "https://openrouter.ai/keys",
    hint: "100+ models; $1 free credit on signup.",
    placeholder: "sk-or-…",
  },
  {
    id: "anthropic",
    label: "Anthropic (Claude) — paid",
    envKey: "ANTHROPIC_API_KEY",
    keyUrl: "https://console.anthropic.com/settings/keys",
    hint: "Optional. Unlocks the full Claude agent tier (paid key).",
    placeholder: "sk-ant-…",
  },
];

export function ProviderKeyWizard({
  provision,
  onDone,
}: {
  provision: ProvisionResult;
  onDone: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [statuses, setStatuses] = useState<Record<string, "idle" | "saving" | "ok" | "err">>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const setKey = (id: string, val: string) =>
    setValues((v) => ({ ...v, [id]: val }));

  async function saveKey(provider: Provider) {
    const val = (values[provider.id] || "").trim();
    if (!val) return;
    setStatuses((s) => ({ ...s, [provider.id]: "saving" }));
    setErrors((e) => ({ ...e, [provider.id]: "" }));
    try {
      const res = await fetch("/oauth/set-provider-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          oauth_token: provision.oauth_token,
          repo: provision.space_repo,
          key_name: provider.envKey,
          key_value: val,
        }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error((j as { error?: string }).error || `HTTP ${res.status}`);
      }
      setStatuses((s) => ({ ...s, [provider.id]: "ok" }));
    } catch (e) {
      setStatuses((s) => ({ ...s, [provider.id]: "err" }));
      setErrors((er) => ({ ...er, [provider.id]: (e as Error).message }));
    }
  }

  const anyOk = Object.values(statuses).some((s) => s === "ok");
  // Returning users already configured their keys on a previous visit —
  // never block them behind a step they've completed before.
  const canContinue = anyOk || !!provision.existing;

  const field =
    "flex-1 bg-surface border border-border rounded-xl px-3 py-2 text-[13px] outline-none focus:border-accent min-w-0";

  return (
    <div className="p-4 space-y-5 max-w-xl mx-auto overflow-y-auto">
      <div>
        <h2 className="text-lg font-semibold">
          {provision.existing ? `Welcome back, ${provision.username}` : "Add a provider key"}
        </h2>
        <p className="text-sm text-muted mt-1">
          {provision.existing ? (
            <>
              Your Space at <span className="text-accent">{provision.space_url}</span>{" "}
              has been re-linked with a fresh secret (it restarts briefly). Your
              existing provider keys are untouched — add more below, or continue.
            </>
          ) : (
            <>
              Your Space is provisioned at{" "}
              <span className="text-accent">{provision.space_url}</span>. Add at
              least one free API key so the judges have models to call.
            </>
          )}
        </p>
      </div>

      {PROVIDERS.map((p) => {
        const st = statuses[p.id] ?? "idle";
        return (
          <div key={p.id} className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">{p.label}</span>
              <span className="text-[11px] text-muted">{p.hint}</span>
              {st === "ok" && <span className="ml-auto text-green-400 text-xs">saved ✓</span>}
            </div>
            <div className="flex gap-2">
              <input
                className={field}
                type="password"
                placeholder={p.placeholder}
                value={values[p.id] ?? ""}
                onChange={(e) => setKey(p.id, e.target.value)}
                autoCapitalize="none"
                autoCorrect="off"
                disabled={st === "ok"}
              />
              <a
                href={p.keyUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="px-3 py-2 rounded-xl border border-border text-[12px] text-muted whitespace-nowrap"
              >
                Get key
              </a>
              <button
                onClick={() => saveKey(p)}
                disabled={st === "saving" || st === "ok" || !(values[p.id] || "").trim()}
                className="px-3 py-2 rounded-xl bg-accent text-white text-[12px] disabled:opacity-40"
              >
                {st === "saving" ? "…" : "Add"}
              </button>
            </div>
            {st === "err" && errors[p.id] && (
              <p className="text-[11px] text-red-400">{errors[p.id]}</p>
            )}
          </div>
        );
      })}

      <div className="pt-2 border-t border-border">
        <button
          onClick={onDone}
          disabled={!canContinue}
          className="w-full py-3 rounded-xl bg-accent text-white font-medium disabled:opacity-40"
        >
          Open my Space →
        </button>
        {!canContinue && (
          <p className="text-[11px] text-muted text-center mt-1">
            Add at least one key above to continue.
          </p>
        )}
      </div>
    </div>
  );
}
