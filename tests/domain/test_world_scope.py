from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import trader.domain.world_scope as scope_mod
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
    WorldScopeResolution,
    _resolve_world_market_anchor,
)


def _scope(kind: str, entity_id: str) -> WorldCanonicalScopeRef:
    return WorldCanonicalScopeRef(kind=kind, entity_id=entity_id)


def _entry(
    *,
    market_venue: str,
    instrument: str,
    venue: str,
    country: str,
    region: str = "iso-un-m49:030",
    proofs: tuple[str, ...] = ("provider:listing",),
    taxonomy_version: str = "geo.v1",
) -> WorldScopeMappingEntry:
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument),
        venue=_scope("venue", venue),
        country=_scope("country", country),
        region=_scope("region", region),
        world=_scope("world", "market"),
        provider_proofs=proofs,
        taxonomy_version=taxonomy_version,
    )


def _mapping(*entries: WorldScopeMappingEntry) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=entries,
    )


def test_logical_market_venues_are_never_defaulted_to_a_mic() -> None:
    mapping = _mapping(
        _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"),
        _entry(market_venue="US", instrument="AAPL", venue="mic:XNAS", country="iso-3166:US", region="iso-un-m49:021"),
    )
    for venue in ("TW", "US", "EU"):
        unresolved = mapping.resolve(WorldMarketAnchorRef(market_venue=venue, instrument="UNMAPPED"))
        assert unresolved.status == "unmapped"
        assert unresolved.scopes == ()
        assert unresolved.mapping_id == "world_scope_mapping.v1"
        assert unresolved.mapping_sha256 == mapping.content_sha256

    with pytest.raises(ValueError, match="instrument"):
        WorldMarketAnchorRef(market_venue="US", instrument=" ")


def test_resolve_returns_ordered_canonical_scopes_for_an_exact_anchor() -> None:
    mapping = _mapping(
        _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"),
    )
    resolved = mapping.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2330"))
    assert resolved.status == "resolved"
    assert [scope.kind for scope in resolved.scopes] == ["venue", "country", "region", "world"]
    assert [scope.entity_id for scope in resolved.scopes] == [
        "mic:XTAI",
        "iso-3166:TW",
        "iso-un-m49:030",
        "market",
    ]
    assert resolved.anchor == WorldMarketAnchorRef(market_venue="TW", instrument="2330")
    replayed = WorldScopeResolution.from_mapping(resolved.to_dict())
    assert replayed == resolved


def test_incompatible_matches_are_ambiguous_and_do_not_pick_a_mic() -> None:
    left = _entry(market_venue="US", instrument="MSFT", venue="mic:XNAS", country="iso-3166:US", region="iso-un-m49:021")
    right = _entry(market_venue="US", instrument="MSFT", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021")
    mapping_hash = "c" * 64
    status = _resolve_world_market_anchor(
        (left, right),
        WorldMarketAnchorRef(market_venue="US", instrument="MSFT"),
        mapping_id="world_scope_mapping.v1",
        mapping_sha256=mapping_hash,
    )
    assert status.status == "ambiguous"
    assert status.scopes == ()
    assert status.mapping_id == "world_scope_mapping.v1"
    assert status.mapping_sha256 == mapping_hash
    with pytest.raises(ValueError, match="conflict"):
        _mapping(left, right)


def test_resolution_requires_versioned_mapping_identity() -> None:
    entry = _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW")
    anchor = WorldMarketAnchorRef(market_venue="TW", instrument="2330")
    assert "resolve_world_market_anchor" not in scope_mod.__all__
    assert not hasattr(scope_mod, "resolve_world_market_anchor")
    with pytest.raises(TypeError):
        _resolve_world_market_anchor((entry,), anchor)
    with pytest.raises(ValueError, match="mapping_id"):
        _resolve_world_market_anchor((entry,), anchor, mapping_id=" ", mapping_sha256="c" * 64)
    with pytest.raises(ValueError, match="mapping_sha256"):
        _resolve_world_market_anchor(
            (entry,),
            anchor,
            mapping_id="world_scope_mapping.v1",
            mapping_sha256="",
        )
    resolved = _mapping(entry).resolve(anchor)
    assert resolved.mapping_id == "world_scope_mapping.v1"
    assert resolved.mapping_sha256 == _mapping(entry).content_sha256
    payload = resolved.to_dict()
    with pytest.raises(ValueError, match="mapping_id"):
        WorldScopeResolution.from_mapping({**payload, "mapping_id": ""})
    with pytest.raises(ValueError, match="mapping_sha256"):
        WorldScopeResolution.from_mapping({**payload, "mapping_sha256": " "})


def test_mapping_hash_is_deep_and_order_insensitive() -> None:
    first = _mapping(
        _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"),
        _entry(market_venue="US", instrument="AAPL", venue="mic:XNAS", country="iso-3166:US", region="iso-un-m49:021"),
    )
    second = _mapping(
        _entry(market_venue="US", instrument="AAPL", venue="mic:XNAS", country="iso-3166:US", region="iso-un-m49:021"),
        _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"),
    )
    assert first.content_sha256 == second.content_sha256
    assert [entry.anchor.market_venue for entry in first.entries] == ["TW", "US"]
    assert [entry.anchor.market_venue for entry in second.entries] == ["TW", "US"]
    assert first.to_dict()["entries"] == second.to_dict()["entries"]
    replayed = WorldScopeMapping.from_mapping(first.to_dict())
    assert replayed == first
    with pytest.raises(ValueError, match="content_sha256"):
        WorldScopeMapping.from_mapping({**first.to_dict(), "content_sha256": "0" * 64})


def test_mapping_hash_is_insensitive_to_provider_proof_order() -> None:
    left = _mapping(
        _entry(
            market_venue="TW",
            instrument="2330",
            venue="mic:XTAI",
            country="iso-3166:TW",
            proofs=("provider:listing", "provider:mic"),
        )
    )
    right = _mapping(
        _entry(
            market_venue="TW",
            instrument="2330",
            venue="mic:XTAI",
            country="iso-3166:TW",
            proofs=("provider:mic", "provider:listing"),
        )
    )
    assert left.content_sha256 == right.content_sha256
    assert left.entries[0].provider_proofs == ("provider:listing", "provider:mic")
    assert right.entries[0].provider_proofs == ("provider:listing", "provider:mic")


def test_mapping_rejects_redundant_duplicate_anchor_rows() -> None:
    row = _entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW")
    duplicate = _entry(
        market_venue="TW",
        instrument="2330",
        venue="mic:XTAI",
        country="iso-3166:TW",
        proofs=("provider:other",),
    )
    with pytest.raises(ValueError, match="duplicate"):
        _mapping(row, duplicate)
    left = _entry(market_venue="US", instrument="MSFT", venue="mic:XNAS", country="iso-3166:US", region="iso-un-m49:021")
    right = _entry(market_venue="US", instrument="MSFT", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021")
    status = _resolve_world_market_anchor(
        (left, right),
        WorldMarketAnchorRef(market_venue="US", instrument="MSFT"),
        mapping_id="world_scope_mapping.v1",
        mapping_sha256="c" * 64,
    )
    assert status.status == "ambiguous"
    assert status.scopes == ()


def test_scope_refs_and_entries_are_frozen_and_kind_checked() -> None:
    mapping = _mapping(_entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"))
    with pytest.raises(FrozenInstanceError):
        mapping.mapping_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="kind"):
        WorldCanonicalScopeRef(kind="company", entity_id="lei:123")
    with pytest.raises(ValueError, match="market"):
        WorldCanonicalScopeRef(kind="world", entity_id="globe")
    with pytest.raises(ValueError, match="mic:"):
        WorldCanonicalScopeRef(kind="venue", entity_id="XTAI")
    with pytest.raises(ValueError, match="iso-3166:"):
        WorldCanonicalScopeRef(kind="country", entity_id="TW")
    with pytest.raises(ValueError, match="iso-un-m49:"):
        WorldCanonicalScopeRef(kind="region", entity_id="east_asia")
    with pytest.raises(ValueError, match="proof"):
        _entry(
            market_venue="TW",
            instrument="2330",
            venue="mic:XTAI",
            country="iso-3166:TW",
            proofs=(),
        )


def test_mapping_subject_ref_is_content_addressed_without_ready_at() -> None:
    from trader.domain.world_availability import world_subject_content_sha256

    mapping = _mapping(_entry(market_venue="TW", instrument="2330", venue="mic:XTAI", country="iso-3166:TW"))
    ref = mapping.subject_ref()
    payload = mapping.content_payload()
    assert "content_sha256" not in payload
    assert ref.kind == "world_scope_mapping"
    assert ref.subject_id == mapping.mapping_id
    assert ref.content_sha256 == mapping.content_sha256
    assert ref.content_sha256 == world_subject_content_sha256(payload)
    assert world_subject_content_sha256(mapping.to_dict()) != ref.content_sha256
    assert "ready_at" not in inspect.signature(WorldScopeMapping.subject_ref).parameters
    assert "ready_at" not in mapping.to_dict()


def test_resolve_signature_has_no_heuristic_fallback() -> None:
    params = inspect.signature(WorldScopeMapping.resolve).parameters
    assert list(params) == ["self", "anchor"]
    source = (Path(__file__).resolve().parents[2] / "trader" / "domain" / "world_scope.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    assert "xtai" not in lowered
    assert "xnys" not in lowered
    assert "xnas" not in lowered
    assert "xpar" not in lowered
    assert "xlon" not in lowered
    assert "east_asia" not in lowered
    assert "twse" not in lowered