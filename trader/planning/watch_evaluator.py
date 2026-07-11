"""Compatibility facade for pure domain watch evaluation."""

from __future__ import annotations

from trader.domain.planning import watch_evaluator as _domain_watch_evaluator

_exported_names = [
    name for name in dir(_domain_watch_evaluator)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_watch_evaluator, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]

del _exported_names
del _domain_watch_evaluator
