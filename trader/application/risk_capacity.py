"""Risk-capacity context exposed to the decision agent."""

from __future__ import annotations

import math
from typing import Callable

from trader.execution.ports import Broker
from trader.execution.risk import RiskLimits


def gross_exposure(
    broker: Broker,
    prices: dict[str, float],
    rate_of: Callable[[str], float] | None = None,
) -> float:
    return sum(
        abs(pos.quantity * prices.get(symbol, 0.0) * (rate_of(symbol) if rate_of else 1.0))
        for symbol, pos in broker.positions().items()
    )


def finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def side_capacity_usd(
    *,
    side: str,
    current_position_value: float,
    gross_exposure: float,
    limits: RiskLimits,
    equity: float,
) -> float:
    if (
        side not in {"BUY", "SELL"}
        or not math.isfinite(current_position_value)
        or not math.isfinite(gross_exposure)
        or not math.isfinite(equity)
        or equity < limits.min_equity
    ):
        return 0.0

    max_order = float(limits.max_order_value)
    position_cap = float(limits.max_position_value)
    gross_cap_for_symbol = float(limits.max_gross_exposure) - gross_exposure + abs(current_position_value)
    cap = min(position_cap, gross_cap_for_symbol)
    if max_order <= 0.0 or cap < 0.0:
        return 0.0

    sign = 1.0 if side == "BUY" else -1.0
    if sign > 0.0:
        lower = -cap - current_position_value
        upper = cap - current_position_value
    else:
        lower = current_position_value - cap
        upper = current_position_value + cap
    lower = max(0.0, lower)
    upper = min(max_order, upper)
    if upper < lower:
        return 0.0
    return max(0.0, upper)


def risk_capacity_context(
    *,
    symbols: list[str],
    prices: dict[str, float],
    broker: Broker,
    gross_exposure: float,
    limits: RiskLimits,
    equity: float,
    rate_of: Callable[[str], float],
    currency_of: Callable[[str], str],
) -> dict:
    """Expose advisory sizing caps before the final RiskGate check."""
    positions = broker.positions()
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        price = prices.get(symbol)
        rate = rate_of(symbol)
        ccy = currency_of(symbol)
        if not finite_positive(price) or not finite_positive(rate):
            per_symbol[symbol] = {
                "price": price,
                "ccy": ccy,
                "fx_usd": rate,
                "current_position_value_usd": 0.0,
                "max_buy_qty": 0.0,
                "max_buy_notional_native": 0.0,
                "max_buy_notional_usd": 0.0,
                "max_sell_qty": 0.0,
                "max_sell_notional_native": 0.0,
                "max_sell_notional_usd": 0.0,
            }
            continue
        pos = positions.get(symbol)
        current_position_value = 0.0 if pos is None else pos.quantity * float(price) * float(rate)
        buy_usd = side_capacity_usd(
            side="BUY",
            current_position_value=current_position_value,
            gross_exposure=gross_exposure,
            limits=limits,
            equity=equity,
        )
        sell_usd = side_capacity_usd(
            side="SELL",
            current_position_value=current_position_value,
            gross_exposure=gross_exposure,
            limits=limits,
            equity=equity,
        )
        price_usd = float(price) * float(rate)
        per_symbol[symbol] = {
            "price": float(price),
            "ccy": ccy,
            "fx_usd": float(rate),
            "current_position_value_usd": current_position_value,
            "max_buy_qty": buy_usd / price_usd,
            "max_buy_notional_native": buy_usd / float(rate),
            "max_buy_notional_usd": buy_usd,
            "max_sell_qty": sell_usd / price_usd,
            "max_sell_notional_native": sell_usd / float(rate),
            "max_sell_notional_usd": sell_usd,
        }
    return {
        "gross_exposure_usd": gross_exposure,
        "max_gross_exposure_usd": float(limits.max_gross_exposure),
        "gross_remaining_usd": max(0.0, float(limits.max_gross_exposure) - gross_exposure),
        "max_order_value_usd": float(limits.max_order_value),
        "max_position_value_usd": float(limits.max_position_value),
        "equity_usd": equity,
        "per_symbol": per_symbol,
    }
