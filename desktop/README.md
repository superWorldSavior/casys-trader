# Casys Trader desktop

Read-only cockpit. The React/Vite UI polls the Python read model via `/api/*`
and never writes runtime state. Native packaging uses Deno Desktop (WebView);
the same UI can run in a browser for development.

## Native (Deno Desktop)

```bash
make desktop
```

Builds the Vite assets, then opens the native window via `deno task desktop`
(`deno desktop --hmr`, WebView backend). The first `Deno.BrowserWindow`
adopts the implicit window at 1440×900 titled "Casys Trader" (Deno 2.9.2
has no min-size API). The Deno process serves `dist/` and maps GET `/api`
routes onto `desktop/bridge/api.py` through `uv`. Headless `deno task serve`
skips the window constructor.

Package a macOS `.app` (written to `desktop/build/CasysTrader.app`, gitignored):

```bash
make desktop-build
```

That is `deno task pack`. Trader-root discovery walks up from the module
directory, the process working directory, and `dirname(Deno.execPath())`. A
bundle left under this repository (for example
`desktop/build/CasysTrader.app/Contents/MacOS/laufey_webview`) therefore finds
`desktop/bridge/api.py` without extra configuration. Set `CASYS_TRADER_ROOT` to
the repository root when the `.app` is moved elsewhere.

Runtime permissions are scoped (`deno … -P`, not `-A`). `read` stays broad
because the Trader root is resolved at runtime from those start directories or
from `CASYS_TRADER_ROOT`. The env allow-list is only `CASYS_TRADER_ROOT`,
`UV_BIN`, and `PATH`.

## Browser (Vite)

```bash
make desktop-browser
```

Opens http://127.0.0.1:1420/ — keep that Vite process running. API mapping is
shared with the Deno server (`desktop/server/api_routes.ts`). Direct
`npm run build` / `npm run dev` still go through Node and `node_modules`.

`deno task` must not call `npm run <script>`: Deno 2.9.2 rewrites that into the
package.json script and resolves `tsc`/`vite` from
`~/Library/Caches/deno/npm/...` when `nodeModulesDir` is `"none"`. Tasks
therefore invoke `./node_modules/.bin/tsc` and `./node_modules/.bin/vite`.
`--hmr` reloads the Deno server (`server/main.ts`), not the Vite dev server.

## Tasks (`desktop/deno.json`)

| Task | Command |
|---|---|
| `deno task desktop` | `./node_modules/.bin/tsc && ./node_modules/.bin/vite build` then `deno desktop -P --hmr --backend webview server/main.ts` |
| `deno task pack` | same UI build then `deno desktop -P --backend webview --include dist server/main.ts` |
| `deno task ui:dev` | `./node_modules/.bin/vite --open` (port 1420) |
| `deno task serve` | Headless `Deno.serve()` on 127.0.0.1 (same handler as the app) |
| `deno task test` | `deno test -P` |

The surface is strictly read-only: unsupported methods/routes are rejected, and
the daemon is never started or stopped.
