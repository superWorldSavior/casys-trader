"""Market data ports used by application services."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trader.domain.market_data import Bar

__all__ = ["DataSource"]


@runtime_checkable
class DataSource(Protocol):
    """Minimal OHLCV bars source contract.

    Implementations may be live adapters, composites, or tests fakes. Connection
    lifecycle stays outside the port unless a concrete adapter exposes optional
    methods such as ``disconnect``.
    """

    def get_bars(
        self,
        symbol: str,
        lookback: str,
        interval: str,
    ) -> list[Bar]:
        """Return OHLCV bars for a symbol or raise ``MarketError``."""
        ...
