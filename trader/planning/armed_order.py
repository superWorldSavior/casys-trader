"""Compatibility facade for pure domain armed-order policy."""

from __future__ import annotations

from trader.domain.planning import armed_order as _domain_armed_order

_exported_names = [
    name for name in dir(_domain_armed_order)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_armed_order, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_armed_order
