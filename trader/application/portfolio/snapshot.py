"""Application service assembling a live portfolio snapshot."""

from __future__ import annotations

import math
from typing import Callable, Protocol

from trader.domain.contracts import Position
from trader.domain.portfolio.snapshot import Holding, Snapshot, safe_last_price


class PortfolioReader(Protocol):
    def positions(self) -> dict[str, Position]: ...

    def cash(self) -> float: ...


def _holding_fx_rate(
    fx_rate_of: Callable[[str], float] | None,
    symbol: str,
) -> float:
    if fx_rate_of is None:
        return 1.0
    try:
        rate = fx_rate_of(symbol)
    except Exception as exc:  # noqa: BLE001 — valuation must not crash the cycle
        if exc.__class__.__name__ != "MissingFxRate":
            raise
        return 0.0
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0.0:
        return 0.0
    return float(rate)


def snapshot(
    broker: PortfolioReader,
    price_of: Callable[[str], float],
    starting_equity: float,
    fx_rate_of: Callable[[str], float] | None = None,
) -> Snapshot:
    """Build a deterministic valuation from an injected broker and price reader."""
    holdings = [
        Holding(
            symbol=position.symbol,
            quantity=position.quantity,
            avg_price=position.avg_price,
            last_price=safe_last_price(price_of(position.symbol), position.avg_price),
            fx_rate=_holding_fx_rate(fx_rate_of, position.symbol),
        )
        for position in broker.positions().values()
    ]
    return Snapshot(cash=broker.cash(), holdings=holdings, starting_equity=starting_equity)


__all__ = ["PortfolioReader", "snapshot"]
