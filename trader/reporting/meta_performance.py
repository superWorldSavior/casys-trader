"""Meta-performance reporting facade.

Projection canonique : `trader.reporting.read_models.meta_performance`.
Ce module garde la compatibilité d'import historique.
"""

from __future__ import annotations

from trader.reporting.read_models.meta_performance import (
    DEFAULT_HORIZONS,
    _AUDIT_CACHE,
    compute_meta_performance,
)

__all__ = [
    "DEFAULT_HORIZONS",
    "_AUDIT_CACHE",
    "compute_meta_performance",
]
