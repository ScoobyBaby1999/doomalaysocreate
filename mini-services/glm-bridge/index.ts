/**
 * GLM Bridge — a tiny Node.js/Bun HTTP service that wraps the z-ai-web-dev-sdk
 * so the Python backend can use the FREE, rate-limited GLM 5.2 model.
 *
 * The Python backend (critique-service) can't use z-ai-web-dev-sdk directly
 * (it's a Node.js package). This bridge exposes a simple HTTP API:
 *
 *   POST /chat  { messages, model? }  →  { content, model }
 *
 * The Python backend's conscious_tools.py calls this bridge for "zai" tier
 * agents instead of going through LiteLLM (which requires a paid API key).
 *
 * Port: 3030 (fixed, per the mini-services convention)
 * Auth: none (internal only — the Python backend is the only caller)
 */
import { serve } from "bun";

const PORT = 3030;

serve({
  port: PORT,
  async fetch(req) {
    // CORS + health check
    if (req.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    if (req.method === "GET" && new URL(req.url).pathname === "/health") {
      return Response.json({ status: "ok", model: "glm-5.2", port: PORT });
    }

    if (req.method !== "POST" || new URL(req.url).pathname !== "/chat") {
      return Response.json({ error: "use POST /chat" }, { status: 404 });
    }

    try {
      const { messages, model } = await req.json();

      if (!messages || !Array.isArray(messages)) {
        return Response.json({ error: "messages array required" }, { status: 400 });
      }

      // Dynamic import — the SDK may not be installed in all environments
      const ZAI = (await import("z-ai-web-dev-sdk")).default;
      const zai = await ZAI.create();

      const completion = await zai.chat.completions.create({
        model: model || "glm-5.2",
        messages,
        thinking: { type: "disabled" },
      });

      const content = completion.choices?.[0]?.message?.content || "(no response)";

      return Response.json({
        content,
        model: model || "glm-5.2",
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("[glm-bridge] error:", msg);
      return Response.json(
        { error: msg, content: `[GLM bridge error] ${msg}` },
        { status: 200 } // 200 so the caller still gets a response
      );
    }
  },
});

console.log(`[glm-bridge] listening on port ${PORT} — GLM 5.2 (free, rate-limited)`);
