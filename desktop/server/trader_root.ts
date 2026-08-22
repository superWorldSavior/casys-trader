import { dirname } from "@std/path/dirname";
import { join } from "@std/path/join";
import { resolve } from "@std/path/resolve";

const BRIDGE_REL = join("desktop", "bridge", "api.py");

export class TraderRootError extends Error {
  override name = "TraderRootError";
}

export type ResolveTraderRootOptions = {
  envGet: (key: string) => string | undefined;
  startDirs: Iterable<string>;
  isFile?: (path: string) => boolean | Promise<boolean>;
};

function defaultIsFile(path: string): boolean {
  try {
    return Deno.statSync(path).isFile;
  } catch {
    return false;
  }
}

export function discoveryStartDirs(input: {
  moduleDir?: string | null;
  cwd?: string | null;
  execPath?: string | null;
}): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const add = (value?: string | null) => {
    if (!value) return;
    const resolved = resolve(value);
    if (seen.has(resolved)) return;
    seen.add(resolved);
    out.push(resolved);
  };
  add(input.moduleDir);
  add(input.cwd);
  if (input.execPath) add(dirname(input.execPath));
  return out;
}

function missingInstallMessage(detail: string): string {
  return [
    detail,
    "This desktop app is read-only and needs an external Casys Trader install to query Python read models.",
    "A .app left under this repository (for example desktop/build/CasysTrader.app) is discovered by walking up from the executable.",
    "Set CASYS_TRADER_ROOT to the repository root (the directory that contains desktop/bridge/api.py) when the app is moved elsewhere.",
    "Example: CASYS_TRADER_ROOT=/path/to/casys-trader",
  ].join("\n");
}

async function hasBridge(
  root: string,
  isFile: (path: string) => boolean | Promise<boolean>,
): Promise<boolean> {
  return await isFile(join(root, BRIDGE_REL));
}

async function walkFrom(
  start: string,
  isFile: (path: string) => boolean | Promise<boolean>,
): Promise<string | null> {
  let current = resolve(start);
  for (let i = 0; i < 24; i++) {
    if (await hasBridge(current, isFile)) return current;
    const parent = resolve(current, "..");
    if (parent === current) break;
    current = parent;
  }
  return null;
}

export async function resolveTraderRoot(
  options: ResolveTraderRootOptions,
): Promise<string> {
  const isFile = options.isFile ?? defaultIsFile;
  const configured = options.envGet("CASYS_TRADER_ROOT")?.trim();
  if (configured) {
    const root = resolve(configured);
    if (await hasBridge(root, isFile)) return root;
    throw new TraderRootError(
      missingInstallMessage(
        `CASYS_TRADER_ROOT=${configured} does not look like a Casys Trader install (missing ${BRIDGE_REL}).`,
      ),
    );
  }
  for (const start of options.startDirs) {
    const found = await walkFrom(start, isFile);
    if (found) return found;
  }
  throw new TraderRootError(
    missingInstallMessage(
      "Casys Trader installation not found from the executable location or working directory.",
    ),
  );
}
