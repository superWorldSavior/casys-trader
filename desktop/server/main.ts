import { join } from "@std/path/join";
import { fromFileUrl } from "@std/path/from-file-url";
import { createUvBridge } from "./bridge.ts";
import { createHandler } from "./handler.ts";
import { discoveryStartDirs, resolveTraderRoot } from "./trader_root.ts";
import { adoptDeskWindow } from "./window.ts";

function here(): string {
  return fromFileUrl(new URL(".", import.meta.url));
}

function distDir(): URL {
  return new URL("../dist/", import.meta.url);
}

function assertDistPresent(dir: URL): void {
  const index = join(fromFileUrl(dir), "index.html");
  try {
    if (!Deno.statSync(index).isFile) {
      throw new Error("not a file");
    }
  } catch {
    throw new Error(
      `Vite build output not found at ${index}. Run \`npm run build\` in desktop/ (or \`deno task ui:build\`) before launching Deno Desktop.`,
    );
  }
}

export async function startDeskServer(): Promise<
  Deno.HttpServer<Deno.NetAddr>
> {
  const traderRoot = await resolveTraderRoot({
    envGet: (key) => Deno.env.get(key),
    startDirs: discoveryStartDirs({
      moduleDir: here(),
      cwd: Deno.cwd(),
      execPath: Deno.execPath(),
    }),
  });
  const dist = distDir();
  assertDistPresent(dist);
  const handler = createHandler({
    distDir: dist,
    invokeBridge: createUvBridge({ traderRoot }),
  });
  const server = Deno.serve({
    hostname: "127.0.0.1",
    onListen: ({ hostname, port }) => {
      console.log(`Casys Trader desktop serving http://${hostname}:${port}`);
    },
  }, handler);
  adoptDeskWindow();
  return server;
}

if (import.meta.main) {
  await startDeskServer();
}
