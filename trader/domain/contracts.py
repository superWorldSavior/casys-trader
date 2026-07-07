"""Pure execution data contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader.domain.orders import Side

CommissionModelName = Literal["none", "ibkr"]

__all__ = [
    "Commission",
    "CommissionModelName",
    "Fill",
    "Order",
    "Position",
]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    def __init__(self, *args: object, **data: object) -> None:
        if args:
            field_names = tuple(type(self).model_fields)
            if len(args) > len(field_names):
                raise TypeError(f"{type(self).__name__} expected at most {len(field_names)} positional arguments")
            for name, value in zip(field_names, args, strict=False):
                if name in data:
                    raise TypeError(f"{type(self).__name__} got multiple values for argument {name!r}")
                data[name] = value
        super().__init__(**data)


class Order(_FrozenContract):
    symbol: str
    side: Side
    quantity: float
    rationale: str = ""


class Fill(_FrozenContract):
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: str
    commission: float = 0.0
    commission_currency: str = "USD"
    commission_model: str = "none"
    fx_rate: float = 1.0


class Commission(_FrozenContract):
    amount: float
    currency: str = "USD"
    model: str = "none"


class Position(_FrozenContract):
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
