"""Pure position and cash accounting for an executed fill."""

from __future__ import annotations

from trader.domain.contracts import Commission, Order
from trader.domain.market import fx

# Les closes venant de l'agent sont normalisés à 8 décimales. Un seuil plus fin
# laisse donc des reliquats (ex. 3.8e-9 action) apparaître comme positions à $0.
POSITION_EPSILON = 1e-8


def compute_fill_effect(
    *,
    old_quantity: float,
    old_avg_price: float,
    order: Order,
    price: float,
    fx_rate: float,
    commission: Commission,
) -> tuple[float, float, float]:
    """Return ``(quantity, average_price, cash_debit_usd)`` after a fill."""
    if abs(old_quantity) <= POSITION_EPSILON:
        old_quantity = 0.0
        old_avg_price = 0.0

    signed = order.quantity if order.side == "BUY" else -order.quantity
    new_quantity = old_quantity + signed
    if abs(new_quantity) <= POSITION_EPSILON:
        new_quantity = 0.0

    if new_quantity == 0:
        new_avg_price = 0.0
    elif old_quantity == 0 or old_quantity * signed > 0:
        total = old_avg_price * abs(old_quantity) + price * abs(signed)
        new_avg_price = total / abs(new_quantity)
    elif old_quantity * new_quantity < 0:
        new_avg_price = price
    else:
        new_avg_price = old_avg_price

    currency = fx.currency_for(order.symbol)
    cash_delta = fx.to_usd(signed * price, currency, fx_rate)
    fee_usd = fx.to_usd(commission.amount, commission.currency, fx_rate)
    return new_quantity, new_avg_price, cash_delta + fee_usd


__all__ = ["POSITION_EPSILON", "compute_fill_effect"]
