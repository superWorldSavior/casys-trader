from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_issuer_registry import (
    ISSUER_REGISTRY_SCHEMA,
    IssuerEntry,
    IssuerRegistry,
    issuer_entity_id_for_listing,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_issuer_registry.py"


def _entry(**overrides: object) -> IssuerEntry:
    values: dict[str, object] = {
        "instrument_node_id": "instrument:mic:XPAR:symbol:TTE.PA",
        "market_venue": "EU",
        "symbol": "TTE.PA",
        "mic": "XPAR",
        "issuer_entity_id": "issuer:yahoo:v1:XPAR:TTE.PA",
        "identity_status": "verified",
        "brief_as_of": "2026-09-05T17:26:49+00:00",
        "source_refs": ("company_micro:v1:TTE.PA:abc",),
        "resolution_method": "mapping_plus_exchange",
    }
    values.update(overrides)
    return IssuerEntry(**values)  # type: ignore[arg-type]


def test_listing_id_is_derived_not_free_text() -> None:
    assert issuer_entity_id_for_listing("XPAR", "TTE.PA") == "issuer:yahoo:v1:XPAR:TTE.PA"
    assert issuer_entity_id_for_listing("xpar", "TTE.PA") == "issuer:yahoo:v1:XPAR:TTE.PA"
    with pytest.raises(ValueError, match="4-character MIC"):
        issuer_entity_id_for_listing("PARIS", "TTE.PA")
    with pytest.raises(ValueError, match="whitespace"):
        issuer_entity_id_for_listing("XPAR", "TTE PA")


def test_entry_cross_checks_node_and_issuer_ids() -> None:
    entry = _entry()
    assert IssuerEntry.from_mapping(entry.to_dict()) == entry
    with pytest.raises(ValueError, match="instrument_node_id"):
        _entry(instrument_node_id="instrument:mic:XPAR:symbol:OTHER")
    with pytest.raises(ValueError, match="issuer_entity_id"):
        _entry(issuer_entity_id="issuer:yahoo:v1:XPAR:OTHER")
    with pytest.raises(ValueError, match="identity_status=verified"):
        _entry(identity_status="unverified")
    with pytest.raises(ValueError, match="source_refs"):
        _entry(source_refs=())
    with pytest.raises(ValueError, match="resolution_method"):
        _entry(resolution_method="guessed")
    with pytest.raises(ValueError, match="unknown fields"):
        IssuerEntry.from_mapping({**entry.to_dict(), "lei": "123"})
    with pytest.raises(FrozenInstanceError):
        entry.symbol = "OTHER"  # type: ignore[misc]


def test_registry_hash_covers_identities_only() -> None:
    first = IssuerRegistry(registry_id="issuer_registry.v1", entries={"instrument:mic:XPAR:symbol:TTE.PA": _entry()})
    refreshed = IssuerRegistry(
        registry_id="issuer_registry.v1",
        entries={
            "instrument:mic:XPAR:symbol:TTE.PA": _entry(
                brief_as_of="2026-09-12T09:00:00+00:00",
                source_refs=("company_micro:v1:TTE.PA:def",),
            )
        },
    )
    assert refreshed.content_sha256 == first.content_sha256
    moved = IssuerRegistry(
        registry_id="issuer_registry.v1",
        entries={
            "instrument:mic:XPAR:symbol:TTE.PA": _entry(
                instrument_node_id="instrument:mic:XPAR:symbol:TTE.PA",
                symbol="TTE.PA",
                mic="XPAR",
                issuer_entity_id="issuer:yahoo:v1:XPAR:TTE.PA",
            ),
            "instrument:mic:XNAS:symbol:AAPL": IssuerEntry(
                instrument_node_id="instrument:mic:XNAS:symbol:AAPL",
                market_venue="US",
                symbol="AAPL",
                mic="XNAS",
                issuer_entity_id="issuer:yahoo:v1:XNAS:AAPL",
                identity_status="verified",
                brief_as_of="2026-09-05T17:26:49+00:00",
                source_refs=("company_micro:v1:AAPL:abc",),
                resolution_method="mapping_only",
            ),
        },
    )
    assert moved.content_sha256 != first.content_sha256


def test_registry_round_trips_and_rejects_mismatches() -> None:
    registry = IssuerRegistry(registry_id="issuer_registry.v1", entries={"instrument:mic:XPAR:symbol:TTE.PA": _entry()})
    payload = registry.to_dict()
    assert payload["schema_version"] == ISSUER_REGISTRY_SCHEMA
    assert IssuerRegistry.from_mapping(payload) == registry
    assert registry.issuer_for_instrument("instrument:mic:XPAR:symbol:TTE.PA") == _entry()
    assert registry.issuer_for_instrument("instrument:mic:XPAR:symbol:OTHER") is None
    with pytest.raises(ValueError, match="content_sha256"):
        IssuerRegistry.from_mapping({**payload, "content_sha256": "0" * 64})
    with pytest.raises(ValueError, match="must equal the entry"):
        IssuerRegistry.from_mapping(
            {**payload, "entries": {"instrument:mic:XPAR:symbol:OTHER": _entry().to_dict()}}
        )
    with pytest.raises(ValueError, match="unknown fields"):
        IssuerRegistry.from_mapping({**payload, "owner": "desk"})


def test_world_issuer_registry_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
