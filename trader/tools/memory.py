"""Mutable compatibility facade for agent memory and raw learnings.

Canonical imports:
- ``trader.agent.memory.Memory``
- ``trader.learnings.raw_store.RawLearningsStore``
"""

from __future__ import annotations

import sys
import types

from trader.agent import memory as _agent_memory
from trader.learnings import raw_store as _raw_store

_ROUTED_NAMES = {
    "Memory": (_agent_memory, "Memory"),
    "LearningsStore": (_raw_store, "LearningsStore"),
    "RawLearningsStore": (_raw_store, "RawLearningsStore"),
}


class _CompatModule(types.ModuleType):
    def __getattr__(self, name: str):
        target = _ROUTED_NAMES.get(name)
        if target is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        module, attr = target
        return getattr(module, attr)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {"_agent_memory", "_raw_store", "_ROUTED_NAMES"}:
            return super().__setattr__(name, value)
        if name == "Memory":
            setattr(_agent_memory, "Memory", value)
            return None
        if name in {"LearningsStore", "RawLearningsStore"}:
            setattr(_raw_store, "LearningsStore", value)
            setattr(_raw_store, "RawLearningsStore", value)
            return None
        return super().__setattr__(name, value)

    def __delattr__(self, name: str):
        if name == "Memory":
            delattr(_agent_memory, "Memory")
            return None
        if name in {"LearningsStore", "RawLearningsStore"}:
            delattr(_raw_store, "LearningsStore")
            delattr(_raw_store, "RawLearningsStore")
            return None
        return super().__delattr__(name)


_module = sys.modules[__name__]
_module.__class__ = _CompatModule
__all__ = sorted(_ROUTED_NAMES)
