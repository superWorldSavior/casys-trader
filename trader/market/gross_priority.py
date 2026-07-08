"""Compatibility facade for pure market-domain gross-priority ordering."""

from __future__ import annotations

from trader.domain.market import gross_priority as _domain_gross_priority

_exported_names = [
    name for name in dir(_domain_gross_priority)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_gross_priority, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_gross_priority
