/** Former Tauri window: 1440x900. Deno 2.9.2 has no minWidth/minHeight. */
export const DESK_WINDOW = {
  title: "Casys Trader",
  width: 1440,
  height: 900,
} as const;

export type DeskBrowserWindowCtor = new (
  options: {
    title: string;
    width: number;
    height: number;
  },
) => unknown;

export function runtimeBrowserWindowCtor(
  deno: Record<string, unknown> = Deno as unknown as Record<string, unknown>,
): DeskBrowserWindowCtor | undefined {
  const ctor = deno["BrowserWindow"];
  return typeof ctor === "function" ? ctor as DeskBrowserWindowCtor : undefined;
}

/** Adopt the implicit desktop window, or no-op under `deno run` / tests. */
export function adoptDeskWindow(
  ctor: DeskBrowserWindowCtor | undefined = runtimeBrowserWindowCtor(),
): { adopted: boolean } {
  if (!ctor) return { adopted: false };
  new ctor({
    title: DESK_WINDOW.title,
    width: DESK_WINDOW.width,
    height: DESK_WINDOW.height,
  });
  return { adopted: true };
}
