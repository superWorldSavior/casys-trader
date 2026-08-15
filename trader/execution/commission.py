"""Compatibility facade for commission and fill-accounting imports.

Dette assumée : ``IbkrCommissionModel`` (impl infra) reste ré-exporté pour
``trader.tools.execution`` et les tests de layout / broker. Voir ``broker.py``.
"""

from trader.application.execute.fee_estimate import round_trip_cost as round_trip_cost
from trader.domain.execution.fill_accounting import (
    POSITION_EPSILON as POSITION_EPSILON,
)
from trader.domain.execution.fill_accounting import (
    compute_fill_effect as compute_fill_effect,
)
from trader.infrastructure.brokers.commission_models import (
    IbkrCommissionModel as IbkrCommissionModel,
)
from trader.infrastructure.brokers.commission_models import (
    NoCommissionModel as NoCommissionModel,
)
from trader.infrastructure.brokers.commission_models import (
    commission_model_from_name as commission_model_from_name,
)

__all__ = [
    "IbkrCommissionModel",
    "NoCommissionModel",
    "POSITION_EPSILON",
    "commission_model_from_name",
    "compute_fill_effect",
    "round_trip_cost",
]
