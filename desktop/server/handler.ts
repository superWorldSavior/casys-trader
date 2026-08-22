import { contentType } from "@std/media-types/content-type";
import { fromFileUrl } from "@std/path/from-file-url";
import { extname } from "@std/path/extname";
import { join } from "@std/path/join";
import { resolve } from "@std/path/resolve";
import { SEPARATOR } from "@std/path/constants";
import { mapApi } from "./api_routes.ts";
import type { BridgeResult } from "./bridge.ts";

export type InvokeBridge = (
  args: readonly string[],
) => Promise<BridgeResult>;

export type HandlerOptions = {
  distDir: string | URL;
  invokeBridge: InvokeBridge;
};

function textResponse(
  status: number,
  body: string,
  extra?: HeadersInit,
): Response {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      ...extra,
    },
  });
}

function distRootPath(distDir: string | URL): string {
  if (distDir instanceof URL) return fromFileUrl(distDir);
  if (distDir.startsWith("file:")) return fromFileUrl(distDir);
  return resolve(distDir);
}

function safeJoin(root: string, pathname: string): string | null {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (decoded.includes("\0")) return null;
  const relative = decoded.replace(/^\/+/, "");
  const rootResolved = resolve(root);
  const candidate = relative === ""
    ? rootResolved
    : resolve(rootResolved, relative);
  if (candidate === rootResolved) return candidate;
  const prefix = rootResolved.endsWith(SEPARATOR)
    ? rootResolved
    : rootResolved + SEPARATOR;
  if (!candidate.startsWith(prefix)) return null;
  return candidate;
}

async function readFile(path: string): Promise<Uint8Array | null> {
  try {
    const info = await Deno.stat(path);
    if (!info.isFile) return null;
    return await Deno.readFile(path);
  } catch {
    return null;
  }
}

function toBlobPart(bytes: Uint8Array): Uint8Array<ArrayBuffer> {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy;
}

function fileResponse(path: string, bytes: Uint8Array): Response {
  const type = contentType(extname(path)) ?? "application/octet-stream";
  return new Response(new Blob([toBlobPart(bytes)]), {
    status: 200,
    headers: { "content-type": type },
  });
}

async function serveStatic(
  distDir: string,
  pathname: string,
): Promise<Response> {
  const indexPath = join(distDir, "index.html");
  if (pathname === "/" || pathname === "") {
    const index = await readFile(indexPath);
    if (!index) {
      return textResponse(
        500,
        `Vite build output not found at ${indexPath}. Run \`npm run build\` in desktop/ (or \`deno task ui:build\`) before launching Deno Desktop.`,
      );
    }
    return fileResponse(indexPath, index);
  }

  const safe = safeJoin(distDir, pathname);
  if (safe === null) return textResponse(404, "not found");

  const direct = await readFile(safe);
  if (direct) return fileResponse(safe, direct);

  const last = pathname.split("/").pop() ?? "";
  if (!last.includes(".")) {
    const index = await readFile(indexPath);
    if (index) return fileResponse(indexPath, index);
  }
  return textResponse(404, "not found");
}

export function createHandler(
  options: HandlerOptions,
): (request: Request) => Promise<Response> {
  const distDir = distRootPath(options.distDir);
  return async (request) => {
    const url = new URL(request.url);
    const method = request.method.toUpperCase();

    if (url.pathname.startsWith("/api/")) {
      if (method !== "GET") {
        return textResponse(405, "method not allowed", { allow: "GET" });
      }
      let mapped: string[] | null;
      try {
        mapped = mapApi(url);
      } catch (error) {
        if (error instanceof URIError) return textResponse(400, "invalid path");
        throw error;
      }
      if (!mapped) return textResponse(404, "not found");
      const result = await options.invokeBridge(mapped);
      if (result.ok) {
        return new Response(result.body, {
          status: 200,
          headers: { "content-type": "application/json; charset=utf-8" },
        });
      }
      return textResponse(result.status, result.body);
    }

    if (method !== "GET" && method !== "HEAD") {
      return textResponse(405, "method not allowed", { allow: "GET, HEAD" });
    }

    const response = await serveStatic(distDir, url.pathname);
    if (method === "HEAD") {
      return new Response(null, {
        status: response.status,
        headers: response.headers,
      });
    }
    return response;
  };
}
