from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_ports import WorldOntologyReadinessPort
from trader.application.world_model.graph_snapshot import expected_scope_heads
from trader.application.world_model.ontology_bootstrap import (
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
            if any(
                node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES
            ):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES
                ):
                    violations.append(f"{rel_path}: import {alias.name}")
    source = path.read_text(encoding="utf-8")
    if "world_graph_store" in source:
        violations.append(f"{rel_path}: world_graph_store mentioned")
    return violations


def test_ontology_bootstrap_is_application_owned_without_infrastructure() -> None:
    assert _BOOTSTRAP.exists()
    assert _import_violations(_BOOTSTRAP) == []
    source = _BOOTSTRAP.read_text(encoding="utf-8")
    assert "generation_pending" not in source
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
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id, require_committed_ontology_revision

    assert revision.revision_id == market_ontology_revision_id(mapping)
    assert revision.scope_mapping_id == mapping.mapping_id
    assert revision.scope_mapping_hash == mapping.content_sha256
    require_committed_ontology_revision(revision, mapping)
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
        from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

        assert ready.revision_id == market_ontology_revision_id(mapping)
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
    from trader.domain.world_feature_contract import WORLD_SCOPE_MAPPING_ID
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    path = tmp_path / "world_model.db"
    revision_id = market_ontology_revision_id(mapping)

    def clock() -> datetime:
        return CUTOFF

    store = WorldGraphStore(path, clock=clock)
    try:
        attestation = WorldOntologyAttestation(store, mapping)
        assert (
            attestation.proven_heads(
                revision_id=revision_id,
                scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
                scope_mapping_hash=mapping.content_sha256,
                at=CUTOFF,
            )
            is None
        )
        assert attestation.readiness(CUTOFF).status == "unpublished"
        ready = attestation.ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        proof = attestation.proven_heads(
            revision_id=revision_id,
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=mapping.content_sha256,
            at=CUTOFF,
        )
        assert isinstance(proof, WorldOntologyHeadsProof)
        assert proof.revision_id == revision_id
        assert proof.scope_mapping_id == mapping.mapping_id
        assert proof.scope_mapping_hash == mapping.content_sha256
        assert proof.content_sha256 == ready.ontology_hash
        assert (
            attestation.proven_heads(
                revision_id="semantic_catalog.v1",
                scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
                scope_mapping_hash=mapping.content_sha256,
                at=CUTOFF,
            )
            is None
        )
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


def test_same_db_publishes_generation_a_then_b_and_preserves_pit(tmp_path: Path) -> None:
    from trader.domain.world_graph import WorldOntologyRevisionPublished, WorldOntologyRevisionSuperseded
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    generation_a = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
        ),
    )
    generation_b = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
            _entry(
                market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"
            ),
        ),
    )
    times = {"now": CUTOFF}

    def clock() -> datetime:
        return times["now"]

    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=clock)
    try:
        first = WorldOntologyBootstrapService(store, generation_a).ensure_published(now=CUTOFF)
        assert first.status == "ready"
        assert first.revision_id == market_ontology_revision_id(generation_a)
        view_a = WorldOntologyService(store).ontology.at_cutoff(CUTOFF)
        assert view_a.published_revision is not None
        assert view_a.published_revision.revision_id == first.revision_id
        assert not any(entity.entity_id.endswith(":symbol:GM") for entity in view_a.published_revision.entities)

        later = CUTOFF.replace(minute=1)
        times["now"] = later
        second = WorldOntologyBootstrapService(store, generation_b).ensure_published(now=later)
        assert second.status == "ready"
        assert second.revision_id == market_ontology_revision_id(generation_b)
        assert second.revision_id != first.revision_id

        old = WorldOntologyService(store).ontology.at_cutoff(CUTOFF)
        assert old.published_revision is not None
        assert old.published_revision.revision_id == first.revision_id
        assert not any(entity.entity_id.endswith(":symbol:GM") for entity in old.published_revision.entities)

        current = WorldOntologyService(store).ontology.at_cutoff(later)
        assert current.published_revision is not None
        assert current.published_revision.revision_id == second.revision_id
        assert any(entity.entity_id.endswith(":symbol:GM") for entity in current.published_revision.entities)

        events = [envelope.event for envelope in store.list_revision_events_available_through(later)]
        assert any(isinstance(event, WorldOntologyRevisionPublished) for event in events)
        assert any(isinstance(event, WorldOntologyRevisionSuperseded) for event in events)
        before = len(store.list_revision_events_available_through(later))
        idempotent = WorldOntologyBootstrapService(store, generation_b).ensure_published(now=later)
        assert idempotent.status == "ready"
        assert idempotent.revision_id == second.revision_id
        assert len(store.list_revision_events_available_through(later)) == before
    finally:
        store.close()


def test_proven_heads_resolves_superseded_pin_at_live_cutoff(tmp_path: Path) -> None:
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    generation_a = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
        ),
    )
    generation_b = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
            _entry(
                market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"
            ),
        ),
    )
    times = {"now": CUTOFF}

    def clock() -> datetime:
        return times["now"]

    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=clock)
    try:
        WorldOntologyBootstrapService(store, generation_a).ensure_published(now=CUTOFF)
        later = CUTOFF.replace(minute=1)
        times["now"] = later
        WorldOntologyBootstrapService(store, generation_b).ensure_published(now=later)
        revision_a = market_ontology_revision_id(generation_a)
        attestation = WorldOntologyAttestation(store, generation_b)
        proof = attestation.proven_heads(
            revision_id=revision_a,
            scope_mapping_id=generation_a.mapping_id,
            scope_mapping_hash=generation_a.content_sha256,
            at=later,
        )
        assert isinstance(proof, WorldOntologyHeadsProof)
        assert proof.revision_id == revision_a
        assert (
            attestation.proven_heads(
                revision_id="market_ontology:v1:" + "0" * 64,
                scope_mapping_id=generation_a.mapping_id,
                scope_mapping_hash=generation_a.content_sha256,
                at=later,
            )
            is None
        )
    finally:
        store.close()


def test_same_revision_id_hash_drift_stays_a_conflict_and_next_generation_supersedes(
    tmp_path: Path,
) -> None:
    first_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
        ),
    )
    drifted = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
            _entry(
                market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"
            ),
        ),
    )
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        _, _, first_revision = derive_market_ontology(first_mapping)
        first_spec = WorldOntologyLifecycleSpec(
            revision_id=first_revision.revision_id,
            mapping_id=first_mapping.mapping_id,
            revision_hash=first_revision.content_sha256,
            mapping_sha256=first_mapping.content_sha256,
        )
        WorldOntologyBootstrapService(store, first_mapping, lifecycle_spec=first_spec).ensure_published(now=CUTOFF)
        drifted_service = WorldOntologyBootstrapService(store, drifted, lifecycle_spec=first_spec)
        drifted_readiness = drifted_service.readiness(CUTOFF)
        assert drifted_readiness.status == "drifted"
        # The drift reason carries the underlying cause instead of
        # swallowing it: a boot warning must name the real conflict.
        assert drifted_readiness.reason.startswith("revision_drift:")
        assert len(drifted_readiness.reason) > len("revision_drift:")
        with pytest.raises(ValueError, match="revision_drift"):
            drifted_service.ensure_published(now=CUTOFF)
        next_revision = derive_market_ontology(drifted)[2]
        next_spec = WorldOntologyLifecycleSpec(
            revision_id=next_revision.revision_id,
            mapping_id=drifted.mapping_id,
            revision_hash=next_revision.content_sha256,
            mapping_sha256=drifted.content_sha256,
        )
        pending = WorldOntologyBootstrapService(store, drifted, lifecycle_spec=next_spec)
        ready = pending.readiness(CUTOFF)
        assert ready.status == "unpublished"
        assert ready.reason == "generation_supersede"
        held = pending.ensure_published(now=CUTOFF)
        assert held.status == "ready"
        assert held.revision_id == next_revision.revision_id
        assert held.ontology_hash == next_revision.content_sha256
    finally:
        store.close()


def test_committed_bootstrap_derives_generation_and_refuses_v2_contract(tmp_path: Path) -> None:
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        service = WorldOntologyBootstrapService(store, mapping)
        expected = service.expected_revision()
        assert expected.revision_id == market_ontology_revision_id(mapping)
        assert expected.revision_id.startswith("market_ontology:v1:")
        assert expected.scope_mapping_hash == mapping.content_sha256
        ready = service.ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        assert ready.ontology_hash == expected.content_sha256
        assert ready.revision_id == expected.revision_id
    finally:
        store.close()

    synthetic = WorldScopeMapping(
        mapping_id="world_scope_mapping.v2",
        entries=(
            _entry(
                market_venue="TW",
                instrument="2301.TW",
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            ),
        ),
    )
    with pytest.raises(ValueError, match="mapping_id"):
        from trader.domain.world_ontology_lifecycle import market_ontology_revision_id as revision_id_for

        revision_id_for(synthetic)
    drifted_path = tmp_path / "drifted.db"
    drifted_store = WorldGraphStore(drifted_path, clock=lambda: CUTOFF)
    try:
        with pytest.raises(ValueError, match="mapping_id"):
            WorldOntologyBootstrapService(drifted_store, synthetic).ensure_published(now=CUTOFF)
    finally:
        drifted_store.close()


def _generation(*instruments: str) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=tuple(
            _entry(
                market_venue="TW",
                instrument=instrument,
                venue="mic:XTAI",
                country="iso-3166:TW",
                region="iso-un-m49:030",
            )
            for instrument in instruments
        ),
    )


def test_supersede_is_recorded_before_the_successor_publish(tmp_path: Path) -> None:
    from trader.domain.world_graph import (
        WorldOntologyRevisionPublished,
        WorldOntologyRevisionSuperseded,
    )
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    first, second = _generation("2301.TW"), _generation("2301.TW", "2330.TW")
    old_id = market_ontology_revision_id(first)
    new_id = market_ontology_revision_id(second)
    assert old_id != new_id
    store = WorldGraphStore(tmp_path / "world_model.db", clock=lambda: CUTOFF)
    try:
        assert WorldOntologyBootstrapService(store, first).ensure_published(now=CUTOFF).status == "ready"
        ready = WorldOntologyBootstrapService(store, second).ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        assert ready.reason == "superseded"
        assert ready.revision_id == new_id
        events = [envelope.event for envelope in store.list_revision_events_available_through(CUTOFF)]
        assert [type(event).__name__ for event in events] == [
            "WorldOntologyRevisionPublished",
            "WorldOntologyRevisionSuperseded",
            "WorldOntologyRevisionPublished",
        ]
        superseded = events[1]
        assert isinstance(superseded, WorldOntologyRevisionSuperseded)
        assert superseded.revision_id == old_id
        assert superseded.successor_revision_id == new_id
        assert isinstance(events[2], WorldOntologyRevisionPublished)
        assert events[2].revision.revision_id == new_id
        head = WorldOntologyService(store).ontology.at_cutoff(CUTOFF).published_revision
        assert head is not None
        assert head.revision_id == new_id
    finally:
        store.close()


def test_crash_between_supersede_and_publish_recovers_on_next_sweep(tmp_path: Path) -> None:
    from trader.application.world_model.ontology_service import SupersedeWorldOntologyRevision
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id

    first, second = _generation("2301.TW"), _generation("2301.TW", "2330.TW")
    old_id = market_ontology_revision_id(first)
    new_id = market_ontology_revision_id(second)
    store = WorldGraphStore(tmp_path / "world_model.db", clock=lambda: CUTOFF)
    try:
        assert WorldOntologyBootstrapService(store, first).ensure_published(now=CUTOFF).status == "ready"
        # Simulate a crash after the supersede write, before the publish.
        WorldOntologyService(store).supersede_revision(
            SupersedeWorldOntologyRevision(revision_id=old_id, successor_revision_id=new_id)
        )
        assert WorldOntologyService(store).ontology.live_head_revision_id(at=CUTOFF) is None
        ready = WorldOntologyBootstrapService(store, second).ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        assert ready.revision_id == new_id
        view = WorldOntologyService(store).ontology.at_cutoff(CUTOFF)
        assert view.published_revision is not None
        assert view.published_revision.revision_id == new_id
        assert len(view.entities) > 0
    finally:
        store.close()


def test_republish_after_blind_first_read_stays_idempotent(tmp_path: Path) -> None:
    """A blind first read must never duplicate the ledger.

    ``first_seen_at`` is reader-local: with a cutoff captured before the
    first read, a committed revision looks absent (``unpublished``). The
    deterministic event ids make the follow-up publish a no-op, so every
    boot converges without writing duplicate generation rows.
    """
    from datetime import timedelta

    mapping = _generation("2301.TW")
    path = tmp_path / "world_model.db"
    writer = WorldGraphStore(path, clock=lambda: CUTOFF)
    try:
        held = WorldOntologyBootstrapService(writer, mapping).ensure_published(now=CUTOFF)
        assert held.status == "ready"
    finally:
        writer.close()
    reader = WorldGraphStore(path, clock=lambda: CUTOFF + timedelta(hours=1))
    try:
        service = WorldOntologyBootstrapService(reader, mapping)
        assert service.readiness(CUTOFF).status == "unpublished"
        confirmed = service.ensure_published(now=CUTOFF)
        assert confirmed.status == "ready"
        assert confirmed.revision_id == held.revision_id
        envelopes = reader.list_revision_events_available_through(datetime.now(timezone.utc))
        published = [item for item in envelopes if item.event.event_type.endswith("published")]
        assert len(published) == 1
    finally:
        reader.close()
