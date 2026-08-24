from __future__ import annotations

import ast
import inspect
import json
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
)
from trader.domain.world_context import ENTITY_KINDS, EntityRef, TopologyEdge
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KNOWLEDGE_RELATION_KINDS,
    STRUCTURAL_RELATION_KINDS,
    WORLD_ENTITY_KINDS,
    WORLD_GRAPH_SNAPSHOT_SCHEMA,
    ancestry_distance_from_instrument_root,
    reconstruct_macro_observes_provenance,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
    MacroGraphBridgeActivated,
    MacroGraphBridgeBlocked,
    MacroGraphBridgeCursorAdvanced,
    MacroGraphBridgeFence,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeResumed,
    MacroGraphBridgeRunHandedOff,
    MacroGraphBridgeRunSpec,
    MacroGraphObservationLinked,
    MacroGraphObservationSkipped,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    MacroObservationKnowledgeLink,
    MacroSourceFactVersionRef,
    PatternHypothesisRef,
    SensorRef,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    StructuralWorldRelationRetired,
    WorldEntityAsserted,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityIdentityLinkRef,
    WorldEntityIdentityLinkSuperseded,
    WorldEntityIdentityMap,
    WorldEntityIdentityUnlinked,
    WorldEntityRef,
    WorldEntityRetired,
    WorldEntityRevisionRef,
    WorldEntitySuperseded,
    WorldGraphSnapshot,
    WorldGraphSnapshotRef,
    WorldKnowledgeRelationRef,
    world_instrument_root_for_resolution,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldStructuralRelationRef,
    evaluate_world_graph_point_in_time,
    fold_knowledge_relation_events_at_cutoff,
    fold_structural_relation_events_at_cutoff,
    parse_macro_graph_bridge_event,
    parse_world_entity_event,
    parse_world_entity_identity_event,
    parse_world_graph_node_ref,
    parse_world_ontology_revision_event,
    parse_world_relation_event,
    reconcile_world_graph_snapshot,
    reconcile_world_ontology_revision,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
    MacroCollectionPlan,
    MacroCollectionTarget,
    MacroCoverage,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroObservationEnvelope,
    MacroObservationId,
    MacroScope,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroWorldObservation,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 8, 23, 12, 5, tzinfo=UTC)
SHA = "a" * 64


def _entity(*, kind: str = "instrument", entity_id: str = "mic:XTAI:symbol:2330") -> WorldEntityRef:
    return WorldEntityRef(kind=kind, entity_id=entity_id)


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _company() -> WorldEntityRef:
    return WorldEntityRef(kind="company", entity_id="lei:549300ABCDEFGHIJKLMN")


def _structural(**overrides: object) -> StructuralWorldRelation:
    values: dict[str, object] = {
        "kind": "TRADED_ON",
        "source": _entity(),
        "target": _venue(),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("provider:instrument-master:2330",),
    }
    values.update(overrides)
    return StructuralWorldRelation(**values)  # type: ignore[arg-type]


def _artifact_ref() -> KnowledgeArtifactRef:
    return KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{SHA}", content_sha256=SHA)


def _observation_ref() -> WorldObservationRef:
    return WorldObservationRef(observation_id=f"world_observation:v1:{SHA}")


def _knowledge(**overrides: object) -> KnowledgeWorldRelation:
    values: dict[str, object] = {
        "kind": "OBSERVES",
        "source": _observation_ref(),
        "target": WorldEntityRef(kind="country", entity_id="iso-3166:TW"),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("macro_world_observation:v1:" + SHA,),
    }
    values.update(overrides)
    return KnowledgeWorldRelation(**values)  # type: ignore[arg-type]


def _link(**overrides: object) -> WorldEntityIdentityLink:
    values: dict[str, object] = {
        "context_ref": EntityRef(kind="instrument", entity_id="2330"),
        "graph_ref": _entity(),
        "source_refs": ("provider:instrument-master:2330",),
        "effective_from": T0,
    }
    values.update(overrides)
    return WorldEntityIdentityLink(**values)  # type: ignore[arg-type]


def _structural_ref(relation: StructuralWorldRelation | None = None) -> WorldStructuralRelationRef:
    resolved = relation if relation is not None else _structural()
    return WorldStructuralRelationRef.from_relation(resolved)


def _knowledge_ref(relation: KnowledgeWorldRelation | None = None) -> WorldKnowledgeRelationRef:
    resolved = relation if relation is not None else _knowledge()
    return WorldKnowledgeRelationRef.from_relation(
        resolved,
        availability_receipt_id=f"world-availability-receipt:v1:{SHA}",
    )


def _revision(**overrides: object) -> WorldOntologyRevision:
    entity = _entity()
    structural = _structural()
    link = _link()
    values: dict[str, object] = {
        "revision_id": "market_ontology.v1",
        "entities": (entity, WorldEntityRef(kind="venue", entity_id="mic:XTAI")),
        "structural_relation_refs": (_structural_ref(structural),),
        "identity_link_refs": (link.as_ref(),),
        "scope_mapping_id": "world_scope_mapping.v1",
        "scope_mapping_hash": SHA,
    }
    values.update(overrides)
    return WorldOntologyRevision(**values)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> WorldGraphSnapshot:
    revision = _revision()
    values: dict[str, object] = {
        "root_episode_id": f"world-episode:v1:{SHA}",
        "root_entity": _entity(),
        "cutoff_at": CUTOFF,
        "ontology_revision": revision.revision_id,
        "ontology_hash": revision.content_sha256,
        "identity_map_hash": revision.identity_map_hash,
        "scope_mapping_id": revision.scope_mapping_id,
        "scope_mapping_hash": revision.scope_mapping_hash,
        "entity_revision_refs": tuple(
            WorldEntityAsserted(
                entity=item,
                source_refs=("provider:instrument-master:2330",),
                effective_from=T0,
            ).as_ref()
            for item in revision.entities
        ),
        "identity_link_refs": tuple(revision.identity_link_refs),
        "structural_relation_refs": frozenset(revision.structural_relation_refs),
        "knowledge_relation_refs": frozenset({_knowledge_ref()}),
        "artifact_refs": (_artifact_ref().artifact_id,),
        "producer_versions": {"graph_snapshot": "world_graph_snapshot.v1"},
        "traversal_policy_version": "graph_traversal.v1",
        "status": "complete",
        "missingness": {},
    }
    values.update(overrides)
    return WorldGraphSnapshot(**values)  # type: ignore[arg-type]


def _attested_receipt(*, subject_id: str, content_sha256: str, ready: datetime = READY) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(
        kind="world_relation",
        subject_id=subject_id,
        content_sha256=content_sha256,
    )
    locator = WorldStorageLocator(kind="jsonl", store_id="world-graph-jsonl.v1", path="relations/2026-08-23.jsonl")
    identity = _receipt_identity_payload(
        schema_version="world_availability_receipt.v1",
        subject=subject,
        scope="instrument:mic:XTAI:symbol:2330",
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready))
    receipt = WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(ready),
            "receipt_sha256": digest,
        }
    )
    return _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )


def test_namespaced_entity_ids_are_closed_immutable_and_hashed() -> None:
    instrument = _entity()
    replayed = WorldEntityRef.from_mapping(instrument.to_dict())
    assert replayed == instrument
    assert instrument.node_id == "instrument:mic:XTAI:symbol:2330"
    assert instrument.to_dict()["node_kind"] == "world_entity"
    with pytest.raises(FrozenInstanceError):
        instrument.entity_id = "2330"  # type: ignore[misc]
    with pytest.raises(ValueError, match="instrument"):
        WorldEntityRef(kind="instrument", entity_id="2330")
    with pytest.raises(ValueError, match="mic:"):
        WorldEntityRef(kind="venue", entity_id="XTAI")
    with pytest.raises(ValueError, match="iso-3166:"):
        WorldEntityRef(kind="country", entity_id="TW")
    with pytest.raises(ValueError, match="one of"):
        WorldEntityRef(kind="sensor", entity_id="world_macro_source.v1")
    world = WorldEntityRef(kind="world", entity_id="market")
    assert world.node_id == "world:market"
    assert WORLD_ENTITY_KINDS.isdisjoint({"sensor", "knowledge_artifact"})


def test_homonymous_symbols_on_two_venues_are_two_instruments() -> None:
    xtai = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")
    xnys = WorldEntityRef(kind="instrument", entity_id="mic:XNYS:symbol:2330")
    assert xtai != xnys
    assert xtai.entity_id != xnys.entity_id
    left = _structural(source=xtai, target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"))
    right = _structural(source=xnys, target=WorldEntityRef(kind="venue", entity_id="mic:XNYS"))
    assert left.relation_id != right.relation_id


def test_company_requires_verified_namespaced_id_and_may_issue_many_instruments() -> None:
    company = _company()
    first = _structural(
        kind="ISSUED_BY",
        source=_entity(),
        target=company,
        source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
    )
    second = _structural(
        kind="ISSUED_BY",
        source=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2454"),
        target=company,
        source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
    )
    assert first.target == second.target
    assert first.relation_id != second.relation_id
    traded = _structural()
    assert traded.kind == "TRADED_ON"
    with pytest.raises(ValueError, match="lei|cik|issuer"):
        WorldEntityRef(kind="company", entity_id="Homonym SA")
    with pytest.raises(ValueError, match="ISIN|isin"):
        WorldEntityRef(kind="company", entity_id="isin:TW0002330008")
    cik = WorldEntityRef(kind="company", entity_id="cik:0000320193")
    issuer = WorldEntityRef(kind="company", entity_id="issuer:twse:2330")
    assert cik.kind == issuer.kind == "company"


def test_node_ref_union_does_not_treat_artifacts_as_world_entities() -> None:
    artifact = _artifact_ref()
    observation = _observation_ref()
    sensor = SensorRef(sensor_id="world_macro_source.v1")
    snapshot_ref = WorldGraphSnapshotRef(snapshot_id=f"world_graph_snapshot:v1:{SHA}")
    hypothesis = PatternHypothesisRef(hypothesis_id=f"pattern_hypothesis:v1:{SHA}")
    fact = MacroSourceFactVersionRef(fact_version_id=f"macro_source_fact_version:v1:{SHA}")
    parsed = [
        parse_world_graph_node_ref(item.to_dict())
        for item in (artifact, observation, sensor, snapshot_ref, hypothesis, fact, _entity())
    ]
    assert [type(item) for item in parsed] == [
        KnowledgeArtifactRef,
        WorldObservationRef,
        SensorRef,
        WorldGraphSnapshotRef,
        PatternHypothesisRef,
        MacroSourceFactVersionRef,
        WorldEntityRef,
    ]
    assert fact.fact_version_id == MacroSourceFactVersionId(f"macro_source_fact_version:v1:{SHA}").value
    with pytest.raises(ValueError, match="world_entity|kind"):
        WorldEntityRef.from_mapping(artifact.to_dict())
    with pytest.raises(ValueError, match="node_kind|unknown"):
        parse_world_graph_node_ref({"node_kind": "prompt", "value": "nope"})


def test_identity_map_links_context_to_graph_and_rejects_ambiguity() -> None:
    empty = WorldEntityIdentityMap.empty()
    linked = empty.link(_link())
    active = linked.active_links_at(CUTOFF)
    assert len(active) == 1
    assert active[0].context_ref == EntityRef(kind="instrument", entity_id="2330")
    assert active[0].graph_ref == _entity()
    assert active[0].source_refs == ("provider:instrument-master:2330",)
    replayed = WorldEntityIdentityMap.from_events([event.to_dict() for event in linked.events])
    assert replayed.active_links_at(CUTOFF) == active
    other_venue = _link(graph_ref=WorldEntityRef(kind="instrument", entity_id="mic:XNYS:symbol:2330"))
    with pytest.raises(ValueError, match="ambiguous|already"):
        linked.link(other_venue)
    with pytest.raises(ValueError, match="kind"):
        _link(context_ref=EntityRef(kind="venue", entity_id="XTAI"), graph_ref=_entity())
    with pytest.raises(ValueError, match="sensor"):
        _link(
            context_ref=EntityRef(kind="sensor", entity_id="news_macro"),
            graph_ref=_entity(kind="event", entity_id="provider:evt-1"),
        )


def test_identity_correction_supersedes_without_rewriting_the_old_link() -> None:
    original = _link()
    mapped = WorldEntityIdentityMap.empty().link(original)
    payload_before = original.to_dict()
    successor = _link(
        graph_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        effective_from=T1,
        supersedes=original.link_id,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    corrected = mapped.supersede(successor)
    assert original.to_dict() == payload_before
    assert original.effective_until is None
    at_t0 = corrected.active_links_at(T0)
    assert len(at_t0) == 1
    assert at_t0[0].link_id == original.link_id
    assert at_t0[0].graph_ref == original.graph_ref
    at_t1 = corrected.active_links_at(T1)
    assert len(at_t1) == 1
    assert at_t1[0].link_id == successor.link_id
    assert at_t1[0].supersedes == original.link_id
    assert isinstance(corrected.events[-1], WorldEntityIdentityLinkSuperseded)
    unlinked = mapped.unlink(original.link_id, retired_at=T1, source_refs=("operator:retire",))
    assert original.to_dict() == payload_before
    assert unlinked.active_links_at(T1) == ()
    assert unlinked.active_links_at(T0)[0].link_id == original.link_id
    assert isinstance(unlinked.events[-1], WorldEntityIdentityUnlinked)


def test_structural_and_knowledge_relation_events_are_distinct() -> None:
    structural = _structural()
    knowledge = _knowledge()
    asserted_s = StructuralWorldRelationAsserted(relation=structural)
    asserted_k = KnowledgeWorldRelationAsserted(relation=knowledge)
    retired_s = StructuralWorldRelationRetired(
        relation_id=structural.relation_id,
        retired_at=T1,
        source_refs=("provider:instrument-master:2330",),
    )
    retired_k = KnowledgeWorldRelationRetired(
        relation_id=knowledge.relation_id,
        retired_at=T1,
        source_refs=("macro_world_observation:v1:" + SHA,),
    )
    assert type(asserted_s) is not type(asserted_k)
    assert type(retired_s) is not type(retired_k)
    assert asserted_s.event_type == asserted_k.event_type == "world_relation_asserted"
    assert asserted_s.family == "structural"
    assert asserted_k.family == "knowledge"
    assert STRUCTURAL_RELATION_KINDS.isdisjoint(KNOWLEDGE_RELATION_KINDS)
    with pytest.raises(ValueError, match="TRADED_ON|structural"):
        KnowledgeWorldRelation(
            kind="TRADED_ON",
            source=_observation_ref(),
            target=_venue(),
            effective_from=T0,
            ontology_revision="market_ontology.v1",
            source_refs=("provider:x",),
        )
    with pytest.raises(ValueError, match="OBSERVES|knowledge"):
        StructuralWorldRelation(
            kind="OBSERVES",
            source=_entity(),
            target=_venue(),
            effective_from=T0,
            ontology_revision="market_ontology.v1",
            source_refs=("provider:x",),
        )
    with pytest.raises(TypeError):
        StructuralWorldRelationAsserted(relation=knowledge)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        KnowledgeWorldRelationAsserted(relation=structural)  # type: ignore[arg-type]


def test_causes_and_hypothesized_influence_are_forbidden_in_factual_relations() -> None:
    assert "CAUSES" in FORBIDDEN_RELATION_KINDS
    for kind in ("CAUSES", "CAUSE", "CAUSED_BY", "CAUSAL", "HypothesizedInfluence"):
        with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
            _structural(kind=kind)
        with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
            _knowledge(kind=kind)
    v2_source = EntityRef(kind="instrument", entity_id="2330")
    v2_target = EntityRef(kind="venue", entity_id="XTAI")
    with pytest.raises(ValueError, match="CAUSES"):
        TopologyEdge(
            kind="CAUSES",
            source=v2_source,
            target=v2_target,
            effective_from=CUTOFF,
            ready_at=CUTOFF,
            ontology_revision="semantic_catalog.v1",
        )


def test_relations_have_exclusive_intervals_and_corrections_do_not_mutate() -> None:
    original = _structural(effective_from=T0, effective_until=None)
    payload = original.to_dict()
    assert original.effective_at(T0)
    assert original.effective_at(CUTOFF)
    bounded = _structural(effective_from=T0, effective_until=T1)
    assert bounded.effective_at(T0)
    assert not bounded.effective_at(T1)
    assert not bounded.effective_at(datetime(2025, 12, 1, tzinfo=UTC))
    correction = original.corrected(
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAF"),
        effective_from=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    assert original.to_dict() == payload
    assert original.effective_until is None
    assert correction.supersedes == original.relation_id
    assert correction.relation_id != original.relation_id
    assert correction.effective_from == T1
    with pytest.raises(ValueError, match="effective_until"):
        _structural(effective_from=T1, effective_until=T0)
    inspect.signature(StructuralWorldRelation).parameters["effective_from"]
    assert "ready_at" not in inspect.signature(StructuralWorldRelation).parameters
    assert "ready_at" not in inspect.signature(KnowledgeWorldRelation).parameters
    assert "ready_at" not in inspect.signature(WorldGraphSnapshot).parameters


def test_point_in_time_gate_hides_late_and_unproven_relation_events() -> None:
    present = _structural(effective_from=T0)
    future = _structural(effective_from=LATER, source_refs=("provider:instrument-master:future",))
    knowledge = _knowledge(effective_from=T0)
    present_evidence = AvailabilityEvidence(
        receipt=_attested_receipt(subject_id=present.relation_id, content_sha256=present.content_sha256),
        first_seen_at=FIRST_SEEN,
    )
    future_evidence = AvailabilityEvidence(
        receipt=_attested_receipt(subject_id=future.relation_id, content_sha256=future.content_sha256),
        first_seen_at=FIRST_SEEN,
    )
    knowledge_evidence = AvailabilityEvidence(
        receipt=_attested_receipt(subject_id=knowledge.relation_id, content_sha256=knowledge.content_sha256),
        first_seen_at=FIRST_SEEN,
    )
    pit_signature = inspect.signature(evaluate_world_graph_point_in_time)
    assert "effective_from" in pit_signature.parameters
    assert pit_signature.parameters["effective_from"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        evaluate_world_graph_point_in_time(evidence=knowledge_evidence, cutoff_at=CUTOFF)
    with pytest.raises(TypeError):
        evaluate_world_graph_point_in_time(
            evidence=knowledge_evidence,
            cutoff_at=CUTOFF,
            effective_from=None,
        )
    assert (
        evaluate_world_graph_point_in_time(
            evidence=knowledge_evidence,
            cutoff_at=CUTOFF,
            effective_from=knowledge.effective_from,
            valid_until=knowledge.effective_until,
        ).status
        == "eligible"
    )
    assert fold_knowledge_relation_events_at_cutoff(
        (KnowledgeWorldRelationAsserted(relation=knowledge),),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={knowledge.relation_id: knowledge_evidence},
    ) == (knowledge,)
    assert PointInTimeEligibilityPolicy().evaluate(evidence=future_evidence, cutoff_at=CUTOFF).status == "eligible"
    assert (
        evaluate_world_graph_point_in_time(
            evidence=future_evidence,
            cutoff_at=CUTOFF,
            effective_from=future.effective_from,
            valid_until=future.effective_until,
        ).status
        != "eligible"
    )
    admitted = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=present),),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={present.relation_id: present_evidence},
    )
    assert admitted == (present,)
    hidden_future = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=future),),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={future.relation_id: future_evidence},
    )
    assert hidden_future == ()
    late_evidence = AvailabilityEvidence(
        receipt=_attested_receipt(subject_id=present.relation_id, content_sha256=present.content_sha256),
        first_seen_at=LATER,
    )
    hidden_late = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=present),),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={present.relation_id: late_evidence},
    )
    assert hidden_late == ()
    hidden_unproven = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=present),),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={},
    )
    assert hidden_unproven == ()
    retired = StructuralWorldRelationRetired(
        relation_id=present.relation_id,
        retired_at=T1,
        source_refs=("provider:instrument-master:2330",),
    )
    payload_before = present.to_dict()
    replayed_retired = parse_world_relation_event(retired.to_dict())
    assert replayed_retired == retired
    early = datetime(2025, 12, 1, tzinfo=UTC)
    early_present = AvailabilityEvidence(
        receipt=_attested_receipt(
            subject_id=present.relation_id,
            content_sha256=present.content_sha256,
            ready=early,
        ),
        first_seen_at=early,
    )
    early_knowledge = AvailabilityEvidence(
        receipt=_attested_receipt(
            subject_id=knowledge.relation_id,
            content_sha256=knowledge.content_sha256,
            ready=early,
        ),
        first_seen_at=early,
    )
    before_retire = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=present), retired),
        cutoff_at=T0,
        evidence_by_relation_id={present.relation_id: early_present},
    )
    after_retire = fold_structural_relation_events_at_cutoff(
        (StructuralWorldRelationAsserted(relation=present), retired),
        cutoff_at=CUTOFF,
        evidence_by_relation_id={present.relation_id: early_present},
    )
    assert before_retire == (present,)
    assert after_retire == ()
    assert present.to_dict() == payload_before
    knowledge_retired = KnowledgeWorldRelationRetired(
        relation_id=knowledge.relation_id,
        retired_at=T1,
        source_refs=("macro_world_observation:v1:" + SHA,),
    )
    assert parse_world_relation_event(knowledge_retired.to_dict()) == knowledge_retired
    assert fold_knowledge_relation_events_at_cutoff(
        (KnowledgeWorldRelationAsserted(relation=knowledge), knowledge_retired),
        cutoff_at=T0,
        evidence_by_relation_id={knowledge.relation_id: early_knowledge},
    ) == (knowledge,)
    assert (
        fold_knowledge_relation_events_at_cutoff(
            (KnowledgeWorldRelationAsserted(relation=knowledge), knowledge_retired),
            cutoff_at=CUTOFF,
            evidence_by_relation_id={knowledge.relation_id: early_knowledge},
        )
        == ()
    )
    bounded = _structural(effective_until=CUTOFF)
    bounded_evidence = AvailabilityEvidence(
        receipt=_attested_receipt(subject_id=bounded.relation_id, content_sha256=bounded.content_sha256),
        first_seen_at=FIRST_SEEN,
    )
    assert (
        evaluate_world_graph_point_in_time(
            evidence=bounded_evidence,
            cutoff_at=CUTOFF,
            effective_from=bounded.effective_from,
            valid_until=bounded.effective_until,
        ).status
        == "stale"
    )
    superseded = evaluate_world_graph_point_in_time(
        evidence=present_evidence,
        cutoff_at=CUTOFF,
        effective_from=present.effective_from,
        superseded=True,
    )
    assert superseded.status == "superseded"


def test_entity_and_revision_lifecycle_events_round_trip() -> None:
    asserted = WorldEntityAsserted(
        entity=_entity(),
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )
    retired = WorldEntityRetired(
        entity=_entity(),
        retired_at=T1,
        source_refs=("provider:instrument-master:2330",),
    )
    superseded = WorldEntitySuperseded(
        entity=_entity(),
        successor=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        superseded_at=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    for event in (asserted, retired, superseded):
        assert parse_world_entity_event(event.to_dict()) == event
    revision = _revision()
    published = WorldOntologyRevisionPublished(revision=revision)
    successor = _revision(revision_id="market_ontology.other")
    superseded_revision = WorldOntologyRevisionSuperseded(
        revision_id=revision.revision_id,
        successor_revision_id=successor.revision_id,
    )
    assert parse_world_ontology_revision_event(published.to_dict()).revision.content_sha256 == revision.content_sha256
    assert parse_world_ontology_revision_event(superseded_revision.to_dict()) == superseded_revision
    knowledge_extra = _knowledge()
    same_without_knowledge = _revision()
    assert revision.content_sha256 == same_without_knowledge.content_sha256
    assert knowledge_extra.kind == "OBSERVES"
    assert "knowledge" not in revision.to_dict()
    poisoned = dict(revision.to_dict())
    poisoned["knowledge_relation_refs"] = [knowledge_extra.to_dict()]
    with pytest.raises(ValueError, match="knowledge"):
        WorldOntologyRevision.from_mapping(poisoned)
    replayed_revision = WorldOntologyRevision.from_mapping(revision.to_dict())
    assert replayed_revision.content_sha256 == revision.content_sha256
    assert revision.identity_map_hash != revision.structural_heads_hash
    other_identity = _revision(
        identity_link_refs=(_link(context_ref=EntityRef(kind="instrument", entity_id="2454")).as_ref(),)
    )
    with pytest.raises(ValueError, match="conflict"):
        reconcile_world_ontology_revision(revision, other_identity)
    assert reconcile_world_ontology_revision(revision, _revision()) == revision


def test_revision_hash_is_independent_of_append_order() -> None:
    venue = _venue()
    country = WorldEntityRef(kind="country", entity_id="iso-3166:TW")
    located = _structural(
        kind="LOCATED_IN",
        source=venue,
        target=country,
        source_refs=("world_scope_mapping.v1",),
    )
    traded = _structural()
    left = _revision(
        entities=(venue, country, _entity()),
        structural_relation_refs=(_structural_ref(traded), _structural_ref(located)),
    )
    right = _revision(
        entities=(_entity(), country, venue),
        structural_relation_refs=(_structural_ref(located), _structural_ref(traded)),
    )
    assert left.content_sha256 == right.content_sha256
    assert left.entity_heads_hash == right.entity_heads_hash
    assert left.structural_heads_hash == right.structural_heads_hash
    map_left = (
        WorldEntityIdentityMap.empty()
        .link(_link())
        .link(
            _link(context_ref=EntityRef(kind="venue", entity_id="XTAI"), graph_ref=_venue(), source_refs=("mic:XTAI",))
        )
    )
    map_right = (
        WorldEntityIdentityMap.empty()
        .link(
            _link(context_ref=EntityRef(kind="venue", entity_id="XTAI"), graph_ref=_venue(), source_refs=("mic:XTAI",))
        )
        .link(_link())
    )
    assert map_left.heads_hash_at(CUTOFF) == map_right.heads_hash_at(CUTOFF)
    assert {event.event_id for event in map_left.events} == {event.event_id for event in map_right.events}


def test_snapshot_keeps_structural_and_knowledge_refs_as_two_sets() -> None:
    structural = _structural_ref()
    knowledge = _knowledge_ref()
    snapshot = _snapshot(
        structural_relation_refs=frozenset({structural}),
        knowledge_relation_refs=frozenset({knowledge}),
    )
    replayed = WorldGraphSnapshot.from_mapping(snapshot.to_dict())
    assert replayed == snapshot
    assert snapshot.structural_relation_refs == frozenset({structural})
    assert snapshot.knowledge_relation_refs == frozenset({knowledge})
    assert structural not in snapshot.knowledge_relation_refs
    assert knowledge not in snapshot.structural_relation_refs
    payload = json.loads(json.dumps(snapshot.to_dict()))
    assert set(payload["structural_relation_refs"][0]) == {"relation_id", "content_sha256"}
    assert "ontology_revision" in payload["knowledge_relation_refs"][0]
    assert "availability_receipt_id" in payload["knowledge_relation_refs"][0]
    assert "content_sha256" in payload["entity_revision_refs"][0]
    assert "entity" in payload["entity_revision_refs"][0]
    assert set(payload["identity_link_refs"][0]) == {"link_id", "content_sha256"}
    assert all(isinstance(item, WorldEntityRevisionRef) for item in snapshot.entity_revision_refs)
    assert all(isinstance(item, WorldEntityIdentityLinkRef) for item in snapshot.identity_link_refs)
    assert inspect.signature(WorldGraphSnapshot).parameters["status"].default == "missing"
    omitted = dict(payload)
    omitted.pop("status")
    omitted.pop("snapshot_id")
    omitted.pop("content_sha256")
    assert WorldGraphSnapshot.from_mapping(omitted).status == "missing"
    with pytest.raises((TypeError, ValueError), match="hash|ref"):
        _snapshot(entity_revision_refs=(_entity().node_id,))
    with pytest.raises((TypeError, ValueError), match="hash|ref"):
        _snapshot(identity_link_refs=(snapshot.identity_link_refs[0].link_id,))
    swapped = _snapshot(
        structural_relation_refs=frozenset(),
        knowledge_relation_refs=frozenset({knowledge}),
    )
    assert swapped.snapshot_id != snapshot.snapshot_id
    assert swapped.content_sha256 != snapshot.content_sha256
    with pytest.raises(ValueError, match="disjoint|both"):
        _snapshot(
            structural_relation_refs=frozenset({structural}),
            knowledge_relation_refs=frozenset(
                {
                    WorldKnowledgeRelationRef(
                        relation_id=structural.relation_id,
                        content_sha256=structural.content_sha256,
                        ontology_revision="market_ontology.v1",
                        availability_receipt_id=f"world-availability-receipt:v1:{SHA}",
                    )
                }
            ),
        )
    assert reconcile_world_graph_snapshot(snapshot, replayed) == snapshot
    with pytest.raises(ValueError, match="conflict"):
        reconcile_world_graph_snapshot(snapshot, swapped)
    assert snapshot.root_entity.kind == "instrument"
    with pytest.raises(ValueError, match="instrument"):
        _snapshot(root_entity=_venue())


def _us_gm_mapping() -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="US", instrument="GM"),
                venue={"kind": "venue", "entity_id": "mic:XNYS"},
                country={"kind": "country", "entity_id": "iso-3166:US"},
                region={"kind": "region", "entity_id": "iso-un-m49:021"},
                world={"kind": "world", "entity_id": "market"},
                provider_proofs=("provider:world-scope:xnys",),
                taxonomy_version="sessions_mic.v1",
            ),
        ),
    )


def _unmapped_snapshot(**overrides: object) -> WorldGraphSnapshot:
    values: dict[str, object] = {
        "root_entity": None,
        "status": "missing",
        "missingness": {"scope": "unmapped"},
        "entity_revision_refs": (),
        "identity_link_refs": (),
        "structural_relation_refs": (),
        "knowledge_relation_refs": (),
        "artifact_refs": (),
    }
    values.update(overrides)
    return _snapshot(**values)


def test_instrument_root_is_exact_resolved_anchor_only() -> None:
    mapping = _us_gm_mapping()
    resolved = mapping.resolve(WorldMarketAnchorRef(market_venue="US", instrument="GM"))
    root = world_instrument_root_for_resolution(resolved, instrument="GM")
    assert root is not None
    assert root.kind == "instrument"
    assert root.entity_id == "mic:XNYS:symbol:GM"

    unmapped = mapping.resolve(WorldMarketAnchorRef(market_venue="US", instrument="AAA"))
    assert unmapped.status == "unmapped"
    assert unmapped.scopes == ()
    assert world_instrument_root_for_resolution(unmapped, instrument="AAA") is None
    with pytest.raises(ValueError, match="instrument"):
        world_instrument_root_for_resolution(resolved, instrument="AAA")


def test_unmapped_snapshot_never_selects_a_world_entity_root_or_topology() -> None:
    fabricated = WorldEntityRef(kind="instrument", entity_id="mic:XNYS:symbol:AAA")
    with pytest.raises(ValueError, match="unmapped|root"):
        _unmapped_snapshot(root_entity=fabricated)
    with pytest.raises(ValueError, match="unmapped|member"):
        _unmapped_snapshot(structural_relation_refs=frozenset({_structural_ref()}))
    with pytest.raises(ValueError, match="root"):
        _snapshot(root_entity=None)

    snapshot = _unmapped_snapshot()
    replayed = WorldGraphSnapshot.from_mapping(snapshot.to_dict())
    assert snapshot.root_entity is None
    assert replayed == snapshot
    assert snapshot.status == "missing"
    assert snapshot.missingness["scope"] == "unmapped"
    assert snapshot.structural_relation_refs == frozenset()
    assert snapshot.knowledge_relation_refs == frozenset()
    payload = snapshot.to_dict()
    assert payload["root_entity"] is None
    assert payload["schema_version"] == WORLD_GRAPH_SNAPSHOT_SCHEMA == "world_graph_snapshot.v1"
    dumped = json.dumps(payload)
    assert "XNYS" not in dumped
    assert "mic:" not in dumped


def test_observation_graph_snapshot_projects_unmapped_fabricated_root_without_selecting_it() -> None:
    from trader.domain.world_graph import observation_graph_snapshot

    fabricated = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:1440.TW")
    with pytest.raises(ValueError, match="unmapped|root"):
        _unmapped_snapshot(root_entity=fabricated)
    honest = _unmapped_snapshot()
    contaminated = honest.to_dict()
    contaminated["root_entity"] = fabricated.to_dict()
    contaminated["entity_revision_refs"] = [
        WorldEntityAsserted(
            entity=fabricated,
            source_refs=("provider:listing",),
            effective_from=T0,
        )
        .as_ref()
        .to_dict()
    ]
    projected = observation_graph_snapshot(contaminated)
    assert projected.root_entity is None
    assert projected.status == "missing"
    assert projected.missingness["scope"] == "unmapped"
    assert projected.entity_revision_refs == ()
    assert projected.identity_link_refs == ()
    assert projected.structural_relation_refs == frozenset()
    assert projected.knowledge_relation_refs == frozenset()
    assert projected.artifact_refs == ()
    dumped = json.dumps(projected.to_dict())
    assert "XTAI" not in dumped
    assert "mic:" not in dumped
    assert "1440.TW" not in dumped
    replayed = observation_graph_snapshot(json.loads(json.dumps(contaminated)))
    assert replayed.root_entity is None
    assert WorldGraphSnapshot.from_mapping(honest.to_dict()) == honest


def test_historical_instrument_root_snapshot_payload_remains_parseable() -> None:
    complete = _snapshot()
    payload = complete.to_dict()
    assert isinstance(payload["root_entity"], dict)
    assert payload["root_entity"]["kind"] == "instrument"
    parsed = WorldGraphSnapshot.from_mapping(json.loads(json.dumps(payload)))
    assert parsed == complete
    assert parsed.root_entity == complete.root_entity

    unpublished = _snapshot(status="missing", missingness={"ontology": "unpublished"})
    unpublished_payload = unpublished.to_dict()
    assert unpublished_payload["root_entity"]["entity_id"] == "mic:XTAI:symbol:2330"
    replayed = WorldGraphSnapshot.from_mapping(unpublished_payload)
    assert replayed == unpublished
    assert replayed.root_entity is not None


def test_context_entity_refs_remain_locally_scoped_and_unrelated_to_graph_ids() -> None:
    context_entity = EntityRef(kind="instrument", entity_id="2330")
    graph_entity = _entity()
    assert context_entity.entity_id == "2330"
    assert graph_entity.entity_id == "mic:XTAI:symbol:2330"
    assert context_entity.to_dict() != graph_entity.to_dict()
    assert "sensor" in ENTITY_KINDS
    assert "country" in ENTITY_KINDS
    assert "region" in ENTITY_KINDS
    assert "sensor" not in WORLD_ENTITY_KINDS
    assert "macro_indicator" in WORLD_ENTITY_KINDS
    country_link = _link(
        context_ref=EntityRef(kind="country", entity_id="iso-3166:TW"),
        graph_ref=WorldEntityRef(kind="country", entity_id="iso-3166:TW"),
    )
    assert country_link.context_ref.kind == country_link.graph_ref.kind == "country"
    edge = TopologyEdge(
        kind="TRADED_ON",
        source=context_entity,
        target=EntityRef(kind="venue", entity_id="XTAI"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
    )
    assert edge.source.entity_id == "2330"
    linked = _link(context_ref=context_entity, graph_ref=graph_entity)
    assert linked.context_ref.entity_id != linked.graph_ref.entity_id


def test_knowledge_endpoints_are_closed_and_structural_endpoints_are_entities() -> None:
    about = _knowledge(kind="ABOUT", source=_artifact_ref(), target=_entity(), source_refs=("artifact:news:1",))
    assert about.source.node_kind == "knowledge_artifact"
    uses = _knowledge(
        kind="USES",
        source=WorldGraphSnapshotRef(snapshot_id=f"world_graph_snapshot:v1:{SHA}"),
        target=_observation_ref(),
        source_refs=("world_graph_snapshot:v1:" + SHA,),
    )
    derived = _knowledge(
        kind="DERIVED_FROM",
        source=_observation_ref(),
        target=MacroSourceFactVersionRef(fact_version_id=f"macro_source_fact_version:v1:{SHA}"),
        source_refs=("macro_world_observation:v1:" + SHA,),
    )
    supersedes = _knowledge(
        kind="SUPERSEDES",
        source=_artifact_ref(),
        target=KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{'b' * 64}", content_sha256="b" * 64),
        source_refs=("artifact:news:2",),
    )
    assert {about.kind, uses.kind, derived.kind, supersedes.kind} <= KNOWLEDGE_RELATION_KINDS
    with pytest.raises(ValueError, match="ABOUT|source"):
        _knowledge(kind="ABOUT", source=_observation_ref(), target=_entity())
    with pytest.raises(ValueError, match="OBSERVES|target"):
        _knowledge(kind="OBSERVES", source=_observation_ref(), target=_artifact_ref())
    with pytest.raises(ValueError, match="LOCATED_IN|source|target"):
        _structural(kind="LOCATED_IN", source=_entity(), target=_venue())
    located = _structural(
        kind="LOCATED_IN",
        source=_venue(),
        target=WorldEntityRef(kind="country", entity_id="iso-3166:TW"),
        source_refs=("world_scope_mapping.v1",),
    )
    part = _structural(
        kind="PART_OF_WORLD",
        source=_venue(),
        target=WorldEntityRef(kind="world", entity_id="market"),
        source_refs=("world_scope_mapping.v1",),
    )
    family = _structural(
        kind="MEMBER_OF_FAMILY",
        source=_entity(),
        target=WorldEntityRef(kind="family", entity_id="taxonomy:v1:semiconductors"),
        source_refs=("taxonomy:v1",),
    )
    assert located.target.kind == "country"
    assert part.target.kind == "world"
    assert family.target.kind == "family"


def test_identity_and_relation_events_parse_as_typed_unions() -> None:
    link = _link()
    linked = WorldEntityIdentityLinked(link=link)
    unlinked = WorldEntityIdentityUnlinked(
        link_id=link.link_id,
        retired_at=T1,
        source_refs=("operator:retire",),
    )
    successor = _link(
        graph_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        effective_from=T1,
        supersedes=link.link_id,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    superseded = WorldEntityIdentityLinkSuperseded(predecessor_link_id=link.link_id, successor=successor)
    assert parse_world_entity_identity_event(linked.to_dict()) == linked
    assert parse_world_entity_identity_event(unlinked.to_dict()) == unlinked
    assert parse_world_entity_identity_event(superseded.to_dict()) == superseded
    asserted = StructuralWorldRelationAsserted(relation=_structural())
    replayed = parse_world_relation_event(asserted.to_dict())
    assert replayed.relation == _structural()
    assert replayed.family == "structural"
    knowledge_asserted = KnowledgeWorldRelationAsserted(relation=_knowledge())
    retired_s = StructuralWorldRelationRetired(
        relation_id=asserted.relation.relation_id,
        retired_at=T1,
        source_refs=("provider:instrument-master:2330",),
    )
    retired_k = KnowledgeWorldRelationRetired(
        relation_id=knowledge_asserted.relation.relation_id,
        retired_at=T1,
        source_refs=("macro_world_observation:v1:" + SHA,),
    )
    assert parse_world_relation_event(knowledge_asserted.to_dict()) == knowledge_asserted
    assert parse_world_relation_event(retired_s.to_dict()) == retired_s
    assert parse_world_relation_event(retired_k.to_dict()) == retired_k
    assert parse_world_relation_event(retired_s.to_dict()).family == "structural"
    assert parse_world_relation_event(retired_k.to_dict()).family == "knowledge"


def test_world_graph_module_is_stdlib_domain_without_networkx() -> None:
    path = REPO_ROOT / "trader" / "domain" / "world_graph.py"
    source = path.read_text(encoding="utf-8")
    assert "networkx" not in source.lower()
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "trader.application" not in source
    assert _domain_import_violations([path], REPO_ROOT) == []
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if node.module.startswith("trader.") and not node.module.startswith("trader.domain"):
                violations.append(node.module)
            elif root != "trader" and root not in sys.stdlib_module_names:
                violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(alias.name)
    assert violations == []
    snapshot = _snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.status = "missing"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.missingness["late"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.producer_versions["x"] = "y"  # type: ignore[index]
    assert "ready_at" not in inspect.signature(evaluate_world_graph_point_in_time).parameters


def test_snapshot_nested_missingness_cannot_corrupt_identity_hash() -> None:
    nested = {"nested": {"items": ["late"]}}
    snapshot = _snapshot(missingness=nested)
    digest = snapshot.content_sha256
    payload = snapshot.to_dict()
    nested["nested"]["items"].append("mutated")
    nested["nested"]["extra"] = True
    with pytest.raises(TypeError):
        snapshot.missingness["nested"]["extra"] = True  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        snapshot.missingness["nested"]["items"].append("via-snapshot")
    assert snapshot.content_sha256 == digest
    assert snapshot.to_dict() == payload
    assert snapshot.to_dict()["missingness"]["nested"]["items"] == ["late"]
    replayed = WorldGraphSnapshot.from_mapping(snapshot.to_dict())
    assert replayed == snapshot
    assert replayed.content_sha256 == digest
    json_roundtrip = WorldGraphSnapshot.from_mapping(json.loads(json.dumps(snapshot.to_dict())))
    assert json_roundtrip == snapshot
    assert json_roundtrip.content_sha256 == digest
    mutated = _snapshot(missingness={"nested": {"items": ["late", "mutated"]}})
    assert mutated.content_sha256 != digest


VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64


def _macro_scope(*, kind: str = "venue", entity_id: str = "mic:XTAI") -> MacroScope:
    return MacroScope(kind=kind, entity_id=entity_id)


def _macro_source() -> MacroFactSource:
    return MacroFactSource(
        provider_id="dbnomics",
        adapter_version="world_dbnomics_series.v1",
        source_record_id="stable-provider-id",
        source_ref="https://source.example/record",
    )


def _macro_fact(**overrides: object) -> MacroSourceFact:
    values: dict[str, object] = {
        "fact_kind": "series_point",
        "metric_key": "policy_rate",
        "scope": _macro_scope(),
        "value": MacroNumericValue(number=4.25, unit="percent"),
        "period": "2026-08",
        "occurred_at": "2026-08-23T00:00:00Z",
        "published_at": "2026-08-23T12:30:00Z",
        "ingested_at": "2026-08-23T12:31:10Z",
        "source": _macro_source(),
    }
    values.update(overrides)
    return MacroSourceFact(**values)  # type: ignore[arg-type]


def _macro_dimension(
    *,
    dimension: str,
    value: str,
    coverage_status: str = "complete",
    fact_refs: tuple[str, ...] = (),
) -> MacroDimensionState:
    return MacroDimensionState(
        dimension=dimension,
        value=value,
        coverage_status=coverage_status,
        method=MACRO_TRANSFORM_VERSION,
        fact_refs=fact_refs,
    )


def _macro_observation(*, scope: MacroScope | None = None, **overrides: object) -> MacroWorldObservation:
    resolved_scope = scope if scope is not None else _macro_scope()
    fact = _macro_fact(scope=resolved_scope)
    fact_ref = fact.fact_version_id.value
    values: dict[str, object] = {
        "scope": resolved_scope,
        "cutoff_at": CUTOFF,
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
        "dimensions": (
            _macro_dimension(dimension="macro_regime", value="mixed", fact_refs=(fact_ref,)),
            _macro_dimension(dimension="rates_regime", value="stable", fact_refs=(fact_ref,)),
            _macro_dimension(dimension="usd_regime", value="unknown", coverage_status="unknown"),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("broad_usd_index",),
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def _observation_envelope(observation: MacroWorldObservation | None = None) -> MacroObservationEnvelope:
    resolved = observation if observation is not None else _macro_observation()
    subject = WorldAvailabilitySubjectRef(
        kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
        subject_id=resolved.observation_id,
        content_sha256=resolved.content_sha256,
    )
    locator = WorldStorageLocator(kind="jsonl", store_id="world-macro-jsonl.v1", path="observations/2026-08-23.jsonl")
    identity = _receipt_identity_payload(
        schema_version="world_availability_receipt.v1",
        subject=subject,
        scope=f"{resolved.scope.kind}:{resolved.scope.entity_id}",
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=READY))
    receipt = WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(READY),
            "receipt_sha256": digest,
        }
    )
    attested = _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )
    return MacroObservationEnvelope(
        observation=resolved,
        persisted=PersistedWorldRef(identity=MacroObservationId(resolved.observation_id), receipt=attested),
        evidence=AvailabilityEvidence(receipt=attested, first_seen_at=FIRST_SEEN),
    )


def _scope_mapping() -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XTAI"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:TW"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:030"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:listing",),
                taxonomy_version="geo.v1",
            ),
        ),
    )


def _revision_for_mapping(mapping: WorldScopeMapping) -> WorldOntologyRevision:
    return _revision(scope_mapping_id=mapping.mapping_id, scope_mapping_hash=mapping.content_sha256)


def _origin_cursor() -> MacroObservationCursor:
    return MacroObservationCursor(receipt_log_generation=1, ordinal=0)


def _cursor(*, ordinal: int, receipt_id: str, observation_id: str) -> MacroObservationCursor:
    return MacroObservationCursor(
        receipt_log_generation=1,
        ordinal=ordinal,
        receipt_id=receipt_id,
        observation_id=observation_id,
    )


def _reservation(
    *,
    cursor: MacroObservationCursor | None = None,
    request_id: str = REQUEST_ID,
) -> MacroObservationCursorReservation:
    return MacroObservationCursorReservation(
        bridge_key=BRIDGE_KEY,
        request_id=request_id,
        cursor=cursor if cursor is not None else _origin_cursor(),
    )


def _collection_plan() -> MacroCollectionPlan:
    return MacroCollectionPlan(
        registry_version="world_macro_sources.v1",
        registry_content_sha256="b" * 64,
        targets=(
            MacroCollectionTarget(
                scope=MacroScope(kind="venue", entity_id="mic:XTAI"),
                source_ids=("fed_policy_rate",),
            ),
        ),
    )


def _run_spec(
    mapping: WorldScopeMapping | None = None, revision: WorldOntologyRevision | None = None
) -> MacroGraphBridgeRunSpec:
    resolved_mapping = mapping if mapping is not None else _scope_mapping()
    resolved_revision = revision if revision is not None else _revision_for_mapping(resolved_mapping)
    plan = _collection_plan()
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=resolved_mapping.mapping_id,
        scope_mapping_hash=resolved_mapping.content_sha256,
        ontology_revision_id=resolved_revision.revision_id,
        ontology_revision_hash=resolved_revision.content_sha256,
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )


def test_macro_observation_knowledge_link_is_deterministic_for_mapping_outputs() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    envelope = _observation_envelope(_macro_observation(scope=_macro_scope(kind="venue", entity_id="mic:XTAI")))
    first = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, revision)
    second = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, revision)
    assert envelope.observation.scope == _macro_scope(kind="venue", entity_id="mic:XTAI")
    assert first.status == "linked"
    assert first.skip_reason is None
    assert first.relation is not None
    assert first.relation.kind == "OBSERVES"
    assert first.relation == second.relation
    assert first.relation.relation_id == second.relation.relation_id
    assert first.relation.effective_from == envelope.observation.cutoff_at
    assert first.relation.effective_until == envelope.observation.valid_until
    assert first.relation.ontology_revision == revision.revision_id
    assert isinstance(first.relation.source, WorldObservationRef)
    assert first.relation.source.observation_id.startswith("world_observation:v1:")
    assert first.relation.target == WorldEntityRef(kind="venue", entity_id="mic:XTAI")
    observation = envelope.observation
    receipt = envelope.persisted.receipt
    from trader.domain.world_macro import MacroObservationProvenance

    provenance = MacroObservationProvenance.from_source_refs(first.relation.source_refs)
    assert provenance.producer_version == observation.producer_version
    assert provenance.origin_scope == observation.scope
    assert provenance.observation_id == observation.observation_id
    assert provenance.observation_sha256 == observation.content_sha256
    assert provenance.fact_refs == observation.fact_refs
    assert provenance.mapping_id == mapping.mapping_id
    assert provenance.mapping_sha256 == mapping.content_sha256
    assert provenance.ancestry_distance is None
    assert f"{receipt.receipt_id}/{receipt.receipt_sha256}" in first.relation.source_refs
    reconstructed = reconstruct_macro_observes_provenance(first.relation)
    assert reconstructed.origin_scope == observation.scope
    assert reconstructed.ancestry_distance is None
    world = MacroObservationKnowledgeLink.from_envelope(
        _observation_envelope(_macro_observation(scope=_macro_scope(kind="world", entity_id="market"))),
        mapping,
        revision,
    )
    assert world.status == "linked"
    assert world.relation is not None
    assert world.relation.target == WorldEntityRef(kind="world", entity_id="market")


def test_macro_observation_knowledge_link_does_not_resolve_market_anchors() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    envelope = _observation_envelope(_macro_observation(scope=_macro_scope(kind="country", entity_id="iso-3166:TW")))
    link = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, revision)
    assert link.status == "linked"
    assert link.relation is not None
    assert link.relation.target.kind == "country"
    assert link.relation.target.entity_id == "iso-3166:TW"
    assert mapping.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2330")).status == "resolved"


def test_observes_provenance_reconstructs_ancestry_distance_from_unique_heads() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    envelope = _observation_envelope(_macro_observation(scope=_macro_scope(kind="country", entity_id="iso-3166:TW")))
    link = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, revision)
    assert link.relation is not None
    root = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")
    venue = WorldEntityRef(kind="venue", entity_id="mic:XTAI")
    country = WorldEntityRef(kind="country", entity_id="iso-3166:TW")
    region = WorldEntityRef(kind="region", entity_id="iso-un-m49:030")
    world = WorldEntityRef(kind="world", entity_id="market")
    structural = (
        _structural(source=root, target=venue),
        _structural(kind="LOCATED_IN", source=venue, target=country),
        _structural(kind="LOCATED_IN", source=country, target=region),
        _structural(kind="PART_OF_WORLD", source=region, target=world),
    )
    assert ancestry_distance_from_instrument_root(root=root, origin=venue, relations=structural) == 0
    assert ancestry_distance_from_instrument_root(root=root, origin=country, relations=structural) == 1
    assert ancestry_distance_from_instrument_root(root=root, origin=region, relations=structural) == 2
    assert ancestry_distance_from_instrument_root(root=root, origin=world, relations=structural) == 3
    reconstructed = reconstruct_macro_observes_provenance(
        link.relation,
        root=root,
        structural_relations=structural,
    )
    assert reconstructed.origin_scope.kind == "country"
    assert reconstructed.origin_scope.entity_id == "iso-3166:TW"
    assert reconstructed.ancestry_distance == 1
    assert reconstructed.producer_version == envelope.observation.producer_version
    unknown = WorldEntityRef(kind="country", entity_id="iso-3166:US")
    assert ancestry_distance_from_instrument_root(root=root, origin=unknown, relations=structural) is None


def test_unregistered_or_invalid_scope_is_terminal_skip_never_an_invented_relation() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    unknown = MacroObservationKnowledgeLink.from_envelope(
        _observation_envelope(_macro_observation(scope=_macro_scope(kind="country", entity_id="iso-3166:FR"))),
        mapping,
        revision,
    )
    assert unknown.status == "skipped"
    assert unknown.skip_reason == "scope_not_registered"
    assert unknown.relation is None
    with pytest.raises(ValueError, match="mic:"):
        _macro_scope(kind="venue", entity_id="XTAI")
    invalid = MacroObservationKnowledgeLink.from_envelope(object(), mapping, revision)
    assert invalid.status == "skipped"
    assert invalid.skip_reason == "invalid_envelope"
    assert invalid.relation is None
    with pytest.raises(ValueError, match="producer"):
        _macro_observation(producer_version="not_admitted")
    admitted = _macro_observation()
    bypassed = object.__new__(type(admitted))
    for name in admitted.__dataclass_fields__:
        object.__setattr__(bypassed, name, getattr(admitted, name))
    object.__setattr__(bypassed, "producer_version", "not_admitted")
    envelope = _observation_envelope(admitted)
    object.__setattr__(envelope, "observation", bypassed)
    legacy = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, revision)
    assert legacy.status == "skipped"
    assert legacy.skip_reason == "producer_not_admitted"
    assert legacy.relation is None


def test_knowledge_link_rejects_mapping_hash_mismatch_without_sealing_a_relation() -> None:
    mapping = _scope_mapping()
    other = WorldScopeMapping(
        mapping_id="world_scope_mapping.drifted",
        entries=mapping.entries,
    )
    revision = _revision(scope_mapping_id=other.mapping_id, scope_mapping_hash=other.content_sha256)
    with pytest.raises(ValueError, match="hash|mapping"):
        MacroObservationKnowledgeLink.from_envelope(_observation_envelope(), mapping, revision)


def test_registry_activate_is_idempotent_and_refuses_a_second_active_generation() -> None:
    reservation = _reservation()
    spec = _run_spec()
    empty = MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
    activated = empty.activate(reservation=reservation, spec=spec, expected_version=0)
    assert activated.version == 1
    assert activated.active_run is not None
    assert activated.active_run.status == "active"
    assert activated.active_run.epoch == 1
    assert activated.active_run.activation_cursor == reservation.cursor
    assert activated.active_run.cursor == reservation.cursor
    assert activated.fence == MacroGraphBridgeFence(
        bridge_key=BRIDGE_KEY,
        run_id=activated.active_run.run_id,
        epoch=1,
    )
    replayed = activated.activate(reservation=reservation, spec=spec, expected_version=1)
    assert replayed.events == activated.events
    other = MacroObservationCursorReservation(
        bridge_key=BRIDGE_KEY,
        request_id="macro_graph_bridge_request:v1:" + "d" * 64,
        cursor=_origin_cursor(),
    )
    with pytest.raises(ValueError, match="active"):
        activated.activate(reservation=other, spec=spec, expected_version=1)
    event = activated.events[0]
    assert isinstance(event, MacroGraphBridgeActivated)
    assert parse_macro_graph_bridge_event(event.to_dict()) == event


def test_registry_skip_and_link_are_terminal_and_cursor_advances_only_over_contiguous_prefix() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    spec = _run_spec(mapping, revision)
    registry = MacroGraphBridgeRegistry.empty(BRIDGE_KEY).activate(
        reservation=_reservation(),
        spec=spec,
        expected_version=0,
    )
    envelope = _observation_envelope(_macro_observation(scope=_macro_scope(kind="country", entity_id="iso-3166:FR")))
    unknown_cursor = _cursor(
        ordinal=1,
        receipt_id=envelope.persisted.receipt.receipt_id,
        observation_id=envelope.observation.observation_id,
    )
    skipped = registry.skip_observation(
        observation_id=envelope.observation.observation_id,
        reason="scope_not_registered",
        cursor=unknown_cursor,
        expected_version=1,
    )
    assert isinstance(skipped.events[-1], MacroGraphObservationSkipped)
    assert skipped.events[-1].reason == "scope_not_registered"
    linked_env = _observation_envelope(_macro_observation(scope=_macro_scope()))
    link = MacroObservationKnowledgeLink.from_envelope(linked_env, mapping, revision)
    assert link.relation is not None
    linked_cursor = _cursor(
        ordinal=2,
        receipt_id=linked_env.persisted.receipt.receipt_id,
        observation_id=linked_env.observation.observation_id,
    )
    linked = skipped.link_observation(
        observation_id=linked_env.observation.observation_id,
        relation=link.relation,
        relation_event_id="world_relation_event:v1:" + "e" * 64,
        cursor=linked_cursor,
        expected_version=2,
    )
    assert isinstance(linked.events[-1], MacroGraphObservationLinked)
    advanced = linked.advance_cursor(linked_cursor, expected_version=3)
    assert isinstance(advanced.events[-1], MacroGraphBridgeCursorAdvanced)
    assert advanced.active_run is not None
    assert advanced.active_run.cursor == linked_cursor
    replay_skip = skipped.skip_observation(
        observation_id=envelope.observation.observation_id,
        reason="scope_not_registered",
        cursor=unknown_cursor,
        expected_version=2,
    )
    assert replay_skip.events == skipped.events
    with pytest.raises(ValueError, match="conflict|content"):
        skipped.skip_observation(
            observation_id=envelope.observation.observation_id,
            reason="invalid_envelope",
            cursor=unknown_cursor,
            expected_version=2,
        )


def test_config_drift_blocks_without_skipping_or_advancing_and_resume_requires_same_hashes() -> None:
    spec = _run_spec()
    registry = MacroGraphBridgeRegistry.empty(BRIDGE_KEY).activate(
        reservation=_reservation(),
        spec=spec,
        expected_version=0,
    )
    blocked = registry.block(reason="config_drift", expected_version=1)
    assert isinstance(blocked.events[-1], MacroGraphBridgeBlocked)
    assert blocked.active_run is not None
    assert blocked.active_run.status == "blocked"
    assert blocked.active_run.cursor == registry.active_run.cursor
    envelope = _observation_envelope()
    cursor = _cursor(
        ordinal=1,
        receipt_id=envelope.persisted.receipt.receipt_id,
        observation_id=envelope.observation.observation_id,
    )
    with pytest.raises(ValueError, match="blocked"):
        blocked.skip_observation(
            observation_id=envelope.observation.observation_id,
            reason="scope_not_registered",
            cursor=cursor,
            expected_version=2,
        )
    with pytest.raises(ValueError, match="blocked"):
        blocked.advance_cursor(cursor, expected_version=2)
    resumed = blocked.resume(spec=spec, expected_version=2)
    assert isinstance(resumed.events[-1], MacroGraphBridgeResumed)
    assert resumed.active_run is not None
    assert resumed.active_run.status == "active"
    other = MacroGraphBridgeRunSpec(
        scope_mapping_id=spec.scope_mapping_id,
        scope_mapping_hash="f" * 64,
        ontology_revision_id=spec.ontology_revision_id,
        ontology_revision_hash=spec.ontology_revision_hash,
        collection_plan_id=spec.collection_plan_id,
        collection_plan_hash=spec.collection_plan_hash,
        producer_version=spec.producer_version,
    )
    with pytest.raises(ValueError, match="hash|spec"):
        blocked.resume(spec=other, expected_version=2)


def test_handoff_is_a_single_event_with_incremented_epoch_and_exactly_one_active_run() -> None:
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    spec = _run_spec(mapping, revision)
    blocked = (
        MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
        .activate(reservation=_reservation(), spec=spec, expected_version=0)
        .block(reason="config_drift", expected_version=1)
    )
    next_mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=mapping.entries,
    )
    next_revision = _revision(
        revision_id="market_ontology.v1",
        scope_mapping_id=next_mapping.mapping_id,
        scope_mapping_hash=next_mapping.content_sha256,
    )
    next_spec = _run_spec(next_mapping, next_revision)
    handed = blocked.handoff(
        active_run_id=blocked.active_run.run_id,
        next_run_spec=next_spec,
        expected_version=2,
    )
    assert handed.version == 3
    assert isinstance(handed.events[-1], MacroGraphBridgeRunHandedOff)
    event = handed.events[-1]
    assert event.epoch == 2
    assert event.predecessor_run_id == blocked.active_run.run_id
    assert event.successor_run_id == handed.active_run.run_id
    assert handed.active_run is not None
    assert handed.active_run.epoch == 2
    assert handed.active_run.status == "active"
    assert handed.active_run.cursor == blocked.active_run.cursor
    assert handed.active_run.spec == next_spec
    assert len([run for run in handed.runs if run.status == "active"]) == 1
    assert handed.fence.epoch == 2
    replay = parse_macro_graph_bridge_event(event.to_dict())
    assert replay == event
    with pytest.raises(ValueError, match="active|blocked"):
        handed.handoff(
            active_run_id=handed.active_run.run_id,
            next_run_spec=spec,
            expected_version=3,
        )


def test_cursor_is_ordinal_not_datetime_and_first_seen_cannot_move_it() -> None:
    cursor = _origin_cursor()
    assert cursor.ordinal == 0
    assert cursor.receipt_id is None
    assert cursor.observation_id is None
    payload = cursor.to_dict()
    assert "ready_at" not in payload
    assert "first_seen_at" not in payload
    assert "effective_ready_at" not in payload
    later = _cursor(
        ordinal=3,
        receipt_id="world-availability-receipt:v1:" + "a" * 64,
        observation_id="macro_world_observation:v1:" + "b" * 64,
    )
    assert later.ordinal > cursor.ordinal
    assert MacroObservationCursor.from_mapping(later.to_dict()) == later
    reservation = _reservation(cursor=later)
    assert reservation.cursor.ordinal == 3
    assert "first_seen_at" not in reservation.to_dict()
    replayed = MacroObservationCursorReservation.from_mapping(reservation.to_dict())
    assert replayed == reservation
    with pytest.raises(FrozenInstanceError):
        later.ordinal = 9  # type: ignore[misc]
