import { assertEquals } from "@std/assert/equals";
import {
  adoptDeskWindow,
  DESK_WINDOW,
  type DeskBrowserWindowCtor,
  runtimeBrowserWindowCtor,
} from "./window.ts";

Deno.test("DESK_WINDOW matches the former Tauri 1440x900 cockpit", () => {
  assertEquals(DESK_WINDOW.title, "Casys Trader");
  assertEquals(DESK_WINDOW.width, 1440);
  assertEquals(DESK_WINDOW.height, 900);
});

Deno.test("runtimeBrowserWindowCtor is absent in headless Deno", () => {
  assertEquals(runtimeBrowserWindowCtor({}), undefined);
  assertEquals(runtimeBrowserWindowCtor({ BrowserWindow: 1 }), undefined);
});

Deno.test("adoptDeskWindow is a no-op when BrowserWindow is missing", () => {
  assertEquals(adoptDeskWindow(undefined), { adopted: false });
});

Deno.test("adoptDeskWindow constructs the implicit window with title and size", () => {
  const seen: unknown[] = [];
  const Fake: DeskBrowserWindowCtor = class {
    constructor(options: { title: string; width: number; height: number }) {
      seen.push(options);
    }
  };
  assertEquals(adoptDeskWindow(Fake), { adopted: true });
  assertEquals(seen, [{
    title: "Casys Trader",
    width: 1440,
    height: 900,
  }]);
  const options = seen[0] as Record<string, unknown>;
  assertEquals("minWidth" in options, false);
  assertEquals("minHeight" in options, false);
});

Deno.test("runtimeBrowserWindowCtor returns a function constructor when present", () => {
  class FakeWindow {}
  const ctor = runtimeBrowserWindowCtor({ BrowserWindow: FakeWindow });
  assertEquals(ctor, FakeWindow);
});
