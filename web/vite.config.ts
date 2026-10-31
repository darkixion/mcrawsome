import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";
import type { ViteDevServer } from "vite";
import { proxyOrigin } from "./dev-origin.ts";
import { defineConfig } from "vitest/config";

let devServer: ViteDevServer | undefined;
const engine = process.env.MCRAWSOME_API ?? "http://127.0.0.1:8642";

// The bundle goes into the Python package, which serves it at / (src/mcrawsome/server/static.py).
export default defineConfig({
  plugins: [svelte(), svelteTesting(), { name: "engine-origin", configureServer(server) { devServer = server; } }],
  build: {
    outDir: "../src/mcrawsome/server/web",
    emptyOutDir: true,
    assetsInlineLimit: 0, // the Content Security Policy allows data: images only, not data: fonts
  },
  server: {
    proxy: { "/api": {
      target: engine, ws: true, changeOrigin: true,
      configure(proxy) {
        const rewrite = (request: import("node:http").ClientRequest) => {
          const origin = request.getHeader("origin");
          const urls = devServer?.resolvedUrls;
          const replacement = proxyOrigin(typeof origin === "string" ? origin : undefined, [...urls?.local ?? [], ...urls?.network ?? []], engine);
          if (replacement) request.setHeader("origin", replacement);
        };
        proxy.on("proxyReq", rewrite);
        proxy.on("proxyReqWs", rewrite);
      },
    } },
  },
  test: {
    maxWorkers: 4,
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
    setupFiles: ["tests/setup.ts"],
  },
});
