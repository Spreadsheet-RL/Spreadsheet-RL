import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const distRoot = fileURLToPath(new URL("../dist", import.meta.url));

/**
 * The Mog compute engine ships a ~39.4 MiB WASM binary, well over Cloudflare's
 * 25 MiB per-file cap for Workers static assets.
 *
 * Vite emits that binary as a normal asset. The build pipeline then takes over:
 * it gzip-compresses the emitted file to `dist/assets/mog-wasm.wasm.gz` (which
 * fits under the cap) and deletes the uncompressed original, so nothing this
 * config produces ships as-is. The authenticated `/api/mog-wasm` Worker route
 * streams that archive back out as a decompressed `application/wasm` response.
 *
 * Hence the rewrite below: references to the WASM from built JavaScript must
 * point at the Worker route, because the emitted filename is gone by the time
 * the app runs. The wasm-bindgen loader reaches the binary via
 * `new URL('./compute_core_wasm_bg.wasm', import.meta.url)` and then `fetch()`s
 * it — a root-relative path resolves against the page origin, and a same-origin
 * fetch sends the session cookie, so the route stays authenticated.
 */
const MOG_WASM_ENDPOINT = "/api/mog-wasm";

export default defineConfig({
  root: frontendRoot,
  base: "/",
  plugins: [react()],
  build: {
    outDir: distRoot,
    emptyOutDir: true,
    target: "es2022",
    // The Mog compute engine ships a large WASM payload; the default warning is noise here.
    chunkSizeWarningLimit: 12_000,
  },
  experimental: {
    renderBuiltUrl(filename, { hostType, type }) {
      // Vite hands over the hashed output name, which may carry a `?`/`#`
      // postfix, so match the extension rather than the whole string.
      const isWasmAsset = type === "asset" && /\.wasm(?:[?#]|$)/.test(filename);
      if (isWasmAsset && hostType === "js") {
        // Vite splices `runtime` into the emitted asset string as
        // `"" + <expression> + ""`, so this has to be a JavaScript expression
        // rather than a bare path.
        return { runtime: JSON.stringify(MOG_WASM_ENDPOINT) };
      }
      // Everything else — hashed JS/CSS and the public workbook files — keeps
      // Vite's default base-relative handling.
      return undefined;
    },
  },
  server: {
    // `npm run dev` runs the worker; this proxy only helps when Vite is started directly.
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
});
