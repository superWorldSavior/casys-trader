"""Shared domain primitives with no dependency on tools or runtime layers."""

from trader.domain.market_data import Bar, MarketError
from trader.domain.orders import Side

__all__ = ["Bar", "MarketError", "Side"]
