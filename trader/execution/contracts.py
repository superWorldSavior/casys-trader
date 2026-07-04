"""Pure execution data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trader.domain.orders import Side

CommissionModelName = Literal["none", "ibkr"]

__all__ = [
    "Commission",
    "CommissionModelName",
    "Fill",
    "Order",
    "Position",
]


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: float
    rationale: str = ""


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: str
    commission: float = 0.0
    commission_currency: str = "USD"
    commission_model: str = "none"
    fx_rate: float = 1.0


@dataclass(frozen=True)
class Commission:
    amount: float
    currency: str = "USD"
    model: str = "none"


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
