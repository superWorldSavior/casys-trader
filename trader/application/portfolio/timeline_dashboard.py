"""Portfolio timeline dashboard application projection."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

from trader.domain.portfolio.timeline import ExposurePoint, FillLike, exposure_timeline


@dataclass(frozen=True)
class PortfolioTimelineProjection:
    """Rows required by the portfolio timeline dashboard."""

    allocation_rows: list[dict[str, object]]
    equity_rows: list[dict[str, object]]
    fills_count: int
    fill_source: str


def allocation_timeline_rows(points: list[ExposurePoint]) -> list[dict[str, object]]:
    """Convert exposure points into dashboard rows with per-timestamp totals."""
    totals: dict[str, float] = defaultdict(float)
    for point in points:
        totals[point.ts] += point.notional

    rows: list[dict[str, object]] = []
    for idx, point in enumerate(points):
        total = totals[point.ts]
        row = asdict(point)
        row["row_id"] = idx
        row["jour"] = point.ts[:10]
        row["total_notional"] = total
        row["notional_pct"] = point.notional / total * 100.0 if total else 0.0
        rows.append(row)
    return rows


def build_portfolio_timeline_projection(
    fills: Iterable[FillLike],
    equity_rows: list[dict[str, object]],
    sym2family: Mapping[str, str],
    *,
    fill_source: str,
) -> PortfolioTimelineProjection:
    """Project fills and equity snapshots into timeline dashboard rows."""
    fills_list = list(fills)
    points = exposure_timeline(fills_list, sym2family)
    if not points:
        raise ValueError("aucune exposition reconstruite depuis les fills")
    if not equity_rows:
        raise ValueError("aucun snapshot equity fourni")

    return PortfolioTimelineProjection(
        allocation_rows=allocation_timeline_rows(points),
        equity_rows=equity_rows,
        fills_count=len(fills_list),
        fill_source=fill_source,
    )
