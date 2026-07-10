"""Application projection of commission models into decision fee signals."""

from __future__ import annotations

import math
from typing import Protocol

from trader.domain.contracts import Commission, Order


class CommissionCalculator(Protocol):
    def calculate(self, order: Order, price: float) -> Commission: ...


def round_trip_cost(
    model: CommissionCalculator | None,
    symbol: str,
    price: float | None,
    ref_notional: float,
) -> dict[str, float | str] | None:
    """Estimate round-trip fees and break-even basis points."""
    if (
        model is None
        or price is None
        or not math.isfinite(price)
        or price <= 0.0
        or not math.isfinite(ref_notional)
        or ref_notional <= 0.0
    ):
        return None
    quantity = ref_notional / price
    if quantity <= 0.0:
        return None
    one_way = model.calculate(Order(symbol=symbol, side="BUY", quantity=quantity), price)
    if one_way.model in {"ibkr_unknown", "ibkr_invalid_order"}:
        return None
    fee_rt = one_way.amount * 2.0
    be_bps = (fee_rt / ref_notional) * 10_000.0
    return {
        "fee_rt": round(fee_rt, 2),
        "currency": one_way.currency,
        "be_bps": round(be_bps, 2),
    }


__all__ = ["CommissionCalculator", "round_trip_cost"]
