"""Compatibility facade for pure market-domain volatility helpers."""

from __future__ import annotations

from trader.domain.market import volatility as _domain_volatility

_exported_names = [
    name for name in dir(_domain_volatility)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_volatility, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_volatility
