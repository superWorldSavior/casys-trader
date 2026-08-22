import { assertEquals } from "@std/assert/equals";
import { assertRejects } from "@std/assert/rejects";
import { dirname } from "@std/path/dirname";
import { join } from "@std/path/join";
import {
  discoveryStartDirs,
  resolveTraderRoot,
  TraderRootError,
} from "./trader_root.ts";

async function makeTraderInstall(): Promise<string> {
  const root = await Deno.makeTempDir({ prefix: "casys-trader-root-" });
  await Deno.mkdir(join(root, "desktop", "bridge"), { recursive: true });
  await Deno.writeTextFile(
    join(root, "desktop", "bridge", "api.py"),
    "# stub\n",
  );
  return root;
}

Deno.test("resolveTraderRoot uses CASYS_TRADER_ROOT when it contains the bridge", async () => {
  const root = await makeTraderInstall();
  try {
    const resolved = await resolveTraderRoot({
      envGet: (key) => key === "CASYS_TRADER_ROOT" ? root : undefined,
      startDirs: ["/tmp/does-not-exist"],
    });
    assertEquals(resolved, root);
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("resolveTraderRoot rejects a CASYS_TRADER_ROOT that is not a Trader install", async () => {
  const empty = await Deno.makeTempDir({ prefix: "casys-not-trader-" });
  try {
    const error = await assertRejects(
      () =>
        resolveTraderRoot({
          envGet: (key) => key === "CASYS_TRADER_ROOT" ? empty : undefined,
          startDirs: [],
        }),
      TraderRootError,
    );
    assertEquals(error.message.includes("CASYS_TRADER_ROOT"), true);
    assertEquals(error.message.includes("desktop/bridge/api.py"), true);
    assertEquals(error.message.includes(empty), true);
  } finally {
    await Deno.remove(empty, { recursive: true });
  }
});

Deno.test("resolveTraderRoot walks up from startDirs during repository development", async () => {
  const root = await makeTraderInstall();
  const nested = join(root, "desktop", "server");
  await Deno.mkdir(nested, { recursive: true });
  try {
    const resolved = await resolveTraderRoot({
      envGet: () => undefined,
      startDirs: [nested],
    });
    assertEquals(resolved, root);
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("discoveryStartDirs walks from dirname(execPath), not the binary file", () => {
  const dirs = discoveryStartDirs({
    moduleDir: "/tmp/module",
    cwd: "/tmp/cwd",
    execPath: "/tmp/app/Contents/MacOS/laufey_webview",
  });
  assertEquals(dirs.includes("/tmp/app/Contents/MacOS"), true);
  assertEquals(dirs.includes("/tmp/app/Contents/MacOS/laufey_webview"), false);
});

Deno.test("resolveTraderRoot finds a repo from a macOS .app executable under desktop/build", async () => {
  const root = await makeTraderInstall();
  const execPath = join(
    root,
    "desktop",
    "build",
    "CasysTrader.app",
    "Contents",
    "MacOS",
    "laufey_webview",
  );
  await Deno.mkdir(dirname(execPath), { recursive: true });
  await Deno.writeFile(execPath, new Uint8Array([1]));
  const elsewhere = await Deno.makeTempDir({ prefix: "casys-elsewhere-" });
  try {
    const resolved = await resolveTraderRoot({
      envGet: () => undefined,
      startDirs: discoveryStartDirs({
        moduleDir: elsewhere,
        cwd: elsewhere,
        execPath,
      }),
    });
    assertEquals(resolved, root);
  } finally {
    await Deno.remove(root, { recursive: true });
    await Deno.remove(elsewhere, { recursive: true });
  }
});

Deno.test("CASYS_TRADER_ROOT overrides execPath discovery when the app was moved", async () => {
  const installed = await makeTraderInstall();
  const movedBundle = await makeTraderInstall();
  const execPath = join(
    movedBundle,
    "desktop",
    "build",
    "CasysTrader.app",
    "Contents",
    "MacOS",
    "laufey_webview",
  );
  await Deno.mkdir(dirname(execPath), { recursive: true });
  await Deno.writeFile(execPath, new Uint8Array([1]));
  try {
    const resolved = await resolveTraderRoot({
      envGet: (key) => key === "CASYS_TRADER_ROOT" ? installed : undefined,
      startDirs: discoveryStartDirs({ execPath }),
    });
    assertEquals(resolved, installed);
  } finally {
    await Deno.remove(installed, { recursive: true });
    await Deno.remove(movedBundle, { recursive: true });
  }
});

Deno.test("resolveTraderRoot fails with an actionable error when nothing matches", async () => {
  const empty = await Deno.makeTempDir({ prefix: "casys-empty-" });
  try {
    const error = await assertRejects(
      () =>
        resolveTraderRoot({
          envGet: () => undefined,
          startDirs: [empty],
        }),
      TraderRootError,
    );
    assertEquals(error.message.includes("CASYS_TRADER_ROOT"), true);
    assertEquals(error.message.includes("desktop/bridge/api.py"), true);
  } finally {
    await Deno.remove(empty, { recursive: true });
  }
});
