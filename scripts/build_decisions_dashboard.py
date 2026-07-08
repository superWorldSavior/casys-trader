#!/usr/bin/env python3
"""Génère un dashboard NO-CODE (drag-drop) des décisions — FINOS Perspective.

Aplatit `state/decisions.jsonl` (DuckDB) en une table analytique + la famille de
chaque symbole, puis produit un HTML autoportant : pivot table + graphes à la
souris (grouper par famille, filtrer par date/modèle, croiser avec les news),
zéro SQL, zéro serveur. Perspective se charge depuis un CDN (WASM) au 1er ouvre.

    uv run python scripts/build_decisions_dashboard.py
    open state/decisions_dashboard.html
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb

from trader.domain.semantic import catalog

_JOURNAL = Path("state/decisions.jsonl")
_OUT = Path("state/decisions_dashboard.html")

_FLATTEN = """
SELECT
    cycle_ts,
    cast(cycle_ts AS DATE)::VARCHAR                                   AS jour,
    symbol, action, intent,
    qty, confidence, price, executed, model_called,
    decision_reason_code, decision_source, llm_model,
    news.news_count                                                  AS news_count,
    news.earnings_in_h                                               AS earnings_in_h,
    news.news_coverage                                               AS news_coverage,
    coalesce(market_snapshot.stale_market_data.stale_reason, 'frais') AS data_state,
    portfolio_snapshot.equity                                        AS equity,
    portfolio_snapshot.total_return_pct                              AS total_return_pct
FROM read_json_auto(?, format='newline_delimited', union_by_name=true,
                    maximum_object_size=33554432)
"""


def _sym2fam() -> dict[str, str]:
    fam = getattr(catalog, "FAMILIES", {}) or {}
    return {s: f for f, syms in fam.items() for s in syms}


def _rows() -> list[dict]:
    con = duckdb.connect()
    cur = con.execute(_FLATTEN, [_JOURNAL.as_posix()])
    cols = [d[0] for d in cur.description]
    sym2fam = _sym2fam()
    out = []
    for values in cur.fetchall():
        row = dict(zip(cols, values))
        row["family"] = sym2fam.get(row["symbol"], "?inconnu?")
        for k, v in list(row.items()):
            if isinstance(v, (dt.datetime, dt.date)):
                row[k] = v.isoformat()
        out.append(row)
    return out


_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>casys — décisions</title>
<link rel="stylesheet" crossorigin="anonymous"
  href="https://cdn.jsdelivr.net/npm/@finos/perspective-viewer@3/dist/css/themes.css" />
<style>
  html,body{{margin:0;height:100%;background:#0f1115;font-family:system-ui}}
  #hdr{{color:#e8c07d;padding:8px 14px;font:600 14px system-ui;letter-spacing:.02em}}
  perspective-viewer{{position:absolute;top:38px;left:0;right:0;bottom:0}}
</style></head>
<body>
<div id="hdr">casys · analyse des décisions — glisse une colonne dans « Group By » / « Split By », filtre, change de graphe. Aucun SQL.</div>
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
await v.restore({{
  plugin: "Datagrid",
  group_by: ["family"],
  split_by: ["action"],
  columns: ["symbol", "confidence", "news_count"],
  aggregates: {{ symbol: "count", confidence: "avg", news_count: "avg" }},
  sort: [["symbol", "desc"]],
  settings: true
}});
</script>
</body></html>
"""


def main() -> None:
    if not _JOURNAL.is_file():
        raise SystemExit(f"journal introuvable : {_JOURNAL} (lancer depuis la racine du repo)")
    rows = _rows()
    html = _HTML.format(data=json.dumps(rows, ensure_ascii=False, default=str))
    _OUT.write_text(html, encoding="utf-8")
    print(f"OK — {len(rows)} décisions → {_OUT}  ({_OUT.stat().st_size // 1024} Ko)")
    print(f"Ouvre : open {_OUT}")


if __name__ == "__main__":
    main()
