// Standalone GLM chat — reads messages JSON from stdin, prints {content} to stdout.
// Called by the Python ZaiAdapter via subprocess (more robust than a persistent
// HTTP server, which crashed silently under Bun's serve() and Node's http server).
import ZAI from "z-ai-web-dev-sdk";

process.on("uncaughtException", (e) => {
  process.stderr.write(`[glm-chat] uncaught: ${e}\n`);
  process.stdout.write(JSON.stringify({ content: `[GLM error] ${e.message}` }));
  process.exit(0);
});

try {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const { messages, model } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
  if (!Array.isArray(messages)) {
    process.stdout.write(JSON.stringify({ error: "messages array required" }));
    process.exit(0);
  }
  const zai = await ZAI.create();
  const completion = await zai.chat.completions.create({
    model: model || "glm-5.2",
    messages,
    thinking: { type: "disabled" },
  });
  const content = completion.choices?.[0]?.message?.content || "(no response)";
  process.stdout.write(JSON.stringify({ content, model: model || "glm-5.2" }));
} catch (e) {
  process.stderr.write(`[glm-chat] error: ${e}\n`);
  process.stdout.write(JSON.stringify({ content: `[GLM error] ${e.message}` }));
}
