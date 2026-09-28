import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend-for-frontend (python -m golem.ui) in development; in production the ingress sends
// these paths to it on the same origin (ADR 0018).
const BFF = process.env.GOLEM_BFF_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    // No asset becomes a data: URL, so the CSP needs no data: source.
    assetsInlineLimit: 0,
  },
  server: {
    proxy: Object.fromEntries(
      ["/api", "/login", "/callback", "/logout", "/healthz"].map((path) => [path, BFF]),
    ),
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
