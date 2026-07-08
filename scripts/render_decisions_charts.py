#!/usr/bin/env python3
"""Rend les vues clés du dashboard décisions en PNG (partage/statique).

Complément « image » de `build_decisions_dashboard.py` (l'interactif) : mêmes
données (DuckDB sur decisions.jsonl + famille), sorties figées dans state/charts/.

    uv run python scripts/render_decisions_charts.py
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from trader.domain.semantic import catalog

_JOURNAL = Path("state/decisions.jsonl")
_OUT = Path("state/charts")
_BG, _FG, _BUY, _SELL, _BAR = "#0f1115", "#e8c07d", "#4e79c7", "#d6863b", "#5b6b8c"

plt.rcParams.update({
    "figure.facecolor": _BG, "axes.facecolor": _BG, "savefig.facecolor": _BG,
    "text.color": _FG, "axes.labelcolor": _FG, "xtick.color": _FG, "ytick.color": _FG,
    "axes.edgecolor": "#333", "font.size": 10,
})


def _rows() -> list[dict]:
    sym2fam = {s: f for f, syms in (getattr(catalog, "FAMILIES", {}) or {}).items() for s in syms}
    cur = duckdb.connect().execute(
        "SELECT symbol, action, cast(cycle_ts AS DATE)::VARCHAR AS jour "
        "FROM read_json_auto(?, format='newline_delimited', union_by_name=true, maximum_object_size=33554432)",
        [_JOURNAL.as_posix()],
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, v)) | {"family": sym2fam.get(v[0], "?")} for v in cur.fetchall()]


def _save(fig, name: str) -> Path:
    fig.tight_layout()
    p = _OUT / name
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p


def chart_allocation(rows: list[dict]) -> Path:
    c = Counter(r["family"] for r in rows).most_common(20)
    fams, vals = [x[0] for x in c][::-1], [x[1] for x in c][::-1]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(fams, vals, color=_BAR)
    ax.set_title("Allocation d'attention — décisions par famille (top 20)", color=_FG, fontweight="bold")
    ax.set_xlabel("nombre de décisions")
    return _save(fig, "01_allocation_familles.png")


def _stacked(ax, keys, buy, sell):
    ax.bar(keys, buy, color=_BUY, label="BUY")
    ax.bar(keys, sell, bottom=buy, color=_SELL, label="SELL")
    ax.legend(facecolor=_BG, edgecolor="#333", labelcolor=_FG)


def chart_trades_by_family(rows: list[dict]) -> Path:
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["action"] == "BUY": agg[r["family"]][0] += 1
        elif r["action"] == "SELL": agg[r["family"]][1] += 1
    items = sorted(agg.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:18]
    fams = [k for k, _ in items]
    buy = [v[0] for _, v in items]
    sell = [v[1] for _, v in items]
    fig, ax = plt.subplots(figsize=(11, 6))
    _stacked(ax, fams, buy, sell)
    ax.set_title("Trades réels par famille (HOLD exclu) — BUY vs SELL", color=_FG, fontweight="bold")
    ax.set_ylabel("nombre de trades")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    return _save(fig, "02_trades_par_famille.png")


def chart_trades_by_day(rows: list[dict]) -> Path:
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["action"] == "BUY": agg[r["jour"]][0] += 1
        elif r["action"] == "SELL": agg[r["jour"]][1] += 1
    days = sorted(agg)
    buy = [agg[d][0] for d in days]
    sell = [agg[d][1] for d in days]
    fig, ax = plt.subplots(figsize=(10, 5))
    _stacked(ax, days, buy, sell)
    ax.set_title("Trades par jour — BUY vs SELL (week-end = marchés fermés)", color=_FG, fontweight="bold")
    ax.set_ylabel("nombre de trades")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    return _save(fig, "03_trades_par_jour.png")


def main() -> None:
    if not _JOURNAL.is_file():
        raise SystemExit(f"journal introuvable : {_JOURNAL}")
    _OUT.mkdir(parents=True, exist_ok=True)
    rows = _rows()
    for fn in (chart_allocation, chart_trades_by_family, chart_trades_by_day):
        print("écrit", fn(rows))
    print(f"({len(rows)} décisions)")


if __name__ == "__main__":
    main()
