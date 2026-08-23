import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import { mapApi } from "./server/api_routes";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");

function deskApi(): Plugin {
  return {
    name: "casys-desk-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.method !== "GET" || !req.url?.startsWith("/api/")) {
          next();
          return;
        }
        let mapped: string[] | null;
        try {
          mapped = mapApi(new URL(req.url, "http://127.0.0.1"));
        } catch {
          res.statusCode = 400;
          res.setHeader("Content-Type", "text/plain; charset=utf-8");
          res.end("invalid path");
          return;
        }
        if (!mapped) {
          next();
          return;
        }
        void execFileAsync("uv", [
          "run",
          "python",
          "desktop/bridge/api.py",
          ...mapped,
        ], {
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
    host: "127.0.0.1",
  },
});
