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
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
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
    WorldObservationRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldStructuralRelationRef,
    evaluate_world_graph_point_in_time,
    fold_knowledge_relation_events_at_cutoff,
    fold_structural_relation_events_at_cutoff,
    parse_world_entity_event,
    parse_world_entity_identity_event,
    parse_world_graph_node_ref,
    parse_world_ontology_revision_event,
    parse_world_relation_event,
    reconcile_world_graph_snapshot,
    reconcile_world_ontology_revision,
)
from trader.domain.world_macro import MacroSourceFactVersionId


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
        "v2_ref": EntityRef(kind="instrument", entity_id="2330"),
        "v3_ref": _entity(),
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
        schema_version="availability_receipt.v2",
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
        WorldEntityRef(kind="sensor", entity_id="macro_source_only.v1")
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
    sensor = SensorRef(sensor_id="macro_source_only.v1")
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


def test_identity_map_links_v2_to_v3_and_rejects_ambiguity() -> None:
    empty = WorldEntityIdentityMap.empty()
    linked = empty.link(_link())
    active = linked.active_links_at(CUTOFF)
    assert len(active) == 1
    assert active[0].v2_ref == EntityRef(kind="instrument", entity_id="2330")
    assert active[0].v3_ref == _entity()
    assert active[0].source_refs == ("provider:instrument-master:2330",)
    replayed = WorldEntityIdentityMap.from_events([event.to_dict() for event in linked.events])
    assert replayed.active_links_at(CUTOFF) == active
    other_venue = _link(v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XNYS:symbol:2330"))
    with pytest.raises(ValueError, match="ambiguous|already"):
        linked.link(other_venue)
    with pytest.raises(ValueError, match="kind"):
        _link(v2_ref=EntityRef(kind="venue", entity_id="XTAI"), v3_ref=_entity())
    with pytest.raises(ValueError, match="sensor"):
        _link(v2_ref=EntityRef(kind="sensor", entity_id="news_macro"), v3_ref=_entity(kind="event", entity_id="provider:evt-1"))


def test_identity_correction_supersedes_without_rewriting_the_old_link() -> None:
    original = _link()
    mapped = WorldEntityIdentityMap.empty().link(original)
    payload_before = original.to_dict()
    successor = _link(
        v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
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
    assert at_t0[0].v3_ref == original.v3_ref
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
    successor = _revision(revision_id="market_ontology.v2")
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
    other_identity = _revision(identity_link_refs=(_link(v2_ref=EntityRef(kind="instrument", entity_id="2454")).as_ref(),))
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
    map_left = WorldEntityIdentityMap.empty().link(_link()).link(
        _link(v2_ref=EntityRef(kind="venue", entity_id="XTAI"), v3_ref=_venue(), source_refs=("mic:XTAI",))
    )
    map_right = WorldEntityIdentityMap.empty().link(
        _link(v2_ref=EntityRef(kind="venue", entity_id="XTAI"), v3_ref=_venue(), source_refs=("mic:XTAI",))
    ).link(_link())
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


def test_v2_entity_refs_remain_locally_scoped_and_unrelated_to_v3_ids() -> None:
    v2 = EntityRef(kind="instrument", entity_id="2330")
    v3 = _entity()
    assert v2.entity_id == "2330"
    assert v3.entity_id == "mic:XTAI:symbol:2330"
    assert v2.to_dict() != v3.to_dict()
    assert "sensor" in ENTITY_KINDS
    assert "sensor" not in WORLD_ENTITY_KINDS
    assert "macro_indicator" in WORLD_ENTITY_KINDS
    edge = TopologyEdge(
        kind="TRADED_ON",
        source=v2,
        target=EntityRef(kind="venue", entity_id="XTAI"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
    )
    assert edge.source.entity_id == "2330"
    linked = _link(v2_ref=v2, v3_ref=v3)
    assert linked.v2_ref.entity_id != linked.v3_ref.entity_id


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
        v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
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
