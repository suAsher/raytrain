import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During local dev, proxy /v1 + /healthz to the control plane so the SPA and
// API share an origin (avoids CORS headaches). In production the SPA is served
// by nginx which proxies the same paths to the raytrain-server Service.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: process.env.RAYTRAIN_API || "http://localhost:8080",
        changeOrigin: true,
      },
      "/healthz": {
        target: process.env.RAYTRAIN_API || "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
});
