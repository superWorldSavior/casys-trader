"""Static portfolio allocation dashboard interface."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import squarify

from trader.application.portfolio.allocation_dashboard import build_allocation_dashboard_rows
from trader.domain.portfolio.allocation import AllocationRow, notional_by
from trader.infrastructure.state_db.portfolio_dashboard_state import load_open_positions, symbol_family_map

DEFAULT_STATE_DIR = Path("state")
DEFAULT_OUT_PNG = DEFAULT_STATE_DIR / "charts" / "04_allocation_treemap.png"
DEFAULT_OUT_HTML = DEFAULT_STATE_DIR / "allocation_dashboard.html"

_BG, _FG = "#0f1115", "#e8c07d"
_REGION_COLOR = {"EU": "#4e79c7", "US": "#59a14f", "TW": "#e15759", "AUTRE": "#8c8c8c"}


@dataclass(frozen=True)
class AllocationDashboardResult:
    positions_count: int
    total_notional: float
    png_path: Path
    html_path: Path


def _treemap_png(rows: Sequence[AllocationRow], out_png: Path) -> None:
    by_family = notional_by(rows, "region", "family")
    items = sorted(by_family.items(), key=lambda kv: -kv[1])
    sizes = [value for _, value in items]
    colors = [_REGION_COLOR.get(region, "#8c8c8c") for (region, _family), _value in items]
    total = sum(sizes)
    labels = [f"{family}\n{value / total * 100:.0f}%" for (_region, family), value in items]

    fig, ax = plt.subplots(figsize=(13, 8))
    fig.patch.set_facecolor(_BG)
    squarify.plot(
        sizes=sizes,
        label=labels,
        color=colors,
        ax=ax,
        text_kwargs={"color": "white", "fontsize": 8},
        pad=True,
        bar_kwargs={"edgecolor": _BG, "linewidth": 2},
    )
    ax.axis("off")
    ax.set_title(
        f"Allocation du portefeuille — notional {total:,.0f} (couleur = région)",
        color=_FG,
        fontweight="bold",
        fontsize=13,
    )
    by_region = notional_by(rows, "region")
    handles = [
        mpatches.Patch(
            color=_REGION_COLOR.get(region, "#8c8c8c"),
            label=f"{region}  {value / total * 100:.0f}%",
        )
        for (region,), value in sorted(by_region.items(), key=lambda kv: -kv[1])
    ]
    ax.legend(
        handles=handles,
        loc="upper right",
        facecolor=_BG,
        edgecolor="#333",
        labelcolor=_FG,
        framealpha=0.9,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)


_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>casys — allocation</title>
<link rel="stylesheet" crossorigin="anonymous"
  href="https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/css/themes.css" />
<style>html,body{{margin:0;height:100%;background:#0f1115}}
perspective-viewer{{position:absolute;inset:34px 0 0 0}}
#h{{display:flex;align-items:center;gap:12px;color:#e8c07d;padding:8px 14px;font:600 13px system-ui}}
#h span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
#h a{{margin-left:auto;color:#e9dcc5;border:1px solid #27303a;padding:4px 8px;border-radius:6px;
text-decoration:none;background:#161b23;font:600 12px system-ui}}</style></head>
<body>
<div id="h"><span>casys · allocation portefeuille — treemap région › famille › symbole (taille = notional)</span><a href="portfolio_timeline.html">Temps</a></div>
<perspective-viewer id="v" theme="Pro Dark"></perspective-viewer>
<script type="module">
import perspective from "https://cdn.jsdelivr.net/npm/@finos/perspective@3/dist/cdn/perspective.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/cdn/perspective-viewer.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer-datagrid@3/dist/cdn/perspective-viewer-datagrid.js";
import "https://cdn.jsdelivr.net/npm/@finos/perspective-viewer-d3fc@3/dist/cdn/perspective-viewer-d3fc.js";
const DATA = {data};
const worker = await perspective.worker();
const table = await worker.table(DATA);
const v = document.getElementById("v");
await v.load(table);
await v.restore({{ plugin:"Treemap", group_by:["region","family","symbol"], split_by:[],
  columns:["notional"], aggregates:{{notional:"sum"}}, settings:true }});
</script></body></html>
"""


def render_dashboard(
    rows: Sequence[AllocationRow],
    *,
    out_png: Path = DEFAULT_OUT_PNG,
    out_html: Path = DEFAULT_OUT_HTML,
) -> AllocationDashboardResult:
    """Render the current allocation treemap PNG and Perspective HTML."""
    if not rows:
        raise ValueError("aucune position ouverte")

    _treemap_png(rows, out_png)
    out_html.write_text(
        _HTML.format(data=json.dumps([asdict(row) for row in rows])),
        encoding="utf-8",
    )
    return AllocationDashboardResult(
        positions_count=len(rows),
        total_notional=sum(row.notional for row in rows),
        png_path=out_png,
        html_path=out_html,
    )


def build_and_render(
    state_dir: Path = DEFAULT_STATE_DIR,
    *,
    out_png: Path = DEFAULT_OUT_PNG,
    out_html: Path = DEFAULT_OUT_HTML,
) -> AllocationDashboardResult:
    """Load portfolio state, build the projection and render the dashboard."""
    rows = build_allocation_dashboard_rows(load_open_positions(state_dir), symbol_family_map())
    return render_dashboard(rows, out_png=out_png, out_html=out_html)


def main() -> None:
    result = build_and_render()
    print(f"OK — {result.positions_count} positions, notional {result.total_notional:,.0f}")
    print(f"  PNG  : {result.png_path}")
    print(f"  HTML : {result.html_path}  (open pour le drill-down interactif)")


if __name__ == "__main__":
    main()
