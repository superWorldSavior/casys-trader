"""Static dashboard index interface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = Path("state")
DEFAULT_OUT_HTML = DEFAULT_STATE_DIR / "dashboards.html"


@dataclass(frozen=True)
class DashboardIndexResult:
    html_path: Path


_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>casys — dashboards</title>
<style>
:root {{
  --bg:#0f1115; --panel:#141821; --line:#27303a; --fg:#e9dcc5; --muted:#94a0ad; --accent:#e8c07d;
}}
html,body {{ margin:0; min-height:100%; background:var(--bg); color:var(--fg);
  font-family:Avenir Next, Segoe UI, sans-serif; letter-spacing:0; }}
main {{ width:min(980px, calc(100vw - 40px)); margin:0 auto; padding:38px 0; }}
h1 {{ margin:0 0 6px; color:var(--accent); font-size:24px; font-weight:750; }}
p {{ margin:0 0 24px; color:var(--muted); font-size:13px; }}
.grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }}
a {{ display:block; min-height:96px; padding:16px; border:1px solid var(--line); border-radius:6px;
  background:var(--panel); color:var(--fg); text-decoration:none; }}
a strong {{ display:block; margin-bottom:8px; font-size:15px; }}
a span {{ color:var(--muted); font-size:12px; line-height:1.45; }}
@media (max-width: 760px) {{ .grid {{ grid-template-columns:1fr; }} main {{ width:calc(100vw - 24px); padding:24px 0; }} }}
</style></head>
<body><main>
<h1>casys · dashboards</h1>
<p>Vues locales générées depuis l'état courant du trader.</p>
<div class="grid">
  <a href="portfolio_timeline.html"><strong>Portefeuille · temps</strong><span>Equité, allocation empilée par région et par famille.</span></a>
  <a href="allocation_dashboard.html"><strong>Portefeuille · snapshot</strong><span>Treemap région › famille › symbole.</span></a>
  <a href="decisions_dashboard.html"><strong>Décisions · interactif</strong><span>Perspective drag-drop sur le journal des décisions.</span></a>
  <a href="charts/01_allocation_familles.png"><strong>Décisions · PNG</strong><span>Charts statiques: attention, trades par famille, trades par jour.</span></a>
</div>
</main></body></html>
"""


def render_index(*, out_html: Path = DEFAULT_OUT_HTML) -> DashboardIndexResult:
    """Render the static dashboard hub."""
    out_html.write_text(_HTML, encoding="utf-8")
    return DashboardIndexResult(html_path=out_html)


def main() -> None:
    result = render_index()
    print(f"OK — index dashboards → {result.html_path}")
    print(f"Ouvre : open {result.html_path}")


if __name__ == "__main__":
    main()
