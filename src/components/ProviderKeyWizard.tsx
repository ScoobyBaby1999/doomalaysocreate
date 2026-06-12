import { useState } from "react";

interface ProvisionResult {
  space_url: string;
  space_repo: string;
  rotation_secret: string;
  username: string;
  oauth_token: string;
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
    id: "groq",
    label: "Groq",
    envKey: "GROQ_API_KEY",
    keyUrl: "https://console.groq.com/keys",
    hint: "Fast inference, very generous free tier.",
    placeholder: "gsk_…",
  },
  {
    id: "nvidia",
    label: "NVIDIA AI",
    envKey: "NVIDIA_API_KEY",
    keyUrl: "https://build.nvidia.com",
    hint: "Free credits for Llama, Mixtral, and more.",
    placeholder: "nvapi-…",
  },
  {
    id: "google",
    label: "Google AI Studio",
    envKey: "GOOGLE_API_KEY",
    keyUrl: "https://aistudio.google.com/apikey",
    hint: "Gemini Flash & Pro — free tier.",
    placeholder: "AIza…",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    envKey: "OPENROUTER_API_KEY",
    keyUrl: "https://openrouter.ai/keys",
    hint: "Access 100+ models; many have free quotas.",
    placeholder: "sk-or-…",
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

  const field =
    "flex-1 bg-surface border border-border rounded-xl px-3 py-2 text-[13px] outline-none focus:border-accent min-w-0";

  return (
    <div className="p-4 space-y-5 max-w-xl mx-auto overflow-y-auto">
      <div>
        <h2 className="text-lg font-semibold">Add a provider key</h2>
        <p className="text-sm text-muted mt-1">
          Your Space is provisioned at{" "}
          <span className="text-accent">{provision.space_url}</span>. Add at
          least one free API key so the judges have models to call.
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
          disabled={!anyOk}
          className="w-full py-3 rounded-xl bg-accent text-white font-medium disabled:opacity-40"
        >
          Open my Space →
        </button>
        {!anyOk && (
          <p className="text-[11px] text-muted text-center mt-1">
            Add at least one key above to continue.
          </p>
        )}
      </div>
    </div>
  );
}
