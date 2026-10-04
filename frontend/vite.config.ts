import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend address is configurable; nothing machine-specific is hard-coded.
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"], globals: true },
  server: {
    port: 5173,
    proxy: { "/health": backend, "/api": backend },
  },
});
