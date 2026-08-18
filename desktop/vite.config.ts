import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vite";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const host = process.env.TAURI_DEV_HOST;

function mapApi(url: URL): string[] | null {
  const pathname = url.pathname;
  const query = url.searchParams;
  const withQuery = (resource: string, keys: string[]) => {
    const args = [resource];
    for (const key of keys) {
      const value = query.get(key);
      if (value) args.push(`--${key}`, value);
    }
    return args;
  };

  if (pathname === "/api/intelligence/briefing") return ["intelligence-briefing"];
  if (pathname === "/api/intelligence/news")
    return withQuery("intelligence-news", ["venue", "symbol", "limit"]);
  if (pathname === "/api/snapshot") return ["snapshot"];
  if (pathname === "/api/overview") return ["overview"];
  if (pathname === "/api/intelligence/world") return withQuery("intelligence-world", ["limit"]);
  if (pathname === "/api/intelligence/regions")
    return withQuery("intelligence-regions", ["venue", "limit"]);
  if (pathname === "/api/intelligence/companies")
    return withQuery("intelligence-companies", ["symbol", "venue", "limit"]);
  if (pathname === "/api/decisions") return withQuery("decisions", ["limit", "symbol", "filter"]);
  if (pathname === "/api/plans") return ["plans"];
  if (pathname === "/api/universe") return ["universe"];
  if (pathname === "/api/health") return ["health"];
  if (pathname === "/api/reports") return ["reports"];
  const report = pathname.match(/^\/api\/reports\/(.+)$/);
  if (report) return ["report", "--key", decodeURIComponent(report[1])];
  if (pathname === "/api/logs/events") return withQuery("logs-events", ["cursor", "limit"]);
  if (pathname === "/api/logs/trace") return withQuery("logs-trace", ["limit"]);
  if (pathname === "/api/settings") return ["settings"];
  if (pathname === "/api/portfolio") return withQuery("portfolio", ["sort"]);
  const symbolBars = pathname.match(/^\/api\/symbols\/([^/]+)\/bars$/);
  if (symbolBars) return ["symbol-bars", "--symbol", decodeURIComponent(symbolBars[1])];
  const symbol = pathname.match(/^\/api\/symbols\/([^/]+)$/);
  if (symbol) return ["symbol", "--symbol", decodeURIComponent(symbol[1])];
  return null;
}

function deskApi(): Plugin {
  return {
    name: "casys-desk-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.method !== "GET" || !req.url?.startsWith("/api/")) {
          next();
          return;
        }
        const mapped = mapApi(new URL(req.url, "http://127.0.0.1"));
        if (!mapped) {
          next();
          return;
        }
        void execFileAsync("uv", ["run", "python", "desktop/bridge/api.py", ...mapped], {
          cwd: repoRoot,
          timeout: 40_000,
          maxBuffer: 20 * 1024 * 1024,
          env: {
            ...process.env,
            PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH ?? ""}`,
          },
        })
          .then(({ stdout }) => {
            res.statusCode = 200;
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(stdout);
          })
          .catch((error: unknown) => {
            const err = error as { stderr?: string; message?: string };
            res.statusCode = 500;
            res.setHeader("Content-Type", "text/plain; charset=utf-8");
            res.end(err.stderr || err.message || "desk api failed");
          });
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), deskApi()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || "127.0.0.1",
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
});
