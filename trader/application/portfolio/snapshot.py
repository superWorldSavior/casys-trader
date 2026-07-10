"""Application service assembling a live portfolio snapshot."""

from __future__ import annotations

from typing import Callable, Protocol

from trader.domain.contracts import Position
from trader.domain.portfolio.snapshot import Holding, Snapshot, safe_last_price


class PortfolioReader(Protocol):
    def positions(self) -> dict[str, Position]: ...

    def cash(self) -> float: ...


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
            fx_rate=fx_rate_of(position.symbol) if fx_rate_of is not None else 1.0,
        )
        for position in broker.positions().values()
    ]
    return Snapshot(cash=broker.cash(), holdings=holdings, starting_equity=starting_equity)


__all__ = ["PortfolioReader", "snapshot"]
