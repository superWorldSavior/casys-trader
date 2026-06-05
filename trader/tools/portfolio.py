"""portfolio — vue agrégée : positions valorisées, équité, PnL, KPI.

S'appuie sur un Broker (positions/cash) + une source de prix. Aucune décision ;
fournit le contexte chiffré que l'agent lit à chaque réveil pour piloter ses KPI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .execution import Broker


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    avg_price: float
    last_price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.quantity


@dataclass(frozen=True)
class Snapshot:
    cash: float
    holdings: list[Holding]
    starting_equity: float

    @property
    def positions_value(self) -> float:
        return sum(h.market_value for h in self.holdings)

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def total_return(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return self.equity / self.starting_equity - 1.0

    def as_context(self) -> dict:
        """Contexte JSON-serializable destiné au prompt Codex."""
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity, 2),
            "total_return_pct": round(self.total_return * 100, 4),
            "holdings": [
                {
                    "symbol": h.symbol,
                    "quantity": h.quantity,
                    "avg_price": round(h.avg_price, 4),
                    "last_price": round(h.last_price, 4),
                    "unrealized_pnl": round(h.unrealized_pnl, 2),
                }
                for h in self.holdings
            ],
        }


def snapshot(broker: Broker, price_of: Callable[[str], float], starting_equity: float) -> Snapshot:
    """price_of(symbol) -> dernier prix. Injecté pour rester testable/déterministe."""
    holdings = [
        Holding(symbol=p.symbol, quantity=p.quantity, avg_price=p.avg_price, last_price=price_of(p.symbol))
        for p in broker.positions().values()
    ]
    return Snapshot(cash=broker.cash(), holdings=holdings, starting_equity=starting_equity)
