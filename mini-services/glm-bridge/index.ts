/**
 * GLM Bridge — multi-provider HTTP service for the Python backend.
 *
 *   POST /chat  { messages, model? }  →  { content, model, served_model, provider, downgraded }
 *   GET  /health → { status, provider, available_providers }
 *
 * PROVIDERS (auto-detected from env vars, first match wins):
 *   1. PUTER_API_TOKEN   → Puter.js  (FREE GLM-5.2, user-pays model, no Z.ai account needed)
 *   2. ZAI_API_KEY       → Z.ai API  (real GLM-5.2, "Limited-time Free" cached input)
 *   3. NVIDIA_API_KEY    → NVIDIA NIM (real GLM-5.1, 1000 free credits, no phone)
 *   4. OPENROUTER_API_KEY → OpenRouter (GLM-5.2, $1 free credit on signup)
 *   5. SILICONFLOW_API_KEY → SiliconFlow (free tier, GitHub login)
 *   6. .z-ai-config file → sandbox fallback
 *
 * The "exactly like this chat" experience:
 *   Set PUTER_API_TOKEN (from puter.com/dashboard) → free GLM-5.2, no phone,
 *   no payment. Puter's "User-Pays" model: developer pays $0.
 */
import http from "node:http";
import ZAI from "z-ai-web-dev-sdk";

const PORT = 3030;

function resolveModel(requested) {
  if (!requested) return "glm-5.2";
  const low = requested.toLowerCase();
  if (low.startsWith("glm-4")) return "glm-5.2";
  return requested;
}

// --- Puter.js provider (FREE, default priority) ---
// Calls Puter's OpenAI-compatible endpoint directly via fetch — NO NPM package
// needed. This is the critical fix: the @heyputer/puter.js SDK requires
// `npm install` on the HF Space, which doesn't always happen. By using plain
// fetch, the bridge works with ZERO extra dependencies (Node 18+ has fetch).
async function callPuter(messages, model) {
  const token = process.env.PUTER_API_TOKEN?.trim();
  if (!token) throw new Error("Puter: PUTER_API_TOKEN not set");
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
  { name: "zai", envKey: "ZAI_API_KEY",
    baseUrl: () => process.env.ZAI_BASE_URL?.trim() || "https://api.z.ai/api/paas/v4",
    models: ["glm-5.2", "glm-5.1"], modelId: (m) => m },
  { name: "nvidia", envKey: "NVIDIA_API_KEY",
    baseUrl: () => "https://integrate.api.nvidia.com/v1",
    models: ["glm-5.1"], modelId: () => "z-ai/glm-5.1" },
  { name: "openrouter", envKey: "OPENROUTER_API_KEY",
    baseUrl: () => "https://openrouter.ai/api/v1",
    models: ["glm-5.2", "glm-5.1"], modelId: (m) => `z-ai/${m}` },
  { name: "siliconflow", envKey: "SILICONFLOW_API_KEY",
    baseUrl: () => "https://api.siliconflow.cn/v1",
    models: ["glm-5.2", "glm-5.1"], modelId: (m) => m },
];

function pickOpenAIProvider(model) {
  for (const p of OPENAI_PROVIDERS) {
    if (process.env[p.envKey]?.trim() && p.models.includes(model)) {
      return { name: p.name, baseUrl: p.baseUrl(),
        apiKey: process.env[p.envKey].trim(), modelId: p.modelId(model) };
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

function availableProviders() {
  const avail = [];
  if (process.env.PUTER_API_TOKEN?.trim()) avail.push({ name: "puter", models: ["glm-5.2", "glm-5.1"] });
  for (const p of OPENAI_PROVIDERS) {
    if (process.env[p.envKey]?.trim()) avail.push({ name: p.name, models: p.models });
  }
  return avail;
}

let _fileZai = null;
async function getFileZai() {
  if (_fileZai) return _fileZai;
  _fileZai = await ZAI.create();
  return _fileZai;
}

process.on("unhandledRejection", (r) => console.error("[glm-bridge] unhandledRejection:", r));
process.on("uncaughtException", (e) => console.error("[glm-bridge] uncaughtException:", e));

const server = http.createServer(async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  const url = new URL(req.url || "", `http://localhost:${PORT}`);

  if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }

  if (req.method === "GET" && url.pathname === "/health") {
    const avail = availableProviders();
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      status: "ok", port: PORT,
      provider: avail[0]?.name || "none",
      available_providers: avail,
      model: "glm-5.2 or glm-5.1",
      note: avail.length ? undefined : "set PUTER_API_TOKEN (free 5.2) for zero-setup GLM",
    }));
    return;
  }

  if (req.method !== "POST" || url.pathname !== "/chat") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "use POST /chat" }));
    return;
  }

  let body = "";
  for await (const chunk of req) body += chunk;

  try {
    const { messages, model } = JSON.parse(body || "{}");
    if (!messages || !Array.isArray(messages)) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "messages array required" }));
      return;
    }
    const resolvedModel = resolveModel(model);
    let content, servedModel, providerName;

    // Priority 1: Puter.js (FREE)
    if (process.env.PUTER_API_TOKEN?.trim()) {
      try {
        const r = await callPuter(messages, resolvedModel);
        content = r.content; servedModel = r.served_model; providerName = "puter";
      } catch (e) {
        console.error("[glm-bridge] puter failed:", e.message);
      }
    }

    // Priority 2: OpenAI-compatible
    if (!content) {
      const provider = pickOpenAIProvider(resolvedModel);
      if (provider) {
        try {
          const r = await callOpenAICompatible(provider, messages);
          content = r.content; servedModel = r.served_model; providerName = provider.name;
        } catch (e) {
          console.error(`[glm-bridge] ${provider.name} failed:`, e.message);
        }
      }
    }

    // Priority 3: File config (sandbox)
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
        console.error("[glm-bridge] file config failed:", e.message);
      }
    }

    if (!content) {
      content = `[GLM error] No provider available. Set PUTER_API_TOKEN (free, puter.com/dashboard) or another provider key.`;
      servedModel = "none"; providerName = "none";
    }

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      content, model: resolvedModel, served_model: servedModel, provider: providerName,
      downgraded: servedModel !== resolvedModel && servedModel !== "none" &&
        !servedModel.includes(resolvedModel.replace("glm-", "glm-")),
    }));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[glm-bridge] chat error:", msg);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: msg, content: `[GLM error] ${msg}` }));
  }
});

server.listen(PORT, () => {
  const avail = availableProviders();
  console.log(`[glm-bridge] port ${PORT} — providers: ${avail.length ? avail.map(p=>p.name).join(", ") : "none"}`);
});
