"""Interactive decision dashboard interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from trader.infrastructure.state_db.decision_dashboard_state import load_decision_dashboard_rows

DEFAULT_STATE_DIR = Path("state")
DEFAULT_OUT_HTML = DEFAULT_STATE_DIR / "decisions_dashboard.html"


@dataclass(frozen=True)
class DecisionDashboardResult:
    rows_count: int
    html_path: Path


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


def render_dashboard(
    rows: list[dict[str, object]],
    *,
    out_html: Path = DEFAULT_OUT_HTML,
) -> DecisionDashboardResult:
    """Render the interactive decision Perspective dashboard."""
    out_html.write_text(
        _HTML.format(data=json.dumps(rows, ensure_ascii=False, default=str)),
        encoding="utf-8",
    )
    return DecisionDashboardResult(rows_count=len(rows), html_path=out_html)


def build_and_render(
    state_dir: Path = DEFAULT_STATE_DIR,
    *,
    out_html: Path = DEFAULT_OUT_HTML,
) -> DecisionDashboardResult:
    """Load decision rows and render the interactive dashboard."""
    return render_dashboard(load_decision_dashboard_rows(state_dir), out_html=out_html)


def main() -> None:
    result = build_and_render()
    print(f"OK — {result.rows_count} décisions → {result.html_path}  ({result.html_path.stat().st_size // 1024} Ko)")
    print(f"Ouvre : open {result.html_path}")


if __name__ == "__main__":
    main()
