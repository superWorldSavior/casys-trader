"""Authoritative listing → canonical MIC/country/region proposal.

Resolution itself stays a pure mapping-table lookup. This module only
proposes typed rows from provider exchange codes or the sessions suffix
MIC table. Unsuffixed US names are never defaulted to XNYS.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from trader.domain.market.sessions import explicit_mic_assignment
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMappingEntry,
)


LISTING_STATUSES = frozenset({"resolved", "unresolved", "ambiguous"})
LISTING_EXCHANGE_TAXONOMY = "listing_exchange.v1"
SESSIONS_MIC_TAXONOMY = "sessions_mic.v1"
_YAHOO_EXCHANGE_TO_MIC = {
    "NYQ": "XNYS",
    "NYSE": "XNYS",
    "NYE": "XNYS",
    "NYS": "XNYS",
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NASDAQ": "XNAS",
    "NAS": "XNAS",
    "NSM": "XNAS",
    "ASE": "XASE",
    "AMEX": "XASE",
    "NYA": "XASE",
    "PCX": "ARCX",
    "ARCA": "ARCX",
    "ARCX": "ARCX",
}
_MIC_GEOGRAPHY = {
    "XNYS": ("iso-3166:US", "iso-un-m49:021"),
    "XNAS": ("iso-3166:US", "iso-un-m49:021"),
    "XASE": ("iso-3166:US", "iso-un-m49:021"),
    "ARCX": ("iso-3166:US", "iso-un-m49:021"),
    "XTAI": ("iso-3166:TW", "iso-un-m49:030"),
    "XLON": ("iso-3166:GB", "iso-un-m49:150"),
    "XSWX": ("iso-3166:CH", "iso-un-m49:150"),
    "XCSE": ("iso-3166:DK", "iso-un-m49:150"),
    "XPAR": ("iso-3166:FR", "iso-un-m49:150"),
    "XWBO": ("iso-3166:AT", "iso-un-m49:150"),
    "XETR": ("iso-3166:DE", "iso-un-m49:150"),
    "XSTO": ("iso-3166:SE", "iso-un-m49:150"),
    "XLIS": ("iso-3166:PT", "iso-un-m49:150"),
    "XBRU": ("iso-3166:BE", "iso-un-m49:150"),
    "XHEL": ("iso-3166:FI", "iso-un-m49:150"),
    "XMAD": ("iso-3166:ES", "iso-un-m49:150"),
    "XAMS": ("iso-3166:NL", "iso-un-m49:150"),
    "XMIL": ("iso-3166:IT", "iso-un-m49:150"),
    "XOSL": ("iso-3166:NO", "iso-un-m49:150"),
}


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _proofs(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("provider_proofs must be a sequence of strings")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        text = _required_text(item, "provider_proofs[]")
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return tuple(ordered)


def listing_mic_for_exchange_code(exchange_code: str | None) -> str | None:
    if exchange_code is None:
        return None
    if not isinstance(exchange_code, str):
        raise TypeError("exchange_code must be a string")
    code = exchange_code.strip().upper()
    if not code:
        return None
    return _YAHOO_EXCHANGE_TO_MIC.get(code)


def _logical_venue_for_assignment(selector: str) -> str:
    if selector in {".TW", ".TWO"} or selector.endswith(".TW") or selector.endswith(".TWO"):
        return "TW"
    return "EU"


@dataclass(frozen=True)
class WorldInstrumentListing:
    """Provider-facing listing facts for one universe instrument."""

    market_venue: str
    instrument: str
    exchange_code: str | None
    provider_proofs: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_venue", _required_text(self.market_venue, "market_venue"))
        object.__setattr__(self, "instrument", _required_text(self.instrument, "instrument"))
        object.__setattr__(self, "exchange_code", _optional_text(self.exchange_code, "exchange_code"))
        object.__setattr__(self, "provider_proofs", _proofs(self.provider_proofs))


@dataclass(frozen=True)
class UnresolvedInstrumentListing:
    """Fail-safe listing outcome. Never invents a MIC."""

    market_venue: str
    instrument: str
    status: str
    reason: str

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in LISTING_STATUSES or status == "resolved":
            raise ValueError("unresolved listing status must be unresolved or ambiguous")
        object.__setattr__(self, "market_venue", _required_text(self.market_venue, "market_venue"))
        object.__setattr__(self, "instrument", _required_text(self.instrument, "instrument"))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))


def _unresolved(listing: WorldInstrumentListing, reason: str) -> UnresolvedInstrumentListing:
    return UnresolvedInstrumentListing(
        market_venue=listing.market_venue,
        instrument=listing.instrument,
        status="unresolved",
        reason=reason,
    )


def _entry(
    listing: WorldInstrumentListing,
    *,
    mic: str,
    proofs: Sequence[str],
    taxonomy_version: str,
) -> WorldScopeMappingEntry | UnresolvedInstrumentListing:
    geography = _MIC_GEOGRAPHY.get(mic)
    if geography is None:
        return _unresolved(listing, "unknown_mic_geography")
    country, region = geography
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue=listing.market_venue, instrument=listing.instrument),
        venue=WorldCanonicalScopeRef(kind="venue", entity_id=f"mic:{mic}"),
        country=WorldCanonicalScopeRef(kind="country", entity_id=country),
        region=WorldCanonicalScopeRef(kind="region", entity_id=region),
        world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
        provider_proofs=proofs,
        taxonomy_version=taxonomy_version,
    )


def propose_world_scope_mapping_entry(
    listing: WorldInstrumentListing,
) -> WorldScopeMappingEntry | UnresolvedInstrumentListing:
    if not isinstance(listing, WorldInstrumentListing):
        raise TypeError("listing must be WorldInstrumentListing")
    if listing.market_venue == "FX" or "=" in listing.instrument:
        return _unresolved(listing, "unsupported_market_venue")
    if listing.market_venue not in {"US", "EU", "TW"}:
        return _unresolved(listing, "unsupported_market_venue")
    assignment = explicit_mic_assignment(listing.instrument)
    if assignment is not None:
        selector, mic = assignment
        expected_venue = _logical_venue_for_assignment(selector)
        if listing.market_venue != expected_venue:
            return _unresolved(listing, "venue_instrument_mismatch")
        proofs = listing.provider_proofs + (
            f"trader.domain.market.sessions.explicit_mic_for_symbol:{selector}={mic}",
        )
        return _entry(listing, mic=mic, proofs=proofs, taxonomy_version=SESSIONS_MIC_TAXONOMY)
    if listing.market_venue != "US":
        return _unresolved(listing, "venue_instrument_mismatch")
    if listing.exchange_code is None:
        return _unresolved(listing, "missing_exchange_code")
    mic = listing_mic_for_exchange_code(listing.exchange_code)
    if mic is None:
        return _unresolved(listing, "unknown_exchange_code")
    proofs = listing.provider_proofs + (f"yfinance.exchange:{listing.exchange_code.upper()}=mic:{mic}",)
    return _entry(listing, mic=mic, proofs=proofs, taxonomy_version=LISTING_EXCHANGE_TAXONOMY)


__all__ = [
    "LISTING_EXCHANGE_TAXONOMY",
    "LISTING_STATUSES",
    "SESSIONS_MIC_TAXONOMY",
    "UnresolvedInstrumentListing",
    "WorldInstrumentListing",
    "listing_mic_for_exchange_code",
    "propose_world_scope_mapping_entry",
]
