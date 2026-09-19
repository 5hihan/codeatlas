import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets are emitted straight into the Python package so `pip install` ships the dashboard.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/codeatlas/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.CODEATLAS_API ?? "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
});
