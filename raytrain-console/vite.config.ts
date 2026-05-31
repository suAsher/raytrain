import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5174,
    proxy: {
      // Dev-only: forward API calls to a locally running raytrain-server.
      // In production nginx does this (see nginx.conf).
      "/v1": "http://127.0.0.1:8099",
      "/healthz": "http://127.0.0.1:8099",
    },
  },
});
