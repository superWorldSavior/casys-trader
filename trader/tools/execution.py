"""Compatibility facade for broker primitives moved to ``trader.execution.broker``."""

from __future__ import annotations

from trader.domain.orders import Side
from trader.execution.broker import (
    Broker,
    Commission,
    CommissionModel,
    CommissionModelName,
    Fill,
    IbkrCommissionModel,
    NoCommissionModel,
    Order,
    Position,
    SimBroker,
    commission_model_from_name,
    compute_fill_effect,
    round_trip_cost,
)

__all__ = [
    "Broker",
    "Commission",
    "CommissionModel",
    "CommissionModelName",
    "Fill",
    "IbkrCommissionModel",
    "NoCommissionModel",
    "Order",
    "Position",
    "Side",
    "SimBroker",
    "commission_model_from_name",
    "compute_fill_effect",
    "round_trip_cost",
]
