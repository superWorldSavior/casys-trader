"""Compatibility facade for pure market-domain indicators."""

from __future__ import annotations

from trader.domain.market import features as _domain_features

_exported_names = [
    name for name in dir(_domain_features)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_features, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_features
