/**
 * GLM Bridge — a tiny Node.js HTTP service that wraps the z-ai-web-dev-sdk
 * so the Python backend can use the FREE, rate-limited GLM 5.2 model.
 *
 *   POST /chat  { messages, model? }  →  { content, model }
 *
 * Uses Node's standard http module (not Bun's serve) because the SDK's
 * network calls crashed silently inside Bun's persistent serve() loop —
 * the process died with no error, reverting the agent panel to mock echo.
 * Node's http module is rock-solid for this long-running bridge pattern.
 *
 * Port: 3030 (fixed, per the mini-services convention)
 * Auth: none (internal only — the Python backend is the only caller)
 * Run:  node index.ts  (or: bun run dev → bun --hot index.ts as fallback)
 */
import http from "node:http";
import ZAI from "z-ai-web-dev-sdk";

const PORT = 3030;

// Create the SDK instance ONCE at startup and reuse it (LLM skill best
// practice). Re-creating per request leaks connections.
let _zai: Awaited<ReturnType<typeof ZAI.create>> | null = null;
async function getZai() {
  if (!_zai) {
    _zai = await ZAI.create();
    console.log("[glm-bridge] ZAI SDK initialized");
  }
  return _zai;
}

// Never let an unhandled rejection kill the process — that would silently
// revert every "zai" tier agent to the MockAdapter echo.
process.on("unhandledRejection", (reason) => {
  console.error("[glm-bridge] unhandledRejection (survived):", reason);
});
process.on("uncaughtException", (err) => {
  console.error("[glm-bridge] uncaughtException (survived):", err);
});

// Warm the instance at startup so the first request isn't slow.
getZai().catch((e) => {
  console.error("[glm-bridge] FATAL: ZAI SDK init failed:", e);
});

const server = http.createServer(async (req, res) => {
  // CORS preflight
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  const url = new URL(req.url || "", `http://localhost:${PORT}`);

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === "GET" && url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      status: "ok",
      model: "glm-5.2",
      port: PORT,
      sdk_ready: !!_zai,
    }));
    return;
  }

  if (req.method !== "POST" || url.pathname !== "/chat") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "use POST /chat" }));
    return;
  }

  // Read the request body
  let body = "";
  for await (const chunk of req) {
    body += chunk;
  }

  try {
    const { messages, model } = JSON.parse(body || "{}");

    if (!messages || !Array.isArray(messages)) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "messages array required" }));
      return;
    }

    const zai = await getZai();
    const completion = await zai.chat.completions.create({
      model: model || "glm-5.2",
      messages,
      thinking: { type: "disabled" },
    });

    const content = completion.choices?.[0]?.message?.content || "(no response)";

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      content,
      model: model || "glm-5.2",
    }));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[glm-bridge] chat error:", msg);
    // 200 so the caller still gets a parseable response with the error text
    // inline — the agent panel surfaces it instead of showing a blank.
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      error: msg,
      content: `[GLM bridge error] ${msg}`,
    }));
  }
});

server.listen(PORT, () => {
  console.log(`[glm-bridge] listening on port ${PORT} — GLM 5.2 (free, rate-limited)`);
});
