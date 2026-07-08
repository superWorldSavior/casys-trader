"""Reference-volatility helpers for resolving volatility-based plans."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

from trader.domain.market.features import build_indicator_snapshot

IndicatorSnapshotBuilder = Callable[..., Mapping[str, object]]


def positive_finite_float(raw: object) -> float | None:
    """Return raw as a strictly positive finite float, otherwise None."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def cockpit_vol_fraction(cockpit: dict, symbol: str) -> float | None:
    """Read the symbol volatility fraction from cockpit rows, preferring daily vol."""
    cols = cockpit.get("cols")
    rows = cockpit.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return None
    try:
        symbol_index = cols.index("s")
    except ValueError:
        return None
    vol_indices = []
    for column in ("vol_d", "vol"):
        try:
            vol_indices.append(cols.index(column))
        except ValueError:
            continue
    if not vol_indices:
        return None
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) <= symbol_index:
            continue
        if row[symbol_index] == symbol:
            for vol_index in vol_indices:
                if len(row) <= vol_index:
                    continue
                value = positive_finite_float(row[vol_index])
                if value is not None:
                    return value
            return None
    return None


def feature_vol_fraction(
    symbol: str,
    tradable_bars_by_symbol: dict[str, list],
    *,
    indicator_snapshot_builder: IndicatorSnapshotBuilder = build_indicator_snapshot,
) -> float | None:
    """Compute the symbol volatility fraction from indicator features."""
    if symbol not in tradable_bars_by_symbol:
        return None
    snapshot = indicator_snapshot_builder(
        tradable_bars_by_symbol,
        symbols=[symbol],
        names=["volatility"],
        window=48,
    )
    item = snapshot.get(symbol, {})
    indicators = item.get("indicators") if isinstance(item, dict) else None
    if not isinstance(indicators, dict):
        return None
    return positive_finite_float(indicators.get("volatility"))


def reference_volatility_for_symbol(
    symbol: str,
    *,
    entry_price: float,
    cockpit: dict,
    tradable_bars_by_symbol: dict[str, list],
    indicator_snapshot_builder: IndicatorSnapshotBuilder = build_indicator_snapshot,
) -> float | None:
    """Return absolute reference volatility for a symbol entry price."""
    vol_fraction = cockpit_vol_fraction(cockpit, symbol)
    if vol_fraction is None:
        vol_fraction = feature_vol_fraction(
            symbol,
            tradable_bars_by_symbol,
            indicator_snapshot_builder=indicator_snapshot_builder,
        )
    price = positive_finite_float(entry_price)
    if price is None or vol_fraction is None:
        return None
    return price * vol_fraction
