/**
 * GLM Bridge — wraps z-ai-web-dev-sdk for the Python backend.
 *
 * Endpoints:
 *   POST /chat    { messages, model?, tools? }  →  { content, model, tool_results }
 *   GET  /health  →  { status, model, tools }
 *
 * Tools supported (via z-ai-web-dev-sdk built-in functions):
 *   - web_search: search the web for real-time info
 *   - page_reader: read/extract content from a URL
 *
 * The Python backend calls this for "zai" tier agents. Free + rate-limited.
 *
 * Port: 3030 (fixed, per the mini-services convention)
 * Auth: none (internal only — the Python backend is the only caller)
 */
import { serve } from "bun";

const PORT = 3030;

const AVAILABLE_TOOLS = ["web_search", "page_reader"];

serve({
  port: PORT,
  async fetch(req) {
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
      return Response.json({
        status: "ok",
        model: "glm-5.2",
        port: PORT,
        tools: AVAILABLE_TOOLS,
      });
    }

    if (req.method !== "POST" || new URL(req.url).pathname !== "/chat") {
      return Response.json({ error: "use POST /chat" }, { status: 404 });
    }

    try {
      const { messages, model, tools } = await req.json() as {
        messages: Array<{ role: string; content: string }>;
        model?: string;
        tools?: string[]; // ["web_search", "page_reader"]
      };

      if (!messages || !Array.isArray(messages)) {
        return Response.json({ error: "messages array required" }, { status: 400 });
      }

      const ZAI = (await import("z-ai-web-dev-sdk")).default;
      const zai = await ZAI.create();

      const completion = await zai.chat.completions.create({
        model: model || "glm-5.2",
        messages,
        thinking: { type: "disabled" as const },
      });

      const content = completion.choices?.[0]?.message?.content || "(no response)";

      // If tools were requested, run them based on the model's response.
      // The z-ai-web-dev-sdk doesn't support automatic tool calling in the
      // chat completions API, so we do a simple heuristic: if the model's
      // response contains "search:" or "read:", we run the corresponding
      // function and append the results.
      const toolResults: Array<{ tool: string; query: string; result: unknown }> = [];

      if (tools && Array.isArray(tools) && tools.length > 0) {
        // Check if the model wants to search the web
        const searchMatch = content.match(/(?:search|look up|find):\s*(.+)/i);
        if (searchMatch && tools.includes("web_search")) {
          try {
            const query = searchMatch[1].trim();
            const results = await zai.functions.invoke("web_search", {
              query,
              num: 5,
            });
            toolResults.push({ tool: "web_search", query, result: results });
          } catch (e) {
            toolResults.push({
              tool: "web_search",
              query: searchMatch[1].trim(),
              result: { error: String(e) },
            });
          }
        }

        // Check if the model wants to read a URL
        const urlMatch = content.match(/(?:read|fetch|visit):\s*(https?:\/\/[^\s]+)/i);
        if (urlMatch && tools.includes("page_reader")) {
          try {
            const url = urlMatch[1].trim();
            const result = await zai.functions.invoke("page_reader", { url });
            toolResults.push({ tool: "page_reader", query: url, result });
          } catch (e) {
            toolResults.push({
              tool: "page_reader",
              query: urlMatch[1].trim(),
              result: { error: String(e) },
            });
          }
        }
      }

      return Response.json({
        content,
        model: model || "glm-5.2",
        tool_results: toolResults.length > 0 ? toolResults : undefined,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("[glm-bridge] error:", msg);
      return Response.json(
        { error: msg, content: `[GLM bridge error] ${msg}` },
        { status: 200 },
      );
    }
  },
});

console.log(`[glm-bridge] listening on port ${PORT} — GLM 5.2 + tools: ${AVAILABLE_TOOLS.join(", ")}`);
