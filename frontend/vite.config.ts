import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend address is configurable; nothing machine-specific is hard-coded.
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/health": backend, "/api": backend },
  },
});
