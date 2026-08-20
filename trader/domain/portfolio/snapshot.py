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
        fee_estimator_cost_scope: str | None = None,
        fee_estimator_is_all_in: bool = False,
    ) -> dict[str, object]:
        """Return the JSON-serializable portfolio context exposed to the agent.

        A bare estimator callable carries no proof of what its number covers.
        Callers must pass ``fee_estimator_cost_scope`` to claim a precise scope;
        otherwise the projection remains explicitly unspecified and never
        upgrades an arbitrary number to ``broker_commission_only``.
        """
        explicit_cost_scope = str(fee_estimator_cost_scope or "").strip()
        cost_scope = explicit_cost_scope or "unspecified_transaction_cost_estimate"
        cost_estimate_is_all_in = (
            fee_estimator_is_all_in is True if explicit_cost_scope else False
        )
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
                if (
                    round_trip_fee is not None
                    and math.isfinite(round_trip_fee)
                    and round_trip_fee >= 0.0
                ):
                    round_trip_fee_usd = round_trip_fee * holding.fx_rate
                    after_modeled_costs = round(
                        unrealized_pnl - round_trip_fee_usd,
                        2,
                    )
                    # Canonical truth-explicit fields are neutral about what
                    # the injected estimator covers.  Broker-specific aliases
                    # are emitted only when the caller explicitly proves that
                    # scope through metadata.
                    item["round_trip_cost_estimate"] = round_trip_fee_usd
                    item["unrealized_pnl_after_modeled_costs"] = after_modeled_costs
                    item["transaction_cost_scope"] = cost_scope
                    item["transaction_cost_estimate_is_all_in"] = (
                        cost_estimate_is_all_in
                    )
                    if cost_scope == "broker_commission_only":
                        item["round_trip_broker_fee"] = round_trip_fee_usd
                        item["unrealized_pnl_after_broker_fees"] = after_modeled_costs

                    # Backward-compatible wire aliases.  Their adjacent scope
                    # field prevents legacy consumers from reading "net" as
                    # all-in while they migrate to the canonical names above.
                    item["round_trip_fee"] = round_trip_fee_usd
                    item["unrealized_pnl_net"] = after_modeled_costs
                    item["unrealized_pnl_net_scope"] = cost_scope
                    item["unrealized_pnl_net_is_all_in"] = cost_estimate_is_all_in
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
