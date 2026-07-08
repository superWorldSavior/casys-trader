#!/usr/bin/env python3
"""Vue d'allocation du portefeuille : région › famille › symbole par notional.

Orchestration seule (CLEAN) : lit les positions via le store infra, délègue le
calcul au domaine pur (`trader.domain.portfolio.allocation`), rend un treemap PNG
(part-of-whole = « taille du book vs répartition ») + un dashboard Perspective
interactif (drill-down région → famille → symbole).

    uv run python scripts/render_allocation.py
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import squarify

from trader.domain.portfolio.allocation import allocation_rows, notional_by
from trader.domain.semantic import catalog
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db

_DB = Path("state/casys.db")
_OUT_PNG = Path("state/charts/04_allocation_treemap.png")
_OUT_HTML = Path("state/allocation_dashboard.html")
_BG, _FG = "#0f1115", "#e8c07d"
_REGION_COLOR = {"EU": "#4e79c7", "US": "#59a14f", "TW": "#e15759", "AUTRE": "#8c8c8c"}


def _load_rows() -> list:
    """Frontière I/O : positions via le store, mapping famille via le catalog."""
    broker = SqliteBroker(open_state_db(_DB))
    positions = broker.positions().values()
    sym2fam = {s: f for f, syms in catalog.FAMILIES.items() for s in syms}
    return allocation_rows(positions, sym2fam)


def _treemap_png(rows: list) -> None:
    by_family = notional_by(rows, "region", "family")
    items = sorted(by_family.items(), key=lambda kv: -kv[1])
    sizes = [v for _, v in items]
    colors = [_REGION_COLOR.get(region, "#8c8c8c") for (region, _), _ in items]
    total = sum(sizes)
    labels = [f"{fam}\n{v / total * 100:.0f}%" for (region, fam), v in items]

    fig, ax = plt.subplots(figsize=(13, 8))
    fig.patch.set_facecolor(_BG)
    squarify.plot(sizes=sizes, label=labels, color=colors, ax=ax,
                  text_kwargs={"color": "white", "fontsize": 8},
                  pad=True, bar_kwargs={"edgecolor": _BG, "linewidth": 2})
    ax.axis("off")
    ax.set_title(f"Allocation du portefeuille — notional {total:,.0f} (couleur = région)",
                 color=_FG, fontweight="bold", fontsize=13)
    by_region = notional_by(rows, "region")
    handles = [mpatches.Patch(color=_REGION_COLOR.get(r, "#8c8c8c"),
                              label=f"{r}  {v / total * 100:.0f}%")
               for r, (rk, v) in ((k[0], (k, val)) for k, val in
                                  sorted(by_region.items(), key=lambda kv: -kv[1]))]
    ax.legend(handles=handles, loc="upper right", facecolor=_BG,
              edgecolor="#333", labelcolor=_FG, framealpha=0.9)
    fig.savefig(_OUT_PNG, dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)


_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>casys — allocation</title>
<link rel="stylesheet" crossorigin="anonymous"
  href="https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/css/themes.css" />
<style>html,body{{margin:0;height:100%;background:#0f1115}}
perspective-viewer{{position:absolute;inset:34px 0 0 0}}
#h{{color:#e8c07d;padding:8px 14px;font:600 13px system-ui}}</style></head>
<body>
<div id="h">casys · allocation portefeuille — treemap région › famille › symbole (taille = notional). Glisse, filtre, plonge.</div>
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


def main() -> None:
    if not _DB.is_file():
        raise SystemExit(f"base introuvable : {_DB} (lancer depuis la racine du repo)")
    rows = _load_rows()
    if not rows:
        raise SystemExit("aucune position ouverte")
    _OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    _treemap_png(rows)
    _OUT_HTML.write_text(_HTML.format(data=json.dumps([asdict(r) for r in rows])), encoding="utf-8")
    total = sum(r.notional for r in rows)
    print(f"OK — {len(rows)} positions, notional {total:,.0f}")
    print(f"  PNG  : {_OUT_PNG}")
    print(f"  HTML : {_OUT_HTML}  (open pour le drill-down interactif)")


if __name__ == "__main__":
    main()
