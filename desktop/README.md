# Casys Trader desktop

Read-only cockpit skeleton in the **browser**. It polls the Python read model via `/api/snapshot` and never writes runtime state.

```bash
make desktop
```

Opens http://127.0.0.1:1420/ — keep that Vite process running. Do not use Tauri for now (`make desktop-tauri` can freeze while it waits on a blocking snapshot).

The default landing page is **Today** — a general-audience daily briefing (headline, top stories, news wire, macro indicators, upcoming events, plain-language portfolio glance) backed by `/api/intelligence/briefing` and `/api/intelligence/news`. Intelligence pages (World / Regions / Companies) come first in the nav; operator pages (Overview desk, Portfolio, Decisions…) live under Agent / Ops. The Textual cockpit remains the operator UI.
