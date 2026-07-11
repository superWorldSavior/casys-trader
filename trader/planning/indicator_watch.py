"""Compatibility facade for pure domain indicator-watch policy."""

from __future__ import annotations

from trader.domain.planning import indicator_watch as _domain_indicator_watch

_exported_names = [
    name for name in dir(_domain_indicator_watch)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_indicator_watch, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_indicator_watch
