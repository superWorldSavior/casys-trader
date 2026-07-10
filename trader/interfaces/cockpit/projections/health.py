"""Pure projections for cockpit data health and configured sources."""

from __future__ import annotations

from dataclasses import dataclass

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.projections.universe import venue_of_safe

VENUE_DISPLAY: dict[str, str] = {"TW": "TPE", "EU": "EU", "US": "US"}
VENUE_ORDER: tuple[str, ...] = ("TW", "EU", "US")


@dataclass(frozen=True)
class StaleSymbolRow:
    symbol: str
    age_minutes: float | None


@dataclass(frozen=True)
class VenueFreshnessRow:
    venue: str
    display_name: str
    symbol_count: int
    is_stale: bool
    max_age_minutes: float | None
    stale_symbols: tuple[StaleSymbolRow, ...]


@dataclass(frozen=True)
class FreshnessProjection:
    venues: list[VenueFreshnessRow]
    stale_symbols: list[StaleSymbolRow]

    @property
    def has_stale(self) -> bool:
        return bool(self.stale_symbols)


@dataclass(frozen=True)
class SourceRow:
    name: str
    detail: str


@dataclass(frozen=True)
class SourcesProjection:
    rows: list[SourceRow]


def symbols_by_venue(state: dict) -> dict[str, list[str]]:
    """Group universe symbols by venue while excluding FX pairs."""

    grouped: dict[str, list[str]] = {}
    for raw_symbol in state.get("universe_symbols") or []:
        symbol = str(raw_symbol)
        venue = venue_of_safe(symbol)
        if venue != "FX":
            grouped.setdefault(venue, []).append(symbol)
    return grouped


def project_freshness(state: dict) -> FreshnessProjection:
    """Project venue freshness and globally age-sorted stale symbols."""

    stale_data = f.safe_dict(state.get("stale_market_data"))
    grouped = symbols_by_venue(state)
    sorted_venues = [venue for venue in VENUE_ORDER if venue in grouped]
    sorted_venues += sorted(
        venue for venue in grouped if venue not in VENUE_ORDER
    )

    venue_rows: list[VenueFreshnessRow] = []
    all_stale: list[StaleSymbolRow] = []
    for venue in sorted_venues:
        symbols = grouped.get(venue, [])
        stale_symbols = tuple(
            StaleSymbolRow(
                symbol=symbol,
                age_minutes=f.staleness_age_m(state, symbol),
            )
            for symbol in symbols
            if symbol in stale_data
        )
        all_stale.extend(stale_symbols)
        venue_rows.append(
            VenueFreshnessRow(
                venue=venue,
                display_name=VENUE_DISPLAY.get(venue, venue),
                symbol_count=len(symbols),
                is_stale=bool(stale_symbols),
                max_age_minutes=max(
                    (
                        row.age_minutes
                        for row in stale_symbols
                        if row.age_minutes is not None
                    ),
                    default=None,
                ),
                stale_symbols=stale_symbols,
            )
        )

    all_stale.sort(
        key=lambda row: row.age_minutes or 0.0,
        reverse=True,
    )
    return FreshnessProjection(venues=venue_rows, stale_symbols=all_stale)


def project_sources(
    state: dict,
    *,
    ib_host: str,
    ib_port: str,
    ib_client_id: str,
) -> SourcesProjection:
    """Project configured source labels from explicit environment values."""

    rows = [
        SourceRow("IB gateway", f"{ib_host}:{ib_port} · id {ib_client_id}"),
        SourceRow("yfinance", "fallback"),
        SourceRow("news", "yahoo"),
    ]
    macro = (
        state.get("macro")
        or state.get("macro_data")
        or state.get("macro_calendar")
    )
    if macro:
        if isinstance(macro, dict):
            next_fomc = macro.get("next_fomc") or macro.get("fomc_next")
            detail = f"next FOMC {next_fomc}" if next_fomc else "available"
        else:
            detail = "available"
        rows.append(SourceRow("macro", detail))
    return SourcesProjection(rows=rows)


__all__ = [
    "FreshnessProjection",
    "SourceRow",
    "SourcesProjection",
    "StaleSymbolRow",
    "VENUE_DISPLAY",
    "VENUE_ORDER",
    "VenueFreshnessRow",
    "project_freshness",
    "project_sources",
    "symbols_by_venue",
]
