"""Compatibility facade for pure market-domain regime classification."""

from __future__ import annotations

from trader.domain.market import regime as _domain_regime

_exported_names = [
    name for name in dir(_domain_regime)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_regime, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_regime
