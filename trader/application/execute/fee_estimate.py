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


def cost_scope_for(model: CommissionCalculator | None) -> tuple[str, bool]:
    """Describe what a commission-backed cost projection actually covers.

    Commission adapters are not assumed to be all-in.  Unknown adapters fail
    conservative at the metadata boundary instead of silently upgrading a
    broker fee into a complete transaction-cost estimate.
    """

    if model is None:
        return "no_commission_model", False
    scope = str(getattr(model, "cost_scope", "") or "").strip()
    if not scope:
        scope = "commission_model_only"
    # Completeness is safety metadata: only the literal boolean True may
    # promote an estimate.  Values such as the string "false" stay fail-closed.
    is_all_in = getattr(model, "cost_estimate_is_all_in", False) is True
    return scope, is_all_in


def round_trip_cost(
    model: CommissionCalculator | None,
    symbol: str,
    price: float | None,
    ref_notional: float,
    *,
    fx_rate: float | None = None,
) -> dict[str, float | str | bool] | None:
    """Estimate round-trip modeled costs for a USD reference notional.

    ``price`` and the returned legacy ``fee_rt`` remain native.  ``fx_rate`` follows
    the project convention (USD per unit of the symbol quote currency) and is
    mandatory for a non-USD listing.  Break-even bps are computed on the USD
    reference notional, after converting each native commission back to USD.
    ``cost_scope`` and ``cost_estimate_is_all_in`` are mandatory truth labels:
    callers must never present this broker projection as an all-in cost.
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
    buy = model.calculate(Order(symbol=symbol, side="BUY", quantity=quantity), price)
    sell = model.calculate(Order(symbol=symbol, side="SELL", quantity=quantity), price)
    if {buy.model, sell.model} & UNAVAILABLE_COMMISSION_MODELS:
        return None
    try:
        buy_amount = float(buy.amount)
        sell_amount = float(sell.amount)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(buy_amount)
        or buy_amount < 0.0
        or not math.isfinite(sell_amount)
        or sell_amount < 0.0
    ):
        return None
    if buy.currency != sell.currency:
        return None

    if buy.currency == fx.BASE_CCY:
        commission_fx_rate = 1.0
    elif buy.currency == quote_currency:
        commission_fx_rate = quote_fx_rate
    else:
        # One symbol-level FX rate cannot truthfully convert a fee charged in a
        # third currency.  Keep the estimate absent until such a rate is passed.
        return None

    # BUY and SELL can have different costs (taxes/levies are commonly
    # side-specific).  Never infer a round trip by doubling one side.
    fee_rt = buy_amount + sell_amount
    fee_rt_usd = fee_rt * commission_fx_rate
    be_bps = (fee_rt_usd / ref_notional) * 10_000.0
    cost_scope, is_all_in = cost_scope_for(model)
    return {
        "fee_rt": round(fee_rt, 2),
        "currency": buy.currency,
        "be_bps": round(be_bps, 2),
        "cost_scope": cost_scope,
        "cost_estimate_is_all_in": is_all_in,
    }


__all__ = [
    "CommissionCalculator",
    "UNAVAILABLE_COMMISSION_MODELS",
    "cost_scope_for",
    "round_trip_cost",
]
