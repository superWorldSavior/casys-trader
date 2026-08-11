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
    # Optional causal links deliberately live at the edge of the contract so
    # existing positional construction and legacy payloads remain unchanged.
    process_instance_id: str | None = None
    attempt_id: str | None = None
    decision_id: str | None = None

    def model_dump(self, **kwargs):  # type: ignore[override]
        """Keep the historical order JSON shape unless causal links are present."""
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)


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
    process_instance_id: str | None = None
    attempt_id: str | None = None
    decision_id: str | None = None

    def model_dump(self, **kwargs):  # type: ignore[override]
        """Keep the historical fill JSON shape unless causal links are present."""
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)


class Commission(_FrozenContract):
    amount: float
    currency: str = "USD"
    model: str = "none"


class Position(_FrozenContract):
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
