import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Фронт отдаётся тем же nginx (hh.volnacrm.ru), API — на том же origin (/api),
// поэтому base "/" и прокси в dev на локальный бэкенд-порт.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8090",
    },
  },
});
