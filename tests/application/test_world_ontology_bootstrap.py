from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_ports import WorldOntologyReadinessPort
from trader.application.world_model.graph_snapshot import expected_scope_heads
from trader.application.world_model.ontology_bootstrap import (
    MARKET_ONTOLOGY_REVISION_ID,
    WorldOntologyAttestation,
    WorldOntologyBootstrapService,
    derive_market_ontology,
)
from trader.application.world_model.ontology_service import WorldOntologyService
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_graph import WorldEntityRef, WorldStructuralRelationRef
from trader.domain.world_ontology_lifecycle import WorldOntologyLifecycleSpec
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
CONFIG_DIR = REPO_ROOT / "config"
_BOOTSTRAP = REPO_ROOT / "trader" / "application" / "world_model" / "ontology_bootstrap.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _import_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel_path = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{rel_path}: import {alias.name}")
    source = path.read_text(encoding="utf-8")
    if "world_graph_store" in source:
        violations.append(f"{rel_path}: world_graph_store mentioned")
    return violations


def test_ontology_bootstrap_is_application_owned_without_infrastructure() -> None:
    assert _BOOTSTRAP.exists()
    assert _import_violations(_BOOTSTRAP) == []
    assert callable(WorldOntologyBootstrapService.readiness)
    assert callable(WorldOntologyBootstrapService.ensure_published)
    assert callable(WorldOntologyAttestation.ensure_published)
    assert callable(WorldOntologyAttestation.proven_heads)
    assert WorldOntologyReadinessPort.__name__ == "WorldOntologyReadinessPort"


def test_derive_market_ontology_uses_exact_mapping_heads_without_issuer_or_suffix_fallback() -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    entities, relations, revision = derive_market_ontology(mapping)
    kinds = {entity.kind for entity in entities}
    assert kinds == {"instrument", "venue", "country", "region", "world"}
    assert all(entity.kind != "company" for entity in entities)
    assert {relation.kind for relation in relations} <= {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}
    assert all("CAUS" not in relation.kind for relation in relations)
    actual = frozenset((item.kind, item.source.node_id, item.target.node_id) for item in relations)
    assert actual == expected_scope_heads(mapping)
    instrument_ids = {entity.entity_id for entity in entities if entity.kind == "instrument"}
    assert any(item.endswith(":symbol:2301.TW") for item in instrument_ids)
    assert any(item.endswith(":symbol:GM") for item in instrument_ids)
    assert not any(item.endswith(":symbol:2301") for item in instrument_ids)
    assert revision.revision_id == MARKET_ONTOLOGY_REVISION_ID
    assert revision.scope_mapping_id == mapping.mapping_id
    assert revision.scope_mapping_hash == mapping.content_sha256
    assert revision.identity_link_refs == ()
    second = derive_market_ontology(mapping)[2]
    assert second.content_sha256 == revision.content_sha256


def test_committed_market_ontology_bootstrap_is_deterministic_and_idempotent_on_restart(tmp_path: Path) -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    path = tmp_path / "world_model.db"

    def clock() -> datetime:
        return CUTOFF

    first = WorldGraphStore(path, clock=clock)
    try:
        service = WorldOntologyBootstrapService(first, mapping)
        assert service.readiness(CUTOFF).status == "unpublished"
        ready = service.ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        assert ready.revision_id == MARKET_ONTOLOGY_REVISION_ID
        entity_events = first.list_entity_events_available_through(CUTOFF)
        relation_events = first.list_structural_relation_events_available_through(CUTOFF)
        revision_events = first.list_revision_events_available_through(CUTOFF)
        kinds = {envelope.event.entity.kind for envelope in entity_events}
        assert kinds <= {"instrument", "venue", "country", "region", "world"}
        assert "company" not in kinds
        relation_kinds = {
            getattr(envelope.event, "relation").kind
            for envelope in relation_events
            if getattr(envelope.event, "relation", None) is not None
        }
        assert relation_kinds <= {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}
        view = WorldOntologyService(first).ontology.at_cutoff(CUTOFF)
        actual = {(item.kind, item.source.node_id, item.target.node_id) for item in view.structural_relations}
        assert actual == expected_scope_heads(mapping)
        assert view.published_revision is not None
        asserted_refs = {
            WorldStructuralRelationRef.from_relation(item.event.relation)
            for item in relation_events
            if getattr(item.event, "relation", None) is not None
        }
        assert frozenset(view.published_revision.structural_relation_refs) == asserted_refs
        hashes = ready.ontology_hash
    finally:
        first.close()

    second = WorldGraphStore(path, clock=clock)
    try:
        again = WorldOntologyBootstrapService(second, mapping).ensure_published(now=CUTOFF)
        assert again.status == "ready"
        assert again.ontology_hash == hashes
        assert len(second.list_entity_events_available_through(CUTOFF)) == len(entity_events)
        assert len(second.list_structural_relation_events_available_through(CUTOFF)) == len(relation_events)
        assert len(second.list_revision_events_available_through(CUTOFF)) == len(revision_events)
        assert WorldOntologyBootstrapService(second, mapping).readiness(CUTOFF).status == "ready"
    finally:
        second.close()


def test_ontology_attestation_is_the_single_proof_query_and_does_not_fabricate_heads(
    tmp_path: Path,
) -> None:
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof
    from trader.domain.world_feature_contract import (
        WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        WORLD_SCOPE_MAPPING_ID,
        WORLD_SCOPE_MAPPING_SHA256,
    )

    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    path = tmp_path / "world_model.db"

    def clock() -> datetime:
        return CUTOFF

    store = WorldGraphStore(path, clock=clock)
    try:
        attestation = WorldOntologyAttestation(store, mapping)
        assert attestation.proven_heads(
            revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
            at=CUTOFF,
        ) is None
        assert attestation.readiness(CUTOFF).status == "unpublished"
        ready = attestation.ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        proof = attestation.proven_heads(
            revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
            at=CUTOFF,
        )
        assert isinstance(proof, WorldOntologyHeadsProof)
        assert proof.revision_id == WORLD_GRAPH_V3_ONTOLOGY_REVISION
        assert proof.scope_mapping_id == mapping.mapping_id
        assert proof.scope_mapping_hash == mapping.content_sha256
        assert proof.content_sha256 == ready.ontology_hash
        assert attestation.proven_heads(
            revision_id="semantic_catalog.v1",
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
            at=CUTOFF,
        ) is None
        again = attestation.ensure_published(now=CUTOFF)
        assert again.ontology_hash == ready.ontology_hash
        assert len(store.list_revision_events_available_through(CUTOFF)) == 1
    finally:
        store.close()


def test_unmapped_instrument_is_not_invented_by_bootstrap() -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    entities, _relations, _revision = derive_market_ontology(mapping)
    unknown = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:9999")
    assert unknown not in entities
    assert all(not entity.entity_id.endswith(":symbol:9999") for entity in entities)


def _scope(kind: str, entity_id: str) -> WorldCanonicalScopeRef:
    return WorldCanonicalScopeRef(kind=kind, entity_id=entity_id)


def _entry(*, market_venue: str, instrument: str, venue: str, country: str, region: str) -> WorldScopeMappingEntry:
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument),
        venue=_scope("venue", venue),
        country=_scope("country", country),
        region=_scope("region", region),
        world=_scope("world", "market"),
        provider_proofs=(f"provider:{instrument}",),
        taxonomy_version="sessions_mic.v1",
    )


def _predecessor_spec() -> WorldOntologyLifecycleSpec:
    return WorldOntologyLifecycleSpec(
        successor_revision_id="market_ontology.v1",
        predecessor_revision_id="market_ontology.v0",
        successor_mapping_id="world_scope_mapping.v1",
        predecessor_mapping_id="world_scope_mapping.v0",
        predecessor_mapping_sha256="0" * 64,
    )


def _successor_spec(*, predecessor_mapping_sha256: str) -> WorldOntologyLifecycleSpec:
    return WorldOntologyLifecycleSpec(
        successor_revision_id="market_ontology.v2",
        predecessor_revision_id="market_ontology.v1",
        successor_mapping_id="world_scope_mapping.v2",
        predecessor_mapping_id="world_scope_mapping.v1",
        predecessor_mapping_sha256=predecessor_mapping_sha256,
    )


def test_boot_supersedes_persisted_predecessor_without_in_place_conflict(tmp_path: Path) -> None:
    from trader.domain.world_graph import WorldOntologyRevisionPublished, WorldOntologyRevisionSuperseded

    v1_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(_entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),),
    )
    v2_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v2",
        entries=(
            _entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),
            _entry(market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"),
        ),
    )
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        predecessor = WorldOntologyBootstrapService(
            store,
            v1_mapping,
            revision_id="market_ontology.v1",
            lifecycle_spec=_predecessor_spec(),
        )
        first = predecessor.ensure_published(now=CUTOFF)
        assert first.status == "ready"
        assert first.revision_id == "market_ontology.v1"
        successor = WorldOntologyBootstrapService(
            store,
            v2_mapping,
            lifecycle_spec=_successor_spec(predecessor_mapping_sha256=v1_mapping.content_sha256),
        )
        pending = successor.readiness(CUTOFF)
        assert pending.status == "unpublished"
        assert pending.reason == "predecessor_published"
        published = successor.ensure_published(now=CUTOFF)
        assert published.status == "ready"
        assert published.revision_id == MARKET_ONTOLOGY_REVISION_ID == "market_ontology.v2"
        assert published.scope_mapping_id == "world_scope_mapping.v2"
        events = [envelope.event for envelope in store.list_revision_events_available_through(CUTOFF)]
        assert any(isinstance(event, WorldOntologyRevisionSuperseded) for event in events)
        assert isinstance(events[-1], WorldOntologyRevisionPublished)
        assert events[-1].revision.revision_id == "market_ontology.v2"
        view = WorldOntologyService(store).ontology.at_cutoff(CUTOFF)
        assert view.published_revision is not None
        assert view.published_revision.revision_id == "market_ontology.v2"
        again = successor.ensure_published(now=CUTOFF)
        assert again.ontology_hash == published.ontology_hash
        assert len(store.list_revision_events_available_through(CUTOFF)) == len(events)
        instrument_ids = {entity.entity_id for entity in view.entities if entity.kind == "instrument"}
        assert "mic:XTAI:symbol:2301.TW" in instrument_ids
        assert "mic:XNYS:symbol:GM" in instrument_ids
    finally:
        store.close()


def test_v1_id_with_wrong_mapping_hash_is_drifted_and_never_superseded(tmp_path: Path) -> None:
    from trader.domain.world_feature_contract import WORLD_SCOPE_MAPPING_PREDECESSOR_SHA256
    from trader.domain.world_graph import WorldOntologyRevisionPublished, WorldOntologyRevisionSuperseded

    v1_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(_entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),),
    )
    v2_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v2",
        entries=(
            _entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),
            _entry(market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"),
        ),
    )
    assert v1_mapping.content_sha256 != WORLD_SCOPE_MAPPING_PREDECESSOR_SHA256
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        predecessor = WorldOntologyBootstrapService(
            store,
            v1_mapping,
            revision_id="market_ontology.v1",
            lifecycle_spec=_predecessor_spec(),
        )
        first = predecessor.ensure_published(now=CUTOFF)
        assert first.status == "ready"
        successor = WorldOntologyBootstrapService(store, v2_mapping)
        pending = successor.readiness(CUTOFF)
        assert pending.status == "drifted"
        with pytest.raises(ValueError, match="conflict"):
            successor.ensure_published(now=CUTOFF)
        events = [envelope.event for envelope in store.list_revision_events_available_through(CUTOFF)]
        assert not any(isinstance(event, WorldOntologyRevisionSuperseded) for event in events)
        assert isinstance(events[-1], WorldOntologyRevisionPublished)
        assert events[-1].revision.revision_id == "market_ontology.v1"
        view = WorldOntologyService(store).ontology.at_cutoff(CUTOFF)
        assert view.published_revision is not None
        assert view.published_revision.revision_id == "market_ontology.v1"
        assert view.published_revision.scope_mapping_hash == v1_mapping.content_sha256
    finally:
        store.close()


def test_same_revision_id_hash_drift_stays_a_conflict(tmp_path: Path) -> None:
    first_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v2",
        entries=(_entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),),
    )
    drifted = WorldScopeMapping(
        mapping_id="world_scope_mapping.v2",
        entries=(
            _entry(market_venue="TW", instrument="2301.TW", venue="mic:XTAI", country="iso-3166:TW", region="iso-un-m49:030"),
            _entry(market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"),
        ),
    )
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        WorldOntologyBootstrapService(store, first_mapping).ensure_published(now=CUTOFF)
        drifted_service = WorldOntologyBootstrapService(store, drifted)
        assert drifted_service.readiness(CUTOFF).status == "drifted"
        with pytest.raises(ValueError, match="conflict"):
            drifted_service.ensure_published(now=CUTOFF)
    finally:
        store.close()
