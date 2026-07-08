"""Compatibility facade for market-session calculations and market-data I/O."""

from __future__ import annotations

from trader.domain.market import sessions as _sessions
from trader.domain.market.sessions import *  # noqa: F403

globals().update(
    {
        name: getattr(_sessions, name)
        for name in dir(_sessions)
        if not (name.startswith("__") and name.endswith("__"))
    }
)

from trader.market.market_data_yf import Quote, get_bars, get_quote

__all__ = [
    *_sessions.__all__,
    "Quote",
    "get_bars",
    "get_quote",
]

del _sessions
