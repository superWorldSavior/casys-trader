from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_scope import WorldScopeMappingEntry
from trader.domain.world_scope_listing import (
    LISTING_STATUSES,
    UnresolvedInstrumentListing,
    WorldInstrumentListing,
    listing_mic_for_exchange_code,
    propose_world_scope_mapping_entry,
)


MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_scope_listing.py"


def test_listing_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "trader.application" not in source
    assert "import yfinance" not in source
    assert "default=xnys" not in source.lower()


def test_nasdaq_and_nyse_exchange_codes_map_to_distinct_mics() -> None:
    assert listing_mic_for_exchange_code("NMS") == "XNAS"
    assert listing_mic_for_exchange_code("NASDAQ") == "XNAS"
    assert listing_mic_for_exchange_code("NYQ") == "XNYS"
    assert listing_mic_for_exchange_code("NYSE") == "XNYS"
    assert listing_mic_for_exchange_code("nms") == "XNAS"
    assert listing_mic_for_exchange_code("UNKNOWN") is None
    assert listing_mic_for_exchange_code("") is None


def test_us_nasdaq_listing_proposes_xnas_without_nyse_default() -> None:
    listing = WorldInstrumentListing(
        market_venue="US",
        instrument="REGN",
        exchange_code="NMS",
        provider_proofs=("yfinance.exchange:NMS",),
    )
    proposed = propose_world_scope_mapping_entry(listing)
    assert isinstance(proposed, WorldScopeMappingEntry)
    assert proposed.anchor.market_venue == "US"
    assert proposed.anchor.instrument == "REGN"
    assert proposed.venue.entity_id == "mic:XNAS"
    assert proposed.country.entity_id == "iso-3166:US"
    assert proposed.region.entity_id == "iso-un-m49:021"
    assert proposed.world.entity_id == "market"
    assert proposed.taxonomy_version == "listing_exchange.v1"
    assert "yfinance.exchange:NMS=mic:XNAS" in proposed.provider_proofs


def test_us_nyse_listing_proposes_xnys() -> None:
    listing = WorldInstrumentListing(
        market_venue="US",
        instrument="NEM",
        exchange_code="NYQ",
        provider_proofs=("yfinance.exchange:NYQ",),
    )
    proposed = propose_world_scope_mapping_entry(listing)
    assert isinstance(proposed, WorldScopeMappingEntry)
    assert proposed.venue.entity_id == "mic:XNYS"
    assert "yfinance.exchange:NYQ=mic:XNYS" in proposed.provider_proofs


def test_unknown_or_missing_exchange_is_unresolved_not_defaulted() -> None:
    missing = propose_world_scope_mapping_entry(
        WorldInstrumentListing(
            market_venue="US",
            instrument="FOO",
            exchange_code=None,
            provider_proofs=("provider:failed",),
        )
    )
    unknown = propose_world_scope_mapping_entry(
        WorldInstrumentListing(
            market_venue="US",
            instrument="FOO",
            exchange_code="OTC",
            provider_proofs=("yfinance.exchange:OTC",),
        )
    )
    assert isinstance(missing, UnresolvedInstrumentListing)
    assert isinstance(unknown, UnresolvedInstrumentListing)
    assert missing.status == unknown.status == "unresolved"
    assert missing.reason == "missing_exchange_code"
    assert unknown.reason == "unknown_exchange_code"
    assert LISTING_STATUSES == frozenset({"resolved", "unresolved", "ambiguous"})
    with pytest.raises(FrozenInstanceError):
        missing.reason = "other"  # type: ignore[misc]


def test_taipei_two_suffix_is_not_collapsed_into_tw() -> None:
    from trader.domain.market.sessions import explicit_mic_assignment

    assert explicit_mic_assignment("6488.TWO") == (".TWO", "XTAI")
    proposed = propose_world_scope_mapping_entry(
        WorldInstrumentListing(
            market_venue="TW",
            instrument="6488.TWO",
            exchange_code=None,
            provider_proofs=("trader.market.rotation.wiring.venue_of:.TWO=TW",),
        )
    )
    assert isinstance(proposed, WorldScopeMappingEntry)
    assert proposed.venue.entity_id == "mic:XTAI"
    assert any(item.endswith(":.TWO=XTAI") for item in proposed.provider_proofs)


def test_suffixed_european_listing_uses_session_mic_authority() -> None:
    listing = WorldInstrumentListing(
        market_venue="EU",
        instrument="AIR.PA",
        exchange_code=None,
        provider_proofs=("trader.market.rotation.wiring.venue_of:.PA=EU",),
    )
    proposed = propose_world_scope_mapping_entry(listing)
    assert isinstance(proposed, WorldScopeMappingEntry)
    assert proposed.venue.entity_id == "mic:XPAR"
    assert proposed.country.entity_id == "iso-3166:FR"
    assert proposed.region.entity_id == "iso-un-m49:150"
    assert proposed.taxonomy_version == "sessions_mic.v1"
    assert any(item.startswith("trader.domain.market.sessions.") for item in proposed.provider_proofs)


def test_fx_and_logical_venue_mismatch_stay_unresolved() -> None:
    fx = propose_world_scope_mapping_entry(
        WorldInstrumentListing(
            market_venue="FX",
            instrument="EURUSD=X",
            exchange_code=None,
            provider_proofs=("venue:FX",),
        )
    )
    mismatch = propose_world_scope_mapping_entry(
        WorldInstrumentListing(
            market_venue="US",
            instrument="SAP.DE",
            exchange_code="NYQ",
            provider_proofs=("yfinance.exchange:NYQ",),
        )
    )
    assert isinstance(fx, UnresolvedInstrumentListing)
    assert fx.reason == "unsupported_market_venue"
    assert isinstance(mismatch, UnresolvedInstrumentListing)
    assert mismatch.reason == "venue_instrument_mismatch"


def test_listing_proposal_signature_has_no_runtime_clock() -> None:
    params = inspect.signature(propose_world_scope_mapping_entry).parameters
    assert list(params) == ["listing"]
    source = Path(MODULE_PATH).read_text(encoding="utf-8")
    assert "ready_at" not in source
    assert "datetime.now" not in source
