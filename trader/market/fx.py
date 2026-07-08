"""Compatibility facade for pure market-domain FX helpers."""

from __future__ import annotations

from trader.domain.market import fx as _domain_fx

_exported_names = [
    name for name in dir(_domain_fx)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_fx, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_fx
