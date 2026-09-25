import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs natively on 127.0.0.1:8000; the built bundle is served by
// Caddy on the Beelink, which proxies the same paths to the workstation.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: false },
      "/intake": api,
      "/positions": api,
      "/scores.json": api,
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
});
