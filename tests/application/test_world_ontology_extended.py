from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.application.world_model.ontology_bootstrap import (
    WorldOntologyBootstrapService,
    derive_extended_ontology,
    derive_market_ontology,
)
from trader.application.world_model.ontology_service import WorldOntologyResolver
from trader.domain.world_family_catalog import FamilyCatalog
from trader.domain.world_issuer_registry import IssuerEntry, IssuerRegistry
from trader.domain.world_ontology_lifecycle import (
    admits_market_ontology_family,
    market_ontology_extended_revision_id,
    market_ontology_revision_id,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _mapping(*symbols: str) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=tuple(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="TW", instrument=symbol),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XTAI"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:TW"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:030"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:listing",),
                taxonomy_version="geo.v1",
            )
            for symbol in symbols
        ),
    )


def _entry(symbol: str) -> IssuerEntry:
    return IssuerEntry(
        instrument_node_id=f"instrument:mic:XTAI:symbol:{symbol}",
        market_venue="TW",
        symbol=symbol,
        mic="XTAI",
        issuer_entity_id=f"issuer:yahoo:v1:XTAI:{symbol}",
        identity_status="verified",
        brief_as_of="2026-09-05T17:26:49+00:00",
        source_refs=(f"company_micro:v1:{symbol}:abc",),
        resolution_method="mapping_plus_exchange",
    )


def _registry(*symbols: str) -> IssuerRegistry:
    return IssuerRegistry(
        registry_id="issuer_registry.v1",
        entries={f"instrument:mic:XTAI:symbol:{symbol}": _entry(symbol) for symbol in symbols},
    )


def _catalog() -> FamilyCatalog:
    return FamilyCatalog.from_grouped("family_catalog.v1", {"v1:semis": ["2330", "2331"]})


def test_extended_derivation_adds_only_sourced_branches() -> None:
    mapping = _mapping("2330", "2331", "2332")
    entities, relations, revision = derive_extended_ontology(mapping, _registry("2330"), _catalog())
    kinds = {(entity.kind, entity.entity_id) for entity in entities}
    assert ("company", "issuer:yahoo:v1:XTAI:2330") in kinds
    assert ("family", "taxonomy:v1:semis") in kinds
    assert not any(kind == "company" and node != "issuer:yahoo:v1:XTAI:2330" for kind, node in kinds)
    heads = {(item.kind, item.source.node_id, item.target.node_id) for item in relations}
    assert ("ISSUED_BY", "instrument:mic:XTAI:symbol:2330", "company:issuer:yahoo:v1:XTAI:2330") in heads
    assert ("MEMBER_OF_FAMILY", "instrument:mic:XTAI:symbol:2330", "family:taxonomy:v1:semis") in heads
    assert ("MEMBER_OF_FAMILY", "instrument:mic:XTAI:symbol:2331", "family:taxonomy:v1:semis") in heads
    assert not any(head[0] == "ISSUED_BY" and "2331" in head[1] for head in heads)
    assert not any("2332" in head[1] and head[0] in {"ISSUED_BY", "MEMBER_OF_FAMILY"} for head in heads)
    issued = next(item for item in relations if item.kind == "ISSUED_BY")
    assert "issuer_registry.v1:issuer:yahoo:v1:XTAI:2330" in issued.source_refs
    member = next(item for item in relations if item.kind == "MEMBER_OF_FAMILY")
    assert "family_catalog.v1:v1:semis" in member.source_refs


def test_extended_revision_id_is_stable_and_distinct() -> None:
    mapping = _mapping("2330")
    registry, catalog = _registry("2330"), _catalog()
    first = derive_extended_ontology(mapping, registry, catalog)[2]
    second = derive_extended_ontology(mapping, registry, catalog)[2]
    assert first.revision_id == second.revision_id
    assert first.content_sha256 == second.content_sha256
    assert admits_market_ontology_family(first.revision_id)
    assert first.revision_id != market_ontology_revision_id(mapping)
    assert first.revision_id == market_ontology_extended_revision_id(
        mapping, registry_sha256=registry.content_sha256 or "", catalog_sha256=catalog.content_sha256 or ""
    )
    altered_registry = derive_extended_ontology(mapping, _registry(), catalog)[2]
    assert altered_registry.revision_id != first.revision_id
    altered_catalog = derive_extended_ontology(
        mapping, registry, FamilyCatalog.from_grouped("family_catalog.v1", {"v1:other": ["2330"]})
    )[2]
    assert altered_catalog.revision_id != first.revision_id


def test_extended_derivation_rejects_empty_and_wrong_inputs() -> None:
    mapping = _mapping("2330")
    with pytest.raises(ValueError, match="requires issuer or family inputs"):
        derive_extended_ontology(mapping, _registry(), FamilyCatalog(catalog_id="c", entries={}))
    with pytest.raises(TypeError, match="issuers must be IssuerRegistry"):
        derive_extended_ontology(mapping, {}, _catalog())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="families must be FamilyCatalog"):
        derive_extended_ontology(mapping, _registry("2330"), {})  # type: ignore[arg-type]


def test_legacy_derivation_is_unchanged_by_extension() -> None:
    mapping = _mapping("2330", "2331")
    legacy = derive_market_ontology(mapping)
    assert {entity.kind for entity in legacy[0]} == {"instrument", "venue", "country", "region", "world"}
    assert {relation.kind for relation in legacy[1]} == {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}


def _service(tmp_path: Path, mapping: WorldScopeMapping, **kwargs):  # type: ignore[no-untyped-def]
    store = WorldGraphStore(tmp_path / "world_model.db", clock=lambda: NOW)
    service = WorldOntologyBootstrapService(store, mapping, **kwargs)
    return service, store


def test_bootstrap_guards_extended_inputs(tmp_path: Path) -> None:
    mapping = _mapping("2330")
    with pytest.raises(ValueError, match="provided together"):
        _service(tmp_path, mapping, issuer_registry=_registry("2330"))
    with pytest.raises(ValueError, match="provided together"):
        _service(tmp_path, mapping, family_catalog=_catalog())


def test_extended_bootstrap_publishes_and_stays_ready(tmp_path: Path) -> None:
    mapping = _mapping("2330", "2331")
    service, store = _service(
        tmp_path, mapping, issuer_registry=_registry("2330"), family_catalog=_catalog()
    )
    first = service.ensure_published(now=NOW)
    assert first.status == "ready"
    assert first.reason == "published"
    assert service.readiness(NOW).status == "ready"
    assert service.ensure_published(now=NOW).reason == "attested"
    view = WorldOntologyResolver(store).at_cutoff(NOW)
    kinds = {entity.kind for entity in view.entities}
    assert {"company", "family"} <= kinds


def test_legacy_then_extended_is_a_clean_supersede(tmp_path: Path) -> None:
    mapping = _mapping("2330")
    legacy, _legacy_store = _service(tmp_path, mapping)
    published = legacy.ensure_published(now=NOW)
    assert published.status == "ready"
    legacy_id = legacy.expected_revision().revision_id
    extended, store = _service(
        tmp_path, mapping, issuer_registry=_registry("2330"), family_catalog=_catalog()
    )
    assert extended.readiness(NOW).status == "unpublished"
    result = extended.ensure_published(now=NOW)
    assert result.status == "ready"
    assert result.reason == "superseded"
    assert result.revision_id != legacy_id
    view = WorldOntologyResolver(store).at_cutoff(NOW)
    assert view.published_revision is not None
    assert view.published_revision.revision_id == result.revision_id
    assert any(entity.kind == "company" for entity in view.entities)


def test_brief_refresh_keeps_same_revision_id_and_content(tmp_path: Path) -> None:
    from dataclasses import replace

    mapping = _mapping("2330", "2331")
    catalog = _catalog()
    first = _registry("2330", "2331")
    refreshed = IssuerRegistry(
        registry_id="issuer_registry.v1",
        entries={
            node: replace(
                entry,
                brief_as_of="2026-09-16T12:00:00+00:00",
                source_refs=(f"company_micro:v1:{entry.symbol}:refreshed",),
            )
            for node, entry in first.entries.items()
        },
    )
    assert refreshed.content_sha256 == first.content_sha256
    _, _, revision_before = derive_extended_ontology(mapping, first, catalog)
    _, _, revision_after = derive_extended_ontology(mapping, refreshed, catalog)
    assert revision_after.revision_id == revision_before.revision_id
    assert revision_after.content_sha256 == revision_before.content_sha256
    service, _store = _service(
        tmp_path, mapping, issuer_registry=first, family_catalog=catalog
    )
    assert service.ensure_published(now=NOW).status == "ready"
    reread, _reread_store = _service(
        tmp_path, mapping, issuer_registry=refreshed, family_catalog=catalog
    )
    assert reread.readiness(NOW).status == "ready"
