import { assertEquals } from "@std/assert/equals";
import { assertRejects } from "@std/assert/rejects";
import {
  BridgeOutputTooLargeError,
  BridgeTimeoutError,
  createUvBridge,
  type RunCommand,
  runDenoCommand,
} from "./bridge.ts";

function delayed<T>(value: T, ms: number): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

Deno.test("createUvBridge invokes uv run python desktop/bridge/api.py", async () => {
  const calls: Array<{ command: string; args: string[]; cwd: string }> = [];
  const runCommand: RunCommand = (request) => {
    calls.push({
      command: request.command,
      args: request.args,
      cwd: request.cwd,
    });
    return Promise.resolve({
      code: 0,
      stdout: new TextEncoder().encode('{"ok":true}'),
      stderr: new Uint8Array(),
    });
  };
  const invoke = createUvBridge({
    traderRoot: "/tmp/trader",
    runCommand,
    uvBin: "uv",
  });
  const result = await invoke(["snapshot"]);
  assertEquals(result, { ok: true, body: '{"ok":true}' });
  assertEquals(calls, [{
    command: "uv",
    args: ["run", "python", "desktop/bridge/api.py", "snapshot"],
    cwd: "/tmp/trader",
  }]);
});

Deno.test("createUvBridge forwards timeout and output-size bounds", async () => {
  let timeoutMs = 0;
  let maxBytes = 0;
  const invoke = createUvBridge({
    traderRoot: "/tmp/trader",
    timeoutMs: 12_000,
    maxBytes: 4096,
    runCommand: (request) => {
      timeoutMs = request.timeoutMs;
      maxBytes = request.maxBytes;
      return Promise.resolve({
        code: 0,
        stdout: new TextEncoder().encode("{}"),
        stderr: new Uint8Array(),
      });
    },
  });
  await invoke(["health"]);
  assertEquals(timeoutMs, 12_000);
  assertEquals(maxBytes, 4096);
});

Deno.test("createUvBridge de-duplicates overlapping identical calls", async () => {
  let started = 0;
  let finished = 0;
  const runCommand: RunCommand = () => {
    started += 1;
    return delayed({
      code: 0,
      stdout: new TextEncoder().encode('{"n":1}'),
      stderr: new Uint8Array(),
    }, 30).then((value) => {
      finished += 1;
      return value;
    });
  };
  const invoke = createUvBridge({
    traderRoot: "/tmp/trader",
    runCommand,
  });
  const [a, b] = await Promise.all([
    invoke(["snapshot"]),
    invoke(["snapshot"]),
  ]);
  assertEquals(a, b);
  assertEquals(started, 1);
  assertEquals(finished, 1);
  await invoke(["snapshot"]);
  assertEquals(started, 2);
});

Deno.test("createUvBridge does not share distinct argument lists", async () => {
  const seen: string[][] = [];
  const runCommand: RunCommand = (request) => {
    seen.push(request.args.slice(3));
    return Promise.resolve({
      code: 0,
      stdout: new TextEncoder().encode("{}"),
      stderr: new Uint8Array(),
    });
  };
  const invoke = createUvBridge({ traderRoot: "/tmp/trader", runCommand });
  await Promise.all([
    invoke(["snapshot"]),
    invoke(["health"]),
  ]);
  assertEquals(seen.length, 2);
});

Deno.test("createUvBridge maps non-zero exit to a 500 payload", async () => {
  const invoke = createUvBridge({
    traderRoot: "/tmp/trader",
    runCommand: () =>
      Promise.resolve({
        code: 2,
        stdout: new Uint8Array(),
        stderr: new TextEncoder().encode("boom"),
      }),
  });
  assertEquals(await invoke(["snapshot"]), {
    ok: false,
    status: 500,
    body: "boom",
  });
});

Deno.test("createUvBridge maps timeout and oversized output to 500 payloads", async () => {
  const timeoutInvoke = createUvBridge({
    traderRoot: "/tmp/trader",
    runCommand: () => Promise.reject(new BridgeTimeoutError(40_000)),
  });
  const timeout = await timeoutInvoke(["snapshot"]);
  assertEquals(timeout.ok, false);
  if (!timeout.ok) {
    assertEquals(timeout.status, 500);
    assertEquals(timeout.body.includes("timed out"), true);
  }

  const largeInvoke = createUvBridge({
    traderRoot: "/tmp/trader",
    runCommand: () => Promise.reject(new BridgeOutputTooLargeError(16)),
  });
  const large = await largeInvoke(["snapshot"]);
  assertEquals(large.ok, false);
  if (!large.ok) {
    assertEquals(large.status, 500);
    assertEquals(large.body.includes("exceeded"), true);
  }
});

function sh(script: string, maxBytes: number, timeoutMs: number) {
  return runDenoCommand({
    command: "/bin/sh",
    args: ["-c", script],
    cwd: "/tmp",
    env: {},
    timeoutMs,
    maxBytes,
  });
}

Deno.test("runDenoCommand kills the child as soon as stdout exceeds maxBytes", async () => {
  const started = Date.now();
  await assertRejects(
    () =>
      sh(
        "dd if=/dev/zero bs=1024 count=40 2>/dev/null; sleep 5",
        2048,
        8_000,
      ),
    BridgeOutputTooLargeError,
  );
  assertEquals(Date.now() - started < 1500, true);
});

Deno.test("runDenoCommand kills the child as soon as stderr exceeds maxBytes", async () => {
  const started = Date.now();
  await assertRejects(
    () =>
      sh(
        "dd if=/dev/zero bs=1024 count=40 1>&2; sleep 5",
        2048,
        8_000,
      ),
    BridgeOutputTooLargeError,
  );
  assertEquals(Date.now() - started < 1500, true);
});

Deno.test("runDenoCommand times out and reaps a stuck child", async () => {
  const started = Date.now();
  await assertRejects(
    () => sh("sleep 5", 1_000_000, 200),
    BridgeTimeoutError,
  );
  assertEquals(Date.now() - started < 1500, true);
});

Deno.test("runDenoCommand returns bounded stdout when the child exits cleanly", async () => {
  const result = await sh("printf 'abc'", 1024, 2_000);
  assertEquals(result.code, 0);
  assertEquals(new TextDecoder().decode(result.stdout), "abc");
});
