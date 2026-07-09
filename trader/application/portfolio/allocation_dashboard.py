"""Portfolio allocation dashboard application projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from trader.domain.portfolio.allocation import AllocationRow, PositionLike, allocation_rows


def build_allocation_dashboard_rows(
    positions: Iterable[PositionLike],
    sym2family: Mapping[str, str],
) -> list[AllocationRow]:
    """Project open positions into allocation dashboard rows."""
    return allocation_rows(positions, sym2family)
