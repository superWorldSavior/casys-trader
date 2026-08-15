"""Compatibility facade for execution broker imports.

Dette assumée : ``SimBroker`` (impl SQLite/JSON) et ``IbkrCommissionModel``
restent ré-exportés pour ``trader.tools.execution`` (``_TOOLS_COMPAT_EXPORTS``
dans ``trader/__init__.py``, hors zone) et ~15 tests. La prod n'importe plus
``trader.execution.*`` (``test_internal_code_does_not_import_execution_compatibility_facades``).
"""

from __future__ import annotations

from trader.application.execute.protocols import Broker as Broker
from trader.application.execute.protocols import CommissionModel as CommissionModel
from trader.domain.contracts import Commission as Commission
from trader.domain.contracts import CommissionModelName as CommissionModelName
from trader.domain.contracts import Fill as Fill
from trader.domain.contracts import Order as Order
from trader.domain.contracts import Position as Position
from trader.domain.orders import Side as Side
from trader.execution.commission import (
    IbkrCommissionModel as IbkrCommissionModel,
    NoCommissionModel as NoCommissionModel,
    POSITION_EPSILON as POSITION_EPSILON,
    commission_model_from_name as commission_model_from_name,
    compute_fill_effect as compute_fill_effect,
    round_trip_cost as round_trip_cost,
)
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
