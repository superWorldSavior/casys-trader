"""portfolio — vue agrégée : positions valorisées, équité, PnL, KPI.

S'appuie sur un Broker (positions/cash) + une source de prix. Aucune décision ;
fournit le contexte chiffré que l'agent lit à chaque réveil pour piloter ses KPI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .execution import Broker


def _safe_last_price(raw: float | None, avg_price: float) -> float:
    """Backstop valorisation : un prix absent/aberrant (None, NaN, inf, <=0) ne
    doit JAMAIS valoriser une position détenue à $0. On retombe sur le coût
    (avg_price) → unrealized=0 le temps qu'un vrai prix revienne, au lieu d'une
    fausse falaise d'équité (cf. STMN.SW 30/06 : prix 0 → équité -8 k un cycle).
    Côté daemon, price_of = `prices.get(s, 0.0)` : un symbole non coté ce cycle
    arrive donc ici à 0.0."""
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
        return sum(h.market_value for h in self.holdings)

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
    ) -> dict:
        """Contexte JSON-serializable destiné au prompt Codex."""
        holdings: list[dict] = []
        for h in self.holdings:
            unrealized_pnl = round(h.unrealized_pnl, 2)
            item = {
                "symbol": h.symbol,
                "quantity": h.quantity,
                "avg_price": round(h.avg_price, 4),
                "last_price": round(h.last_price, 4),
                "unrealized_pnl": unrealized_pnl,
                "fx_rate": h.fx_rate,
            }
            if fee_estimator is not None:
                round_trip_fee = fee_estimator(
                    h.symbol,
                    h.quantity,
                    h.avg_price,
                    h.last_price,
                )
                if round_trip_fee is not None:
                    round_trip_fee_usd = round_trip_fee * h.fx_rate
                    item["round_trip_fee"] = round_trip_fee_usd
                    item["unrealized_pnl_net"] = round(
                        unrealized_pnl - round_trip_fee_usd,
                        2,
                    )
            holdings.append(item)
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity, 2),
            "total_return_pct": round(self.total_return * 100, 4),
            "holdings": holdings,
        }


def snapshot(
    broker: Broker,
    price_of: Callable[[str], float],
    starting_equity: float,
    fx_rate_of: Callable[[str], float] | None = None,
) -> Snapshot:
    """price_of(symbol) -> dernier prix. Injecté pour rester testable/déterministe.

    fx_rate_of(symbol) -> taux USD/native (1.0 par défaut = USD natif).
    """
    holdings = [
        Holding(
            symbol=p.symbol,
            quantity=p.quantity,
            avg_price=p.avg_price,
            last_price=_safe_last_price(price_of(p.symbol), p.avg_price),
            fx_rate=fx_rate_of(p.symbol) if fx_rate_of is not None else 1.0,
        )
        for p in broker.positions().values()
    ]
    return Snapshot(cash=broker.cash(), holdings=holdings, starting_equity=starting_equity)
