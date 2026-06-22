import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In the browser/dev target we proxy /backend -> the panel gateway to dodge CORS.
// In the Tauri WebView the client talks to the configured URL directly (no proxy).
// Set VITE_BACKEND_ORIGIN to point the dev proxy at a different gateway.
// Set VITE_MAIN_SPACE to the main Space URL for GitHub OAuth proxy flow.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_ORIGIN || "https://scoobybaby1999-doomalaysocreate.hf.space";
  const mainSpace = env.VITE_MAIN_SPACE || "https://scoobybaby1999-doomalaysocreate.hf.space";
  return {
    plugins: [react()],
    define: {
      "import.meta.env.VITE_MAIN_SPACE": JSON.stringify(mainSpace),
    },
    server: {
      host: true,
      proxy: {
        "/backend": {
          target: backend,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/backend/, ""),
        },
      },
    },
    build: { target: "es2020", sourcemap: true },
  };
});
