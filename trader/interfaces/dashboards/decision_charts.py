"""Static decision chart dashboard interface."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from trader.infrastructure.state_db.decision_dashboard_state import load_decision_chart_rows

DEFAULT_STATE_DIR = Path("state")
DEFAULT_OUT_DIR = DEFAULT_STATE_DIR / "charts"

_BG, _FG, _BUY, _SELL, _BAR = "#0f1115", "#e8c07d", "#4e79c7", "#d6863b", "#5b6b8c"

plt.rcParams.update(
    {
        "figure.facecolor": _BG,
        "axes.facecolor": _BG,
        "savefig.facecolor": _BG,
        "text.color": _FG,
        "axes.labelcolor": _FG,
        "xtick.color": _FG,
        "ytick.color": _FG,
        "axes.edgecolor": "#333",
        "font.size": 10,
    }
)


@dataclass(frozen=True)
class DecisionChartsResult:
    rows_count: int
    paths: tuple[Path, ...]


def _save(fig, out_dir: Path, name: str) -> Path:
    fig.tight_layout()
    path = out_dir / name
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_allocation(rows: list[dict[str, object]], out_dir: Path) -> Path:
    counts = Counter(str(row["family"]) for row in rows).most_common(20)
    families = [item[0] for item in counts][::-1]
    values = [item[1] for item in counts][::-1]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(families, values, color=_BAR)
    ax.set_title("Allocation d'attention — décisions par famille (top 20)", color=_FG, fontweight="bold")
    ax.set_xlabel("nombre de décisions")
    return _save(fig, out_dir, "01_allocation_familles.png")


def _stacked(ax, keys: list[str], buy: list[int], sell: list[int]) -> None:
    ax.bar(keys, buy, color=_BUY, label="BUY")
    ax.bar(keys, sell, bottom=buy, color=_SELL, label="SELL")
    ax.legend(facecolor=_BG, edgecolor="#333", labelcolor=_FG)


def chart_trades_by_family(rows: list[dict[str, object]], out_dir: Path) -> Path:
    counts: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        family = str(row["family"])
        if row["action"] == "BUY":
            counts[family][0] += 1
        elif row["action"] == "SELL":
            counts[family][1] += 1
    items = sorted(counts.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:18]
    families = [key for key, _value in items]
    buy = [value[0] for _key, value in items]
    sell = [value[1] for _key, value in items]
    fig, ax = plt.subplots(figsize=(11, 6))
    _stacked(ax, families, buy, sell)
    ax.set_title("Trades réels par famille (HOLD exclu) — BUY vs SELL", color=_FG, fontweight="bold")
    ax.set_ylabel("nombre de trades")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    return _save(fig, out_dir, "02_trades_par_famille.png")


def chart_trades_by_day(rows: list[dict[str, object]], out_dir: Path) -> Path:
    counts: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        day = str(row["jour"])
        if row["action"] == "BUY":
            counts[day][0] += 1
        elif row["action"] == "SELL":
            counts[day][1] += 1
    days = sorted(counts)
    buy = [counts[day][0] for day in days]
    sell = [counts[day][1] for day in days]
    fig, ax = plt.subplots(figsize=(10, 5))
    _stacked(ax, days, buy, sell)
    ax.set_title("Trades par jour — BUY vs SELL (week-end = marchés fermés)", color=_FG, fontweight="bold")
    ax.set_ylabel("nombre de trades")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    return _save(fig, out_dir, "03_trades_par_jour.png")


def render_charts(
    rows: list[dict[str, object]],
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> DecisionChartsResult:
    """Render static decision charts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chart_fns: tuple[Callable[[list[dict[str, object]], Path], Path], ...] = (
        chart_allocation,
        chart_trades_by_family,
        chart_trades_by_day,
    )
    paths = tuple(chart_fn(rows, out_dir) for chart_fn in chart_fns)
    return DecisionChartsResult(rows_count=len(rows), paths=paths)


def build_and_render(
    state_dir: Path = DEFAULT_STATE_DIR,
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> DecisionChartsResult:
    """Load decision rows and render static charts."""
    return render_charts(load_decision_chart_rows(state_dir), out_dir=out_dir)


def main() -> None:
    result = build_and_render()
    for path in result.paths:
        print("écrit", path)
    print(f"({result.rows_count} décisions)")


if __name__ == "__main__":
    main()
