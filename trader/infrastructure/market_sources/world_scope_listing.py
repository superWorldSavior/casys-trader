"""Listing-metadata adapters for World scope-mapping reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from trader.domain.market.sessions import explicit_mic_assignment
from trader.domain.world_scope_listing import WorldInstrumentListing
from trader.market.rotation.wiring import venue_of


class YFinanceListingExchangeLookup:
    """Narrow exchange-code lookup. Does not pull financial statements."""

    def __init__(self, *, ticker_factory: Callable[[str], Any] | None = None) -> None:
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory

    def __call__(self, symbol: str) -> str | None:
        ticker = self._ticker_factory(symbol)
        info = _safe_mapping_call(ticker, "get_info", fallback_attr="info")
        code = str(info.get("exchange") or "").strip()
        return code or None


class InstrumentListingMetadataAdapter:
    """Suffix MIC first (sessions taxonomy); US unsuffixed names use exchange lookup."""

    def __init__(
        self,
        *,
        exchange_lookup: Callable[[str], str | None] | None = None,
        venue_of_fn: Callable[[str], str] = venue_of,
    ) -> None:
        self._exchange_lookup = exchange_lookup
        self._venue_of = venue_of_fn

    def lookup(self, *, market_venue: str, instrument: str) -> WorldInstrumentListing:
        venue = str(market_venue).strip()
        symbol = str(instrument).strip()
        proofs = (f"trader.market.rotation.wiring.venue_of:{symbol}={self._venue_of(symbol)}",)
        if explicit_mic_assignment(symbol) is not None:
            return WorldInstrumentListing(
                market_venue=venue,
                instrument=symbol,
                exchange_code=None,
                provider_proofs=proofs,
            )
        if self._exchange_lookup is None:
            raise RuntimeError("listing exchange lookup is not configured")
        code = self._exchange_lookup(symbol)
        extra = () if not code else (f"yfinance.exchange:{code}",)
        return WorldInstrumentListing(
            market_venue=venue,
            instrument=symbol,
            exchange_code=code,
            provider_proofs=proofs + extra,
        )


def _safe_mapping_call(target: Any, method_name: str, *, fallback_attr: str) -> dict[str, Any]:
    try:
        method = getattr(target, method_name, None)
        value = method() if callable(method) else getattr(target, fallback_attr, {})
    except Exception:  # noqa: BLE001 - listing coverage is partial by contract
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["InstrumentListingMetadataAdapter", "YFinanceListingExchangeLookup"]
