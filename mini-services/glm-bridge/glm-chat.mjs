// Standalone GLM chat — multi-provider bridge.
// Reads messages JSON from stdin, prints {content} to stdout.
//
// PROVIDERS (auto-detected from env vars, first match wins):
//   1. PUTER_API_TOKEN   → Puter.js  (FREE GLM-5.2, no API key, user-pays model)
//      → Get token: puter.com/dashboard → "Copy" auth token
//      → Free for developers; each user gets their own AI credits
//   2. ZAI_API_KEY       → Z.ai public API (real GLM-5.2, "Limited-time Free" cached input)
//   3. NVIDIA_API_KEY    → NVIDIA NIM (real GLM-5.1, 1000 free credits, no phone)
//   4. OPENROUTER_API_KEY → OpenRouter (GLM-5.2, paid, $1 free credit on signup)
//   5. SILICONFLOW_API_KEY → SiliconFlow (GLM models, free tier, GitHub login)
//   6. .z-ai-config file → sandbox fallback (session JWT, sandbox-only)
//
// MODEL ROUTING:
//   - "glm-5.2" → Puter / Z.ai / OpenRouter (NVIDIA only has 5.1)
//   - "glm-5.1" → Puter / NVIDIA / Z.ai / OpenRouter
//   - "glm-4.x" → auto-upgraded to 5.2 (never silently serve 4.x)
//
// THE "EXACTLY LIKE THIS CHAT" EXPERIENCE:
//   Set PUTER_API_TOKEN (from puter.com/dashboard) → free GLM-5.2, no Z.ai account,
//   no phone, no payment. Puter's "User-Pays" model: each user authenticates with
//   their free Puter account and gets AI credits. Developer pays $0.
import ZAI from "z-ai-web-dev-sdk";

process.on("uncaughtException", (e) => {
  process.stderr.write(`[glm-chat] uncaught: ${e}\n`);
  process.stdout.write(JSON.stringify({ content: `[GLM error] ${e.message}`, error: e.message }));
  process.exit(0);
});
// Absorb Puter.js internal rejections — it fires promises that reject
// independently of our await chain (e.g. on auth failure). Without this,
// the process crashes before the fallback providers can run.
process.on("unhandledRejection", (reason) => {
  process.stderr.write(`[glm-chat] unhandledRejection (absorbed): ${String(reason).slice(0, 200)}\n`);
});

function resolveModel(requested) {
  if (!requested) return "glm-5.2";
  const low = requested.toLowerCase();
  if (low.startsWith("glm-4")) return "glm-5.2";
  return requested;
}

// --- Puter.js provider (FREE, default) ---
// Calls Puter's OpenAI-compatible endpoint directly via fetch — NO NPM package
// needed. This is the critical fix: the @heyputer/puter.js SDK requires
// `npm install` on the HF Space, which doesn't always happen. By using plain
// fetch to https://api.puter.com/puterai/openai/v1/chat/completions, the
// bridge works with ZERO dependencies (Node 18+ has fetch built in).
async function callPuter(messages, model) {
  const token = process.env.PUTER_API_TOKEN?.trim();
  if (!token) throw new Error("Puter: PUTER_API_TOKEN not set");
  // Puter uses "z-ai/glm-5.2" format for model IDs on their OpenAI endpoint
  const puterModel = model.startsWith("z-ai/") ? model : `z-ai/${model}`;
  const url = "https://api.puter.com/puterai/openai/v1/chat/completions";
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: JSON.stringify({ model: puterModel, messages }),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`Puter API ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = await resp.json();
  return {
    content: data.choices?.[0]?.message?.content || "(no response)",
    served_model: data.model || model,
  };
}

// --- OpenAI-compatible providers ---
const OPENAI_PROVIDERS = [
  {
    name: "zai",
    envKey: "ZAI_API_KEY",
    baseUrl: () => process.env.ZAI_BASE_URL?.trim() || "https://api.z.ai/api/paas/v4",
    models: ["glm-5.2", "glm-5.1"],
    modelId: (m) => m,
  },
  {
    name: "nvidia",
    envKey: "NVIDIA_API_KEY",
    baseUrl: () => "https://integrate.api.nvidia.com/v1",
    models: ["glm-5.1"],
    modelId: () => "z-ai/glm-5.1",
  },
  {
    name: "openrouter",
    envKey: "OPENROUTER_API_KEY",
    baseUrl: () => "https://openrouter.ai/api/v1",
    models: ["glm-5.2", "glm-5.1"],
    modelId: (m) => `z-ai/${m}`,
  },
  {
    name: "siliconflow",
    envKey: "SILICONFLOW_API_KEY",
    baseUrl: () => "https://api.siliconflow.cn/v1",
    models: ["glm-5.2", "glm-5.1"],
    modelId: (m) => m,
  },
];

function pickOpenAIProvider(model) {
  for (const p of OPENAI_PROVIDERS) {
    if (process.env[p.envKey]?.trim() && p.models.includes(model)) {
      return {
        name: p.name,
        baseUrl: p.baseUrl(),
        apiKey: process.env[p.envKey].trim(),
        modelId: p.modelId(model),
      };
    }
  }
  return null;
}

async function callOpenAICompatible(provider, messages) {
  const url = `${provider.baseUrl}/chat/completions`;
  const body = JSON.stringify({ model: provider.modelId, messages });
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${provider.apiKey}`,
      ...(provider.name === "openrouter" ? {
        "HTTP-Referer": "https://scoobybaby1999-loom.hf.space",
        "X-Title": "loom conscious agents",
      } : {}),
    },
    body,
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${provider.name} API ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = await resp.json();
  return {
    content: data.choices?.[0]?.message?.content || "(no response)",
    served_model: data.model || provider.modelId,
  };
}

async function getFileZai() {
  return await ZAI.create();
}

try {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const { messages, model } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
  if (!Array.isArray(messages)) {
    process.stdout.write(JSON.stringify({ error: "messages array required" }));
    process.exit(0);
  }
  const resolvedModel = resolveModel(model);

  let content, servedModel, providerName;

  // Priority 1: Puter.js (FREE — no API key, user-pays model)
  if (process.env.PUTER_API_TOKEN?.trim()) {
    try {
      const result = await callPuter(messages, resolvedModel);
      content = result.content;
      servedModel = result.served_model;
      providerName = "puter";
    } catch (e) {
      process.stderr.write(`[glm-chat] puter failed: ${e.message}\n`);
    }
  }

  // Priority 2: OpenAI-compatible providers (Z.ai, NVIDIA, OpenRouter, SiliconFlow)
  if (!content) {
    const provider = pickOpenAIProvider(resolvedModel);
    if (provider) {
      try {
        const result = await callOpenAICompatible(provider, messages);
        content = result.content;
        servedModel = result.served_model;
        providerName = provider.name;
      } catch (e) {
        process.stderr.write(`[glm-chat] ${provider.name} failed: ${e.message}\n`);
      }
    }
  }

  // Priority 3: File config (sandbox fallback)
  if (!content) {
    try {
      const zai = await getFileZai();
      const completion = await zai.chat.completions.create({
        model: resolvedModel, messages, thinking: { type: "disabled" },
      });
      content = completion.choices?.[0]?.message?.content || "(no response)";
      servedModel = completion.model || resolvedModel;
      providerName = "file-config";
    } catch (e) {
      process.stderr.write(`[glm-chat] file config failed: ${e.message}\n`);
    }
  }

  if (!content) {
    content = `[GLM error] No provider available. Set ONE of:\n` +
      `  • PUTER_API_TOKEN (FREE GLM-5.2, no phone) → puter.com/dashboard\n` +
      `  • NVIDIA_API_KEY (free GLM-5.1, no phone) → build.nvidia.com\n` +
      `  • ZAI_API_KEY (real GLM-5.2, limited-time free) → z.ai\n` +
      `  • OPENROUTER_API_KEY (GLM-5.2, $1 free credit) → openrouter.ai`;
    servedModel = "none";
    providerName = "none";
  }

  process.stdout.write(JSON.stringify({
    content,
    model: resolvedModel,
    served_model: servedModel,
    provider: providerName,
    downgraded: servedModel !== resolvedModel && servedModel !== "none" &&
      !servedModel.includes(resolvedModel.replace("glm-", "glm-")),
  }));
} catch (e) {
  process.stderr.write(`[glm-chat] fatal: ${e}\n`);
  const msg = e instanceof Error ? e.message : String(e);
  process.stdout.write(JSON.stringify({ content: `[GLM error] ${msg}`, error: msg }));
}
