"""Shared domain primitives and governed metadata with no runtime dependency."""

from trader.domain.market_data import Bar, MarketError
from trader.domain.orders import Side

__all__ = ["Bar", "MarketError", "Side"]
