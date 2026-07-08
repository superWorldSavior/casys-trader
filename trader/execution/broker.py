"""Compatibility facade for execution broker imports."""

from __future__ import annotations

from trader.domain.orders import Side as Side
from trader.execution.commission import (
    IbkrCommissionModel as IbkrCommissionModel,
    NoCommissionModel as NoCommissionModel,
    POSITION_EPSILON as POSITION_EPSILON,
    commission_model_from_name as commission_model_from_name,
    compute_fill_effect as compute_fill_effect,
    round_trip_cost as round_trip_cost,
)
from trader.execution.contracts import Commission as Commission
from trader.execution.contracts import CommissionModelName as CommissionModelName
from trader.execution.contracts import Fill as Fill
from trader.execution.contracts import Order as Order
from trader.execution.contracts import Position as Position
from trader.execution.protocols import Broker as Broker
from trader.execution.protocols import CommissionModel as CommissionModel
from trader.infrastructure.state_db.sim_broker import SimBroker as SimBroker

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
