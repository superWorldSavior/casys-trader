"""Mutable compatibility facade for ``trader.market.data_source``."""

from __future__ import annotations

import sys
import types

from trader.market import data_source as _target


class _CompatModule(types.ModuleType):
    def __getattr__(self, name: str):
        return getattr(_target, name)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {"_target"}:
            return super().__setattr__(name, value)
        setattr(_target, name, value)

    def __delattr__(self, name: str):
        delattr(_target, name)


_module = sys.modules[__name__]
_module.__class__ = _CompatModule
__all__ = [name for name in dir(_target) if not name.startswith("__")]
