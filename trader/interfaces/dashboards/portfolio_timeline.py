"""Static portfolio timeline dashboard interface."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from trader.application.portfolio.timeline_dashboard import PortfolioTimelineProjection, build_portfolio_timeline_projection
from trader.infrastructure.state_db.portfolio_dashboard_state import load_equity_rows, load_fills, symbol_family_map

DEFAULT_STATE_DIR = Path("state")
DEFAULT_OUT_HTML = DEFAULT_STATE_DIR / "portfolio_timeline.html"
DEFAULT_OUT_PNG = DEFAULT_STATE_DIR / "charts" / "05_allocation_timeline_regions.png"

_BG = "#0f1115"
_PANEL = "#141821"
_GRID = "#2a303a"
_FG = "#e9dcc5"
_ACCENT = "#e8c07d"
_REGION_COLOR = {"EU": "#4e79c7", "US": "#59a14f", "TW": "#e15759", "AUTRE": "#8c8c8c"}
_REGION_ORDER = {"US": 0, "EU": 1, "TW": 2, "AUTRE": 3}


@dataclass(frozen=True)
class PortfolioTimelineDashboardResult:
    allocation_points: int
    equity_points: int
    fills_count: int
    png_path: Path
    html_path: Path


def _parse_ts(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _timeline_png(rows: list[dict[str, object]], out_png: Path) -> None:
    by_ts_region: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        by_ts_region[str(row["ts"])][str(row["region"])] += float(row["notional"])

    times = sorted(by_ts_region)
    regions = sorted(
        {region for values in by_ts_region.values() for region in values},
        key=lambda region: (_REGION_ORDER.get(region, 99), region),
    )
    xs = [_parse_ts(ts) for ts in times]
    ys = [[by_ts_region[ts].get(region, 0.0) for ts in times] for region in regions]

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_PANEL)
    ax.stackplot(
        xs,
        ys,
        labels=regions,
        colors=[_REGION_COLOR.get(region, "#8c8c8c") for region in regions],
        alpha=0.88,
    )
    ax.set_title(
        "Allocation portefeuille dans le temps — notional USD approx. par region",
        color=_ACCENT,
        fontsize=13,
    )
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value / 1000:.0f}k"))
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    ax.tick_params(colors=_FG, labelsize=9)
    ax.grid(color=_GRID, linewidth=0.8, alpha=0.7)
    for spine in ax.spines.values():
        spine.set_color(_GRID)
    ax.legend(loc="upper left", facecolor=_BG, edgecolor=_GRID, labelcolor=_FG, framealpha=0.9)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=135, facecolor=_BG)
    plt.close(fig)


_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>casys — portefeuille temps</title>
<link rel="stylesheet" crossorigin="anonymous"
  href="https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/css/themes.css" />
<style>
:root {
  --bg:#0f1115; --panel:#141821; --panel2:#10131a; --line:#27303a;
  --fg:#e9dcc5; --muted:#94a0ad; --accent:#e8c07d;
}
html,body { margin:0; height:100%; background:var(--bg); color:var(--fg);
  font-family:Avenir Next, Segoe UI, sans-serif; letter-spacing:0; }
body { display:grid; grid-template-rows:44px minmax(220px,30vh) 1fr; overflow:hidden; }
header { display:flex; align-items:center; gap:16px; padding:0 14px; border-bottom:1px solid var(--line);
  background:#11151c; min-width:0; }
header strong { color:var(--accent); font-size:14px; white-space:nowrap; }
header span { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
header nav { margin-left:auto; display:flex; gap:8px; }
header a { color:var(--fg); border:1px solid var(--line); padding:5px 9px; border-radius:6px;
  text-decoration:none; font-size:12px; background:#161b23; }
section { position:relative; min-height:0; background:var(--panel); }
.equity { border-bottom:1px solid var(--line); }
.grid { display:grid; grid-template-columns:minmax(0,0.72fr) minmax(0,1.28fr); min-height:0; }
.grid section:first-child { border-right:1px solid var(--line); }
h2 { position:absolute; z-index:2; top:8px; left:12px; margin:0; color:var(--fg);
  font-size:12px; font-weight:700; letter-spacing:.02em; pointer-events:none; }
perspective-viewer { position:absolute; inset:28px 0 0 0; --d3fc-gridline-color:var(--line); }
@media (max-width: 900px) {
  body { grid-template-rows:50px minmax(210px,30vh) 1fr; }
  header { gap:10px; padding:0 10px; }
  header nav { display:none; }
  .grid { grid-template-columns:1fr; grid-template-rows:1fr 1fr; }
  .grid section:first-child { border-right:0; border-bottom:1px solid var(--line); }
}
</style></head>
<body>
<header>
  <strong>casys · portefeuille temps</strong>
  <span>__META__</span>
  <nav><a href="allocation_dashboard.html">Treemap</a><a href="decisions_dashboard.html">Decisions</a></nav>
</header>
<section class="equity"><h2>Equite</h2><perspective-viewer id="equity" theme="Pro Dark"></perspective-viewer></section>
<div class="grid">
  <section><h2>Allocation USD approx. · regions</h2><perspective-viewer id="regions" theme="Pro Dark"></perspective-viewer></section>
  <section><h2>Allocation USD approx. · familles</h2><perspective-viewer id="families" theme="Pro Dark"></perspective-viewer></section>
</div>
<script type="module">
import perspective from "https://cdn.jsdelivr.net/npm/@finos/perspective@3/dist/cdn/perspective.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/cdn/perspective-viewer.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer-datagrid@3/dist/cdn/perspective-viewer-datagrid.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer-d3fc@3/dist/cdn/perspective-viewer-d3fc.js";

const ALLOCATION = __ALLOCATION_DATA__;
const EQUITY = __EQUITY_DATA__;
const worker = await perspective.worker();
const allocationTable = await worker.table(ALLOCATION);
const equityTable = await worker.table(EQUITY);

const equity = document.getElementById("equity");
await equity.load(equityTable);
await equity.restore({
  plugin: "Y Line",
  group_by: ["ts"],
  columns: ["equity"],
  aggregates: { equity: "avg", total_return_pct: "avg" },
  sort: [["ts", "asc"]],
  settings: true
});

const regions = document.getElementById("regions");
await regions.load(allocationTable);
await regions.restore({
  plugin: "Y Area",
  group_by: ["ts"],
  split_by: ["region"],
  columns: ["notional"],
  aggregates: { notional: "sum", total_notional: "avg", notional_pct: "sum" },
  sort: [["ts", "asc"]],
  settings: true
});

const families = document.getElementById("families");
await families.load(allocationTable);
await families.restore({
  plugin: "Y Area",
  group_by: ["ts"],
  split_by: ["family"],
  columns: ["notional"],
  aggregates: { notional: "sum", total_notional: "avg", notional_pct: "sum" },
  sort: [["ts", "asc"]],
  settings: true
});
</script></body></html>
"""


def render_dashboard(
    projection: PortfolioTimelineProjection,
    *,
    out_png: Path = DEFAULT_OUT_PNG,
    out_html: Path = DEFAULT_OUT_HTML,
) -> PortfolioTimelineDashboardResult:
    """Render the timeline stacked-area PNG and Perspective HTML."""
    _timeline_png(projection.allocation_rows, out_png)
    meta = (
        f"{projection.fills_count} fills · {len(projection.allocation_rows)} points allocation · "
        f"{len(projection.equity_rows)} snapshots equity · source {projection.fill_source}"
    )
    html = (
        _HTML.replace("__META__", meta)
        .replace("__ALLOCATION_DATA__", json.dumps(projection.allocation_rows, ensure_ascii=False))
        .replace("__EQUITY_DATA__", json.dumps(projection.equity_rows, ensure_ascii=False, default=str))
    )
    out_html.write_text(html, encoding="utf-8")
    return PortfolioTimelineDashboardResult(
        allocation_points=len(projection.allocation_rows),
        equity_points=len(projection.equity_rows),
        fills_count=projection.fills_count,
        png_path=out_png,
        html_path=out_html,
    )


def build_and_render(
    state_dir: Path = DEFAULT_STATE_DIR,
    *,
    out_png: Path = DEFAULT_OUT_PNG,
    out_html: Path = DEFAULT_OUT_HTML,
) -> PortfolioTimelineDashboardResult:
    """Load portfolio state, build the projection and render the dashboard."""
    fills, fill_source = load_fills(state_dir)
    projection = build_portfolio_timeline_projection(
        fills,
        load_equity_rows(state_dir),
        symbol_family_map(),
        fill_source=fill_source,
    )
    return render_dashboard(projection, out_png=out_png, out_html=out_html)


def main() -> None:
    result = build_and_render()
    print(f"OK — {result.fills_count} fills -> {result.allocation_points} points allocation")
    print(f"  Equity : {result.equity_points} snapshots depuis {DEFAULT_STATE_DIR / 'decisions.jsonl'}")
    print(f"  PNG    : {result.png_path}")
    print(f"  HTML   : {result.html_path}")


if __name__ == "__main__":
    main()
