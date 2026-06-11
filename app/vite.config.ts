import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In the browser/dev target we proxy /backend -> the panel gateway to dodge CORS.
// In the Tauri WebView the client talks to the configured URL directly (no proxy).
// Set VITE_BACKEND_ORIGIN to point the dev proxy at a different gateway.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_ORIGIN || "https://scoobybaby1999-loom.hf.space";
  return {
    plugins: [react()],
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
