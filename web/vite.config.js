import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Builds into the Python package, which serves it at /ui/ (inkbridge/api.py).
// `npm run dev` serves on :5173 and proxies the API to a local `inkbridge serve` on :8000.
export default defineConfig({
  base: "/ui/",
  plugins: [tailwindcss(), svelte()],
  build: { outDir: "../inkbridge/web", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
