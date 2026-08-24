from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
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
from trader.domain.world_context import EntityRef
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    StructuralWorldRelationRetired,
    WorldEntityAsserted,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityIdentityLinkSuperseded,
    WorldEntityIdentityMap,
    WorldEntityIdentityUnlinked,
    WorldEntityRef,
    WorldEntityRetired,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldStructuralRelationRef,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 64
SCOPE_HASH = "b" * 64

_GRAPH_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "graph_ports.py"
_ONTOLOGY_SERVICE = REPO_ROOT / "trader" / "application" / "world_model" / "ontology_service.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _entity(*, kind: str = "instrument", entity_id: str = "mic:XTAI:symbol:2330") -> WorldEntityRef:
    return WorldEntityRef(kind=kind, entity_id=entity_id)


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _country() -> WorldEntityRef:
    return WorldEntityRef(kind="country", entity_id="iso-3166:TW")


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


def _observation_ref() -> WorldObservationRef:
    return WorldObservationRef(observation_id=f"world_observation:v1:{SHA}")


def _knowledge(**overrides: object) -> KnowledgeWorldRelation:
    values: dict[str, object] = {
        "kind": "OBSERVES",
        "source": _observation_ref(),
        "target": _country(),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("macro_world_observation:v1:" + SHA, "producer:world_macro_source.v1"),
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


def _revision(**overrides: object) -> WorldOntologyRevision:
    entity = _entity()
    structural = _structural()
    link = _link()
    values: dict[str, object] = {
        "revision_id": "market_ontology.v1",
        "entities": (entity, _venue()),
        "structural_relation_refs": (WorldStructuralRelationRef.from_relation(structural),),
        "identity_link_refs": (link.as_ref(),),
        "scope_mapping_id": "world_scope_mapping.v1",
        "scope_mapping_hash": SCOPE_HASH,
    }
    values.update(overrides)
    return WorldOntologyRevision(**values)  # type: ignore[arg-type]


def _attested_receipt(
    *,
    kind: str,
    subject_id: str,
    content_sha256: str,
    ready: datetime,
) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(kind=kind, subject_id=subject_id, content_sha256=content_sha256)
    locator = WorldStorageLocator(kind="jsonl", store_id="world-graph-jsonl.v1", path="events/2026-08-23.jsonl")
    identity = _receipt_identity_payload(
        schema_version="availability_receipt.v2",
        subject=subject,
        scope="world_graph",
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


class _InMemoryWorldGraphLedger:
    """Test double: stores every append, lists only receipt-eligible events."""

    def __init__(self, *, now: datetime = READY) -> None:
        self.now = now
        self.entity_log: list[object] = []
        self.identity_log: list[object] = []
        self.structural_log: list[object] = []
        self.knowledge_log: list[object] = []
        self.revision_log: list[object] = []
        self.append_calls: list[tuple[str, object]] = []

    def seed_unproven_structural(self, event: StructuralWorldRelationAsserted) -> None:
        self.structural_log.append(event)

    def _evidence(self, event: object, *, kind: str) -> AvailabilityEvidence:
        payload = event.to_dict()  # type: ignore[union-attr]
        receipt = _attested_receipt(
            kind=kind,
            subject_id=event.event_id,  # type: ignore[union-attr]
            content_sha256=canonical_sha256(payload),
            ready=self.now,
        )
        return AvailabilityEvidence(receipt=receipt, first_seen_at=self.now)

    def _eligible(self, evidence: AvailabilityEvidence | None, cutoff_at: datetime) -> bool:
        return PointInTimeEligibilityPolicy().is_eligible(evidence=evidence, cutoff_at=cutoff_at)

    def append_entity_event(self, event: WorldEntityAsserted | WorldEntityRetired) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldEntityEventEnvelope, WorldEntityEventId

        evidence = self._evidence(event, kind="world_entity_event")
        self.entity_log.append(WorldEntityEventEnvelope(event=event, evidence=evidence))
        self.append_calls.append(("entity", event))
        return PersistedWorldRef(identity=WorldEntityEventId(event.event_id), receipt=evidence.receipt)

    def append_identity_event(
        self,
        event: WorldEntityIdentityLinked | WorldEntityIdentityUnlinked | WorldEntityIdentityLinkSuperseded,
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import (
            WorldEntityIdentityEventEnvelope,
            WorldEntityIdentityEventId,
        )

        evidence = self._evidence(event, kind="world_entity_identity_event")
        self.identity_log.append(WorldEntityIdentityEventEnvelope(event=event, evidence=evidence))
        self.append_calls.append(("identity", event))
        return PersistedWorldRef(identity=WorldEntityIdentityEventId(event.event_id), receipt=evidence.receipt)

    def append_structural_relation_event(
        self, event: StructuralWorldRelationAsserted | StructuralWorldRelationRetired
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldRelationEventEnvelope, WorldRelationEventId

        evidence = self._evidence(event, kind="world_relation_event")
        self.structural_log.append(WorldRelationEventEnvelope(event=event, evidence=evidence))
        self.append_calls.append(("structural", event))
        return PersistedWorldRef(identity=WorldRelationEventId(event.event_id), receipt=evidence.receipt)

    def append_knowledge_relation_event(
        self, event: KnowledgeWorldRelationAsserted | KnowledgeWorldRelationRetired
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldRelationEventEnvelope, WorldRelationEventId

        evidence = self._evidence(event, kind="world_relation_event")
        self.knowledge_log.append(WorldRelationEventEnvelope(event=event, evidence=evidence))
        self.append_calls.append(("knowledge", event))
        return PersistedWorldRef(identity=WorldRelationEventId(event.event_id), receipt=evidence.receipt)

    def append_revision_event(
        self, event: WorldOntologyRevisionPublished | WorldOntologyRevisionSuperseded
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import (
            WorldOntologyRevisionEventEnvelope,
            WorldOntologyRevisionEventId,
        )

        evidence = self._evidence(event, kind="world_ontology_revision_event")
        self.revision_log.append(WorldOntologyRevisionEventEnvelope(event=event, evidence=evidence))
        self.append_calls.append(("revision", event))
        return PersistedWorldRef(identity=WorldOntologyRevisionEventId(event.event_id), receipt=evidence.receipt)

    def list_entity_events_available_through(self, cutoff_at: datetime):
        return tuple(env for env in self.entity_log if self._eligible(env.evidence, cutoff_at))

    def list_identity_events_available_through(self, cutoff_at: datetime):
        return tuple(env for env in self.identity_log if self._eligible(env.evidence, cutoff_at))

    def list_structural_relation_events_available_through(self, cutoff_at: datetime):
        return tuple(
            env
            for env in self.structural_log
            if getattr(env, "evidence", None) is not None and self._eligible(env.evidence, cutoff_at)
        )

    def list_knowledge_relation_events_available_through(self, cutoff_at: datetime):
        return tuple(env for env in self.knowledge_log if self._eligible(env.evidence, cutoff_at))

    def list_revision_events_available_through(self, cutoff_at: datetime):
        return tuple(env for env in self.revision_log if self._eligible(env.evidence, cutoff_at))


def test_graph_application_modules_do_not_import_store_networkx_runtime_or_reporting() -> None:
    assert _GRAPH_PORTS.exists()
    assert _ONTOLOGY_SERVICE.exists()
    assert _import_violations(_GRAPH_PORTS) == []
    assert _import_violations(_ONTOLOGY_SERVICE) == []


def test_world_graph_ledger_port_exposes_typed_append_and_available_through_reads() -> None:
    from trader.application.world_model.graph_ports import WorldGraphLedger, WorldGraphSnapshotLedger

    append_methods = (
        "append_entity_event",
        "append_identity_event",
        "append_structural_relation_event",
        "append_knowledge_relation_event",
        "append_revision_event",
    )
    list_methods = (
        "list_entity_events_available_through",
        "list_identity_events_available_through",
        "list_structural_relation_events_available_through",
        "list_knowledge_relation_events_available_through",
        "list_revision_events_available_through",
    )
    for name in append_methods + list_methods:
        assert hasattr(WorldGraphLedger, name)
        parameters = inspect.signature(getattr(WorldGraphLedger, name)).parameters
        assert "self" in parameters
    assert inspect.signature(WorldGraphLedger.append_knowledge_relation_event).parameters["event"].annotation
    assert hasattr(WorldGraphSnapshotLedger, "append")
    assert hasattr(WorldGraphSnapshotLedger, "get")
    list_structural = inspect.signature(WorldGraphLedger.list_structural_relation_events_available_through)
    assert list(list_structural.parameters) == ["self", "cutoff_at"]


def test_typed_commands_append_entity_identity_relation_and_revision_events() -> None:
    from trader.application.world_model.ontology_service import (
        AssertKnowledgeWorldRelation,
        AssertStructuralWorldRelation,
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        PublishWorldOntologyRevision,
        RetireStructuralWorldRelation,
        RetireWorldEntity,
        SupersedeWorldEntity,
        SupersedeWorldOntologyRevision,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger()
    service = WorldOntologyService(ledger)
    entity = _entity()
    asserted = service.assert_entity(
        AssertWorldEntity(entity=entity, source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    assert isinstance(asserted, PersistedWorldRef)
    assert isinstance(ledger.append_calls[0][1], WorldEntityAsserted)
    service.retire_entity(
        RetireWorldEntity(entity=entity, retired_at=T1, source_refs=("provider:instrument-master:2330",))
    )
    service.supersede_entity(
        SupersedeWorldEntity(
            entity=entity,
            successor=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
            superseded_at=T1,
            source_refs=("provider:instrument-master:2330:corrected",),
        )
    )
    service.link_identity(LinkWorldEntityIdentity(link=_link()))
    relation = _structural()
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
    service.retire_structural_relation(
        RetireStructuralWorldRelation(
            relation_id=relation.relation_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330",),
        )
    )
    service.assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=_knowledge()))
    revision = _revision()
    service.publish_revision(PublishWorldOntologyRevision(revision=revision))
    service.supersede_revision(
        SupersedeWorldOntologyRevision(revision_id=revision.revision_id, successor_revision_id="market_ontology.v2")
    )
    kinds = [kind for kind, _event in ledger.append_calls]
    assert kinds.count("entity") == 3
    assert "identity" in kinds
    assert kinds.count("structural") == 2
    assert "knowledge" in kinds
    assert kinds.count("revision") == 2
    assert isinstance(ledger.append_calls[-2][1], WorldOntologyRevisionPublished)
    assert isinstance(ledger.append_calls[-1][1], WorldOntologyRevisionSuperseded)
    with pytest.raises(FrozenInstanceError):
        AssertWorldEntity(entity=entity, source_refs=("x",), effective_from=T0).entity = _venue()  # type: ignore[misc]


def test_ledger_lists_raw_envelopes_without_applying_supersession() -> None:
    from trader.application.world_model.ontology_service import (
        AssertStructuralWorldRelation,
        RetireStructuralWorldRelation,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger(now=READY)
    service = WorldOntologyService(ledger)
    relation = _structural()
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
    service.retire_structural_relation(
        RetireStructuralWorldRelation(
            relation_id=relation.relation_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330",),
        )
    )
    envelopes = ledger.list_structural_relation_events_available_through(CUTOFF)
    assert len(envelopes) == 2
    assert isinstance(envelopes[0].event, StructuralWorldRelationAsserted)
    assert isinstance(envelopes[1].event, StructuralWorldRelationRetired)
    assert envelopes[0].event.relation.effective_until is None


def test_structural_and_knowledge_resolvers_are_distinct_pit_authors() -> None:
    from trader.application.world_model.ontology_service import (
        AssertKnowledgeWorldRelation,
        AssertStructuralWorldRelation,
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        PublishWorldOntologyRevision,
        WorldKnowledgeOverlayView,
        WorldKnowledgeResolver,
        WorldOntologyResolver,
        WorldOntologyRevisionView,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger()
    service = WorldOntologyService(ledger)
    service.assert_entity(
        AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    service.assert_entity(AssertWorldEntity(entity=_venue(), source_refs=("mic:XTAI",), effective_from=T0))
    service.link_identity(LinkWorldEntityIdentity(link=_link()))
    relation = _structural()
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
    revision = _revision()
    service.publish_revision(PublishWorldOntologyRevision(revision=revision))
    knowledge = _knowledge()
    service.assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=knowledge))

    structural = WorldOntologyResolver(ledger)
    overlay = WorldKnowledgeResolver(ledger)
    view = structural.at_cutoff(CUTOFF)
    assert isinstance(view, WorldOntologyRevisionView)
    assert view.cutoff_at == CUTOFF
    assert relation in view.structural_relations
    assert _entity() in view.entities
    assert isinstance(view.identity_map, WorldEntityIdentityMap)
    assert view.identity_links[0].v3_ref == _entity()
    assert view.published_revision == revision
    assert "knowledge_relations" not in view.__dataclass_fields__
    with pytest.raises(FrozenInstanceError):
        view.structural_relations = ()  # type: ignore[misc]

    knowledge_view = overlay.at_cutoff(CUTOFF, revision)
    assert isinstance(knowledge_view, WorldKnowledgeOverlayView)
    assert knowledge in knowledge_view.relations
    assert knowledge_view.structural_revision_id == revision.revision_id
    assert knowledge_view.structural_revision_hash == revision.content_sha256
    assert knowledge_view.structural_revision_hash == view.published_revision.content_sha256

    spy_calls: list[str] = []
    original_structural = ledger.list_structural_relation_events_available_through
    original_knowledge = ledger.list_knowledge_relation_events_available_through

    def _spy_structural(cutoff_at: datetime):
        spy_calls.append("structural")
        return original_structural(cutoff_at)

    def _spy_knowledge(cutoff_at: datetime):
        spy_calls.append("knowledge")
        return original_knowledge(cutoff_at)

    ledger.list_structural_relation_events_available_through = _spy_structural  # type: ignore[method-assign]
    ledger.list_knowledge_relation_events_available_through = _spy_knowledge  # type: ignore[method-assign]
    spy_calls.clear()
    WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert "structural" in spy_calls
    assert "knowledge" not in spy_calls
    spy_calls.clear()
    WorldKnowledgeResolver(ledger).at_cutoff(CUTOFF, revision)
    assert "knowledge" in spy_calls
    assert "structural" not in spy_calls


def test_future_relation_stays_in_ledger_but_is_invisible_in_structural_view() -> None:
    from trader.application.world_model.ontology_service import (
        AssertStructuralWorldRelation,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger(now=READY)
    service = WorldOntologyService(ledger)
    future = _structural(effective_from=LATER, source_refs=("provider:instrument-master:future",))
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=future))
    envelopes = ledger.list_structural_relation_events_available_through(CUTOFF)
    assert len(envelopes) == 1
    assert envelopes[0].event.relation == future
    view = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert view.structural_relations == ()


def test_late_and_unproven_relation_events_are_invisible() -> None:
    from trader.application.world_model.ontology_service import (
        AssertStructuralWorldRelation,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    late_ledger = _InMemoryWorldGraphLedger(now=LATER)
    late_service = WorldOntologyService(late_ledger)
    present = _structural()
    late_service.assert_structural_relation(AssertStructuralWorldRelation(relation=present))
    assert late_ledger.list_structural_relation_events_available_through(CUTOFF) == ()
    assert WorldOntologyResolver(late_ledger).at_cutoff(CUTOFF).structural_relations == ()

    unproven_ledger = _InMemoryWorldGraphLedger(now=READY)
    unproven_ledger.seed_unproven_structural(StructuralWorldRelationAsserted(relation=present))
    assert unproven_ledger.list_structural_relation_events_available_through(CUTOFF) == ()
    assert WorldOntologyResolver(unproven_ledger).at_cutoff(CUTOFF).structural_relations == ()


def test_correction_is_not_retroactive_across_cutoffs() -> None:
    from trader.application.world_model.ontology_service import (
        AssertStructuralWorldRelation,
        RetireStructuralWorldRelation,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger(now=READY)
    service = WorldOntologyService(ledger)
    original = _structural()
    original_payload = original.to_dict()
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=original))
    before = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert before.structural_relations == (original,)

    ledger.now = LATER
    correction = original.corrected(
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAF"),
        effective_from=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    service.retire_structural_relation(
        RetireStructuralWorldRelation(
            relation_id=original.relation_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330:corrected",),
        )
    )
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=correction))

    after_old_cutoff = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert after_old_cutoff.structural_relations == (original,)
    assert original.to_dict() == original_payload
    later_view = WorldOntologyResolver(ledger).at_cutoff(LATER)
    assert original not in later_view.structural_relations
    assert correction in later_view.structural_relations
    assert correction.supersedes == original.relation_id


def test_knowledge_overlay_does_not_enter_structural_heads_or_foreign_revisions() -> None:
    from trader.application.world_model.ontology_service import (
        AssertKnowledgeWorldRelation,
        AssertStructuralWorldRelation,
        PublishWorldOntologyRevision,
        WorldKnowledgeResolver,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger()
    service = WorldOntologyService(ledger)
    structural = _structural()
    service.assert_structural_relation(AssertStructuralWorldRelation(relation=structural))
    revision = _revision()
    service.publish_revision(PublishWorldOntologyRevision(revision=revision))
    structural_before = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    service.assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=_knowledge()))
    foreign = _knowledge(
        ontology_revision="market_ontology.v2",
        source_refs=("macro_world_observation:v1:" + "c" * 64, "producer:world_macro_source.v1"),
        source=WorldObservationRef(observation_id=f"world_observation:v1:{'c' * 64}"),
    )
    service.assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=foreign))
    structural_after = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    overlay = WorldKnowledgeResolver(ledger).at_cutoff(CUTOFF, revision)
    assert structural_before.structural_heads_hash == structural_after.structural_heads_hash
    assert structural_before.entity_heads_hash == structural_after.entity_heads_hash
    assert structural_before.identity_map_hash == structural_after.identity_map_hash
    assert overlay.structural_revision_hash == revision.content_sha256
    assert {item.relation_id for item in overlay.relations} == {_knowledge().relation_id}
    assert all(item.ontology_revision == revision.revision_id for item in overlay.relations)


def test_causes_cannot_be_asserted_through_ontology_commands() -> None:
    from trader.application.world_model.ontology_service import (
        AssertKnowledgeWorldRelation,
        AssertStructuralWorldRelation,
        WorldOntologyService,
    )

    service = WorldOntologyService(_InMemoryWorldGraphLedger())
    with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
        service.assert_structural_relation(
            AssertStructuralWorldRelation(
                relation=_structural(kind="CAUSES"),
            )
        )
    with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
        service.assert_knowledge_relation(
            AssertKnowledgeWorldRelation(
                relation=_knowledge(kind="CAUSES"),
            )
        )


def test_identity_map_at_cutoff_is_authored_by_structural_resolver_and_ignores_append_order() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    first = _InMemoryWorldGraphLedger()
    second = _InMemoryWorldGraphLedger()
    left = WorldOntologyService(first)
    right = WorldOntologyService(second)
    venue_link = _link(v2_ref=EntityRef(kind="venue", entity_id="XTAI"), v3_ref=_venue(), source_refs=("mic:XTAI",))
    left.assert_entity(
        AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    left.assert_entity(AssertWorldEntity(entity=_venue(), source_refs=("mic:XTAI",), effective_from=T0))
    left.link_identity(LinkWorldEntityIdentity(link=_link()))
    left.link_identity(LinkWorldEntityIdentity(link=venue_link))
    right.assert_entity(AssertWorldEntity(entity=_venue(), source_refs=("mic:XTAI",), effective_from=T0))
    right.assert_entity(
        AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    right.link_identity(LinkWorldEntityIdentity(link=venue_link))
    right.link_identity(LinkWorldEntityIdentity(link=_link()))
    left_view = WorldOntologyResolver(first).at_cutoff(CUTOFF)
    right_view = WorldOntologyResolver(second).at_cutoff(CUTOFF)
    assert left_view.identity_map_hash == right_view.identity_map_hash
    assert left_view.entity_heads_hash == right_view.entity_heads_hash
    assert {link.link_id for link in left_view.identity_links} == {link.link_id for link in right_view.identity_links}


def test_duplicate_entity_assertion_with_different_content_or_provenance_conflicts() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger()
    service = WorldOntologyService(ledger)
    entity = _entity()
    service.assert_entity(
        AssertWorldEntity(entity=entity, source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    service.assert_entity(
        AssertWorldEntity(
            entity=entity,
            source_refs=("provider:instrument-master:2330:corrected",),
            effective_from=T0,
        )
    )
    with pytest.raises(ValueError, match="conflict"):
        WorldOntologyResolver(ledger).at_cutoff(CUTOFF)


def test_identity_unlink_then_relink_at_same_cutoff_is_deterministic_across_append_order() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        UnlinkWorldEntityIdentity,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    original = _link()
    replacement = _link(
        v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        effective_from=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    original_payload = original.to_dict()

    def _seed(order: tuple[str, ...]) -> _InMemoryWorldGraphLedger:
        ledger = _InMemoryWorldGraphLedger()
        service = WorldOntologyService(ledger)
        service.assert_entity(
            AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
        )
        actions = {
            "link": lambda: service.link_identity(LinkWorldEntityIdentity(link=original)),
            "unlink": lambda: service.unlink_identity(
                UnlinkWorldEntityIdentity(
                    link_id=original.link_id,
                    retired_at=T1,
                    source_refs=("provider:instrument-master:2330:corrected",),
                )
            ),
            "relink": lambda: service.link_identity(LinkWorldEntityIdentity(link=replacement)),
        }
        for name in order:
            actions[name]()
        return ledger

    views = []
    for order in (
        ("link", "unlink", "relink"),
        ("link", "relink", "unlink"),
        ("relink", "link", "unlink"),
        ("unlink", "relink", "link"),
    ):
        ledger = _seed(order)
        view = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
        views.append(view)
        assert [link.link_id for link in view.identity_links] == [replacement.link_id]
        assert view.identity_links[0].v3_ref == replacement.v3_ref
        at_t0 = view.identity_map.active_links_at(T0)
        assert [link.link_id for link in at_t0] == [original.link_id]
        assert at_t0[0].v3_ref == original.v3_ref
        assert original.to_dict() == original_payload

    assert {view.identity_map_hash for view in views} == {views[0].identity_map_hash}
    assert {view.identity_links for view in views} == {views[0].identity_links}


def test_identical_entity_assertions_are_idempotent() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    ledger = _InMemoryWorldGraphLedger()
    service = WorldOntologyService(ledger)
    command = AssertWorldEntity(
        entity=_entity(),
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )
    service.assert_entity(command)
    service.assert_entity(command)
    view = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert view.entities == (_entity(),)
    assert len(view.entity_revision_refs) == 1


def test_identity_supersede_stays_non_retroactive_across_append_order() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        SupersedeWorldEntityIdentityLink,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    original = _link()
    successor = _link(
        v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        effective_from=T1,
        supersedes=original.link_id,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    original_payload = original.to_dict()

    def _seed(*, link_first: bool) -> _InMemoryWorldGraphLedger:
        ledger = _InMemoryWorldGraphLedger()
        service = WorldOntologyService(ledger)
        service.assert_entity(
            AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
        )
        commands = (
            lambda: service.link_identity(LinkWorldEntityIdentity(link=original)),
            lambda: service.supersede_identity_link(SupersedeWorldEntityIdentityLink(successor=successor)),
        )
        for command in commands if link_first else reversed(commands):
            command()
        return ledger

    left = WorldOntologyResolver(_seed(link_first=True)).at_cutoff(CUTOFF)
    right = WorldOntologyResolver(_seed(link_first=False)).at_cutoff(CUTOFF)
    assert left.identity_map_hash == right.identity_map_hash
    assert [link.link_id for link in left.identity_links] == [successor.link_id]
    assert left.identity_links[0].supersedes == original.link_id
    at_t0 = left.identity_map.active_links_at(T0)
    assert [link.link_id for link in at_t0] == [original.link_id]
    assert original.to_dict() == original_payload


def test_late_identity_unlink_relink_is_not_visible_at_earlier_cutoff() -> None:
    from trader.application.world_model.ontology_service import (
        AssertWorldEntity,
        LinkWorldEntityIdentity,
        UnlinkWorldEntityIdentity,
        WorldOntologyResolver,
        WorldOntologyService,
    )

    original = _link()
    replacement = _link(
        v3_ref=WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330.TW"),
        effective_from=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    ledger = _InMemoryWorldGraphLedger(now=READY)
    service = WorldOntologyService(ledger)
    service.assert_entity(
        AssertWorldEntity(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    service.link_identity(LinkWorldEntityIdentity(link=original))
    before = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert [link.link_id for link in before.identity_links] == [original.link_id]

    ledger.now = LATER
    service.unlink_identity(
        UnlinkWorldEntityIdentity(
            link_id=original.link_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330:corrected",),
        )
    )
    service.link_identity(LinkWorldEntityIdentity(link=replacement))
    after_old_cutoff = WorldOntologyResolver(ledger).at_cutoff(CUTOFF)
    assert [link.link_id for link in after_old_cutoff.identity_links] == [original.link_id]
    later_view = WorldOntologyResolver(ledger).at_cutoff(LATER)
    assert [link.link_id for link in later_view.identity_links] == [replacement.link_id]
