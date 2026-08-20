"""Application projection of commission models into decision fee signals."""

from __future__ import annotations

import math
from typing import Protocol

from trader.domain.contracts import Commission, Order
from trader.domain.market import fx


UNAVAILABLE_COMMISSION_MODELS = frozenset(
    {
        "ibkr_unknown",
        "ibkr_invalid_order",
        "ibkr_unpriced_venue",
    }
)


class CommissionCalculator(Protocol):
    def calculate(self, order: Order, price: float) -> Commission: ...


def round_trip_cost(
    model: CommissionCalculator | None,
    symbol: str,
    price: float | None,
    ref_notional: float,
    *,
    fx_rate: float | None = None,
) -> dict[str, float | str] | None:
    """Estimate round-trip fees for a reference notional expressed in USD.

    ``price`` and the returned ``fee_rt`` remain native.  ``fx_rate`` follows
    the project convention (USD per unit of the symbol quote currency) and is
    mandatory for a non-USD listing.  Break-even bps are computed on the USD
    reference notional, after converting the native commission back to USD.
    """
    if (
        model is None
        or price is None
        or not math.isfinite(price)
        or price <= 0.0
        or not math.isfinite(ref_notional)
        or ref_notional <= 0.0
    ):
        return None
    quote_currency = fx.currency_for(symbol)
    if quote_currency == fx.BASE_CCY:
        quote_fx_rate = 1.0
    elif (
        fx_rate is None
        or not math.isfinite(fx_rate)
        or fx_rate <= 0.0
    ):
        return None
    else:
        quote_fx_rate = float(fx_rate)

    native_notional = ref_notional / quote_fx_rate
    quantity = native_notional / price
    if quantity <= 0.0:
        return None
    one_way = model.calculate(Order(symbol=symbol, side="BUY", quantity=quantity), price)
    if one_way.model in UNAVAILABLE_COMMISSION_MODELS:
        return None
    if one_way.currency == fx.BASE_CCY:
        commission_fx_rate = 1.0
    elif one_way.currency == quote_currency:
        commission_fx_rate = quote_fx_rate
    else:
        # One symbol-level FX rate cannot truthfully convert a fee charged in a
        # third currency.  Keep the estimate absent until such a rate is passed.
        return None

    fee_rt = one_way.amount * 2.0
    fee_rt_usd = fee_rt * commission_fx_rate
    be_bps = (fee_rt_usd / ref_notional) * 10_000.0
    return {
        "fee_rt": round(fee_rt, 2),
        "currency": one_way.currency,
        "be_bps": round(be_bps, 2),
    }


__all__ = [
    "CommissionCalculator",
    "UNAVAILABLE_COMMISSION_MODELS",
    "round_trip_cost",
]
