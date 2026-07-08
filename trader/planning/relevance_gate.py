"""Compatibility facade for pure domain relevance gating."""

from __future__ import annotations

from trader.domain.planning import relevance_gate as _domain_relevance_gate
from trader.domain.planning.relevance_gate import *  # noqa: F401,F403

globals().update(
    {
        name: getattr(_domain_relevance_gate, name)
        for name in dir(_domain_relevance_gate)
        if not name.startswith("__")
    }
)

__all__ = [name for name in dir(_domain_relevance_gate) if not name.startswith("__")]
