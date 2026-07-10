"""Pure live portfolio valuation and exposure aggregates."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable


def safe_last_price(raw: float | None, avg_price: float) -> float:
    """Fall back to cost when the latest market price cannot value a holding."""
    if raw is None or not math.isfinite(raw) or raw <= 0.0:
        return avg_price
    return raw


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    avg_price: float
    last_price: float
    fx_rate: float = field(default=1.0)

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price * self.fx_rate

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.quantity * self.fx_rate


@dataclass(frozen=True)
class Snapshot:
    cash: float
    holdings: list[Holding]
    starting_equity: float

    @property
    def positions_value(self) -> float:
        return sum(holding.market_value for holding in self.holdings)

    @property
    def long_exposure(self) -> float:
        return sum(holding.market_value for holding in self.holdings if holding.market_value > 0.0)

    @property
    def short_exposure(self) -> float:
        return sum(abs(holding.market_value) for holding in self.holdings if holding.market_value < 0.0)

    @property
    def gross_exposure(self) -> float:
        return self.long_exposure + self.short_exposure

    @property
    def net_exposure(self) -> float:
        return self.long_exposure - self.short_exposure

    @property
    def cash_available(self) -> float:
        return self.cash - self.short_exposure

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def total_return(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return self.equity / self.starting_equity - 1.0

    def as_context(
        self,
        *,
        fee_estimator: Callable[[str, float, float, float], float | None] | None = None,
    ) -> dict[str, object]:
        """Return the JSON-serializable portfolio context exposed to the agent."""
        holdings: list[dict[str, object]] = []
        for holding in self.holdings:
            unrealized_pnl = round(holding.unrealized_pnl, 2)
            item: dict[str, object] = {
                "symbol": holding.symbol,
                "quantity": holding.quantity,
                "avg_price": round(holding.avg_price, 4),
                "last_price": round(holding.last_price, 4),
                "unrealized_pnl": unrealized_pnl,
                "fx_rate": holding.fx_rate,
            }
            if fee_estimator is not None:
                round_trip_fee = fee_estimator(
                    holding.symbol,
                    holding.quantity,
                    holding.avg_price,
                    holding.last_price,
                )
                if round_trip_fee is not None:
                    round_trip_fee_usd = round_trip_fee * holding.fx_rate
                    item["round_trip_fee"] = round_trip_fee_usd
                    item["unrealized_pnl_net"] = round(
                        unrealized_pnl - round_trip_fee_usd,
                        2,
                    )
            holdings.append(item)
        return {
            "cash": round(self.cash, 2),
            "cash_ledger": round(self.cash, 2),
            "cash_available": round(self.cash_available, 2),
            "equity": round(self.equity, 2),
            "total_return_pct": round(self.total_return * 100, 4),
            "long_exposure_usd": round(self.long_exposure, 2),
            "short_exposure_usd": round(self.short_exposure, 2),
            "gross_exposure_usd": round(self.gross_exposure, 2),
            "net_exposure_usd": round(self.net_exposure, 2),
            "holdings": holdings,
        }


__all__ = ["Holding", "Snapshot", "safe_last_price"]
