import { assertEquals } from "@std/assert/equals";
import { join } from "@std/path/join";
import { createHandler, type InvokeBridge } from "./handler.ts";

const encoder = new TextEncoder();

async function makeDist(): Promise<string> {
  const dist = await Deno.makeTempDir({ prefix: "casys-dist-" });
  await Deno.writeTextFile(
    join(dist, "index.html"),
    "<!doctype html><title>desk</title>",
  );
  await Deno.mkdir(join(dist, "assets"));
  await Deno.writeTextFile(join(dist, "assets", "app.js"), "console.log(1)");
  await Deno.writeTextFile(join(dist, "assets", "app.css"), "body{color:red}");
  return dist;
}

function stubBridge(
  impl?: InvokeBridge,
): { calls: string[][]; invoke: InvokeBridge } {
  const calls: string[][] = [];
  return {
    calls,
    invoke: async (args) => {
      calls.push([...args]);
      if (impl) return await impl(args);
      return { ok: true, body: '{"ok":true}' };
    },
  };
}

Deno.test("GET /api/world-graph maps to the world-graph resource", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const response = await handler(
      new Request("http://127.0.0.1/api/world-graph?cutoff=2020-01-01"),
    );
    assertEquals(response.status, 200);
    assertEquals(bridge.calls, [["world-graph"]]);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("GET /api/snapshot maps to the bridge and returns JSON", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const response = await handler(
      new Request("http://127.0.0.1/api/snapshot"),
    );
    assertEquals(response.status, 200);
    assertEquals(
      response.headers.get("content-type"),
      "application/json; charset=utf-8",
    );
    assertEquals(await response.text(), '{"ok":true}');
    assertEquals(bridge.calls, [["snapshot"]]);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("GET /api/decisions forwards query args to the bridge", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const response = await handler(
      new Request("http://127.0.0.1/api/decisions?limit=10&symbol=AAPL"),
    );
    assertEquals(response.status, 200);
    assertEquals(bridge.calls, [[
      "decisions",
      "--limit",
      "10",
      "--symbol",
      "AAPL",
    ]]);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("unsupported API methods and routes are rejected without invoking the bridge", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const post = await handler(
      new Request("http://127.0.0.1/api/snapshot", { method: "POST" }),
    );
    assertEquals(post.status, 405);
    assertEquals(post.headers.get("allow"), "GET");
    const missing = await handler(
      new Request("http://127.0.0.1/api/not-a-route"),
    );
    assertEquals(missing.status, 404);
    const put = await handler(
      new Request("http://127.0.0.1/", { method: "PUT" }),
    );
    assertEquals(put.status, 405);
    assertEquals(bridge.calls, []);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("bridge failures surface as 500 text responses", async () => {
  const dist = await makeDist();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: () =>
        Promise.resolve({ ok: false, status: 500, body: "desk api failed" }),
    });
    const response = await handler(
      new Request("http://127.0.0.1/api/health"),
    );
    assertEquals(response.status, 500);
    assertEquals(
      response.headers.get("content-type"),
      "text/plain; charset=utf-8",
    );
    assertEquals(await response.text(), "desk api failed");
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("GET / serves index.html and hashed assets keep their MIME type", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const home = await handler(new Request("http://127.0.0.1/"));
    assertEquals(home.status, 200);
    assertEquals(home.headers.get("content-type")?.includes("text/html"), true);
    assertEquals(await home.text(), "<!doctype html><title>desk</title>");

    const js = await handler(new Request("http://127.0.0.1/assets/app.js"));
    assertEquals(js.status, 200);
    assertEquals(js.headers.get("content-type")?.includes("javascript"), true);
    assertEquals(await js.text(), "console.log(1)");

    const css = await handler(new Request("http://127.0.0.1/assets/app.css"));
    assertEquals(css.status, 200);
    assertEquals(css.headers.get("content-type")?.includes("text/css"), true);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("missing extension-less paths fall back to index.html; missing assets 404", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const spa = await handler(new Request("http://127.0.0.1/today"));
    assertEquals(spa.status, 200);
    assertEquals(await spa.text(), "<!doctype html><title>desk</title>");
    const missing = await handler(
      new Request("http://127.0.0.1/assets/missing.js"),
    );
    assertEquals(missing.status, 404);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("path traversal outside dist is rejected", async () => {
  const dist = await makeDist();
  const bridge = stubBridge();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const response = await handler(
      new Request("http://127.0.0.1/../handler_test.ts"),
    );
    assertEquals(response.status === 404 || response.status === 400, true);
    assertEquals(bridge.calls, []);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("Deno.serve smoke uses the stub bridge and never touches Python", async () => {
  const dist = await makeDist();
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: () =>
        Promise.resolve({ ok: true, body: '{"source":"stub"}' }),
    });
    const server = Deno.serve({
      hostname: "127.0.0.1",
      port: 0,
      onListen() {},
    }, handler);
    const addr = server.addr as Deno.NetAddr;
    try {
      const api = await fetch(`http://127.0.0.1:${addr.port}/api/snapshot`);
      assertEquals(api.status, 200);
      assertEquals(await api.json(), { source: "stub" });
      const home = await fetch(`http://127.0.0.1:${addr.port}/`);
      assertEquals(home.status, 200);
      assertEquals((await home.text()).includes("desk"), true);
      const denied = await fetch(`http://127.0.0.1:${addr.port}/api/snapshot`, {
        method: "DELETE",
      });
      assertEquals(denied.status, 405);
    } finally {
      await server.shutdown();
    }
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});

Deno.test("handler never treats a request body as a write to Trader state", async () => {
  const dist = await makeDist();
  const bridge = stubBridge(() => Promise.resolve({ ok: true, body: "{}" }));
  try {
    const handler = createHandler({
      distDir: dist,
      invokeBridge: bridge.invoke,
    });
    const response = await handler(
      new Request("http://127.0.0.1/api/settings", {
        method: "POST",
        body: encoder.encode('{"kill":true}'),
      }),
    );
    assertEquals(response.status, 405);
    assertEquals(bridge.calls, []);
  } finally {
    await Deno.remove(dist, { recursive: true });
  }
});
