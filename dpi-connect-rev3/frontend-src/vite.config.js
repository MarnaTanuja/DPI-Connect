import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI backend serves the built files at /app/ from ../frontend
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "../frontend", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8000" } },
});
