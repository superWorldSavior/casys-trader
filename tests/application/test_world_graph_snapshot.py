from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.ontology_service import (
    AssertKnowledgeWorldRelation,
    AssertStructuralWorldRelation,
    AssertWorldEntity,
    LinkWorldEntityIdentity,
    PublishWorldOntologyRevision,
    SupersedeWorldOntologyRevision,
    WorldOntologyService,
)
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
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_VERSION,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    canonical_sha256,
)
from trader.domain.world_graph import (
    GRAPH_TRAVERSAL_POLICY_VERSION,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldKnowledgeRelationRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
)
from trader.domain.world_scope import (
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
    WorldScopeResolution,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 64
OBS_SHA = "c" * 64
ART_SHA = "d" * 64

_GRAPH_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "graph_ports.py"
_GRAPH_SNAPSHOT = REPO_ROOT / "trader" / "application" / "world_model" / "graph_snapshot.py"
_GRAPH_FEATURES = REPO_ROOT / "trader" / "application" / "world_model" / "graph_features.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "httpx",
    "openai",
    "anthropic",
    "requests",
    "urllib.request",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _instrument(symbol: str = "2330") -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id=f"mic:XTAI:symbol:{symbol}")


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _country() -> WorldEntityRef:
    return WorldEntityRef(kind="country", entity_id="iso-3166:TW")


def _region() -> WorldEntityRef:
    return WorldEntityRef(kind="region", entity_id="iso-un-m49:030")


def _world() -> WorldEntityRef:
    return WorldEntityRef(kind="world", entity_id="market")


def _family() -> WorldEntityRef:
    return WorldEntityRef(kind="family", entity_id="taxonomy:v1:semiconductors")


def _company() -> WorldEntityRef:
    return WorldEntityRef(kind="company", entity_id="lei:549300ABCDEFGHIJKLMN")


def _observation_ref() -> WorldObservationRef:
    return WorldObservationRef(observation_id=f"world_observation:v1:{OBS_SHA}")


def _artifact_ref() -> KnowledgeArtifactRef:
    return KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{ART_SHA}", content_sha256=ART_SHA)


def _structural(**overrides: object) -> StructuralWorldRelation:
    values: dict[str, object] = {
        "kind": "TRADED_ON",
        "source": _instrument(),
        "target": _venue(),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("provider:instrument-master:2330",),
    }
    values.update(overrides)
    return StructuralWorldRelation(**values)  # type: ignore[arg-type]


def _knowledge(**overrides: object) -> KnowledgeWorldRelation:
    values: dict[str, object] = {
        "kind": "OBSERVES",
        "source": _observation_ref(),
        "target": _region(),
        "effective_from": CUTOFF - timedelta(hours=1),
        "ontology_revision": "market_ontology.v1",
        "source_refs": (f"macro_world_observation:v1:{OBS_SHA}",),
    }
    values.update(overrides)
    return KnowledgeWorldRelation(**values)  # type: ignore[arg-type]


def _about() -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=_artifact_ref(),
        target=_instrument(),
        effective_from=CUTOFF - timedelta(hours=2),
        ontology_revision="market_ontology.v1",
        source_refs=(f"artifact:{ART_SHA}",),
    )


def _link(**overrides: object) -> WorldEntityIdentityLink:
    values: dict[str, object] = {
        "v2_ref": EntityRef(kind="instrument", entity_id="2330"),
        "v3_ref": _instrument(),
        "source_refs": ("provider:instrument-master:2330",),
        "effective_from": T0,
    }
    values.update(overrides)
    return WorldEntityIdentityLink(**values)  # type: ignore[arg-type]


def _mapping_entry(*, instrument: str = "2330") -> WorldScopeMappingEntry:
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue="TW", instrument=instrument),
        venue={"kind": "venue", "entity_id": "mic:XTAI"},
        country={"kind": "country", "entity_id": "iso-3166:TW"},
        region={"kind": "region", "entity_id": "iso-un-m49:030"},
        world={"kind": "world", "entity_id": "market"},
        provider_proofs=("provider:world-scope:xtai",),
        taxonomy_version="sessions_mic.v1",
    )


def _mapping(*, entries: tuple[WorldScopeMappingEntry, ...] | None = None) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(_mapping_entry(),) if entries is None else entries,
    )


def _resolution_for(mapping: WorldScopeMapping, *, instrument: str = "2330") -> WorldScopeResolution:
    return mapping.resolve(WorldMarketAnchorRef(market_venue="TW", instrument=instrument))


def _rfc_structural() -> tuple[StructuralWorldRelation, ...]:
    return (
        _structural(),
        _structural(
            kind="LOCATED_IN",
            source=_venue(),
            target=_country(),
            source_refs=("world-scope-mapping:xtai-country",),
        ),
        _structural(
            kind="LOCATED_IN",
            source=_country(),
            target=_region(),
            source_refs=("world-scope-mapping:tw-region",),
        ),
        _structural(
            kind="PART_OF_WORLD",
            source=_region(),
            target=_world(),
            source_refs=("world-scope-mapping:region-world",),
        ),
        _structural(
            kind="ISSUED_BY",
            source=_instrument(),
            target=_company(),
            source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
        ),
        _structural(
            kind="MEMBER_OF_FAMILY",
            source=_instrument(),
            target=_family(),
            source_refs=("taxonomy:v1:semiconductors",),
        ),
    )


def _rfc_entities() -> tuple[WorldEntityRef, ...]:
    return (_instrument(), _venue(), _country(), _region(), _world(), _family(), _company())


def _revision_for(
    mapping: WorldScopeMapping,
    *,
    entities: tuple[WorldEntityRef, ...] | None = None,
    structural: tuple[StructuralWorldRelation, ...] | None = None,
    identity_links: tuple[WorldEntityIdentityLink, ...] = (),
) -> WorldOntologyRevision:
    resolved_entities = entities if entities is not None else _rfc_entities()
    resolved_structural = structural if structural is not None else _rfc_structural()
    return WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=resolved_entities,
        structural_relation_refs=tuple(WorldStructuralRelationRef.from_relation(item) for item in resolved_structural),
        identity_link_refs=tuple(link.as_ref() for link in identity_links),
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
    )


def _episode() -> WorldEpisode:
    return WorldEpisode(
        observation=WorldObservation(
            venue="TW",
            symbol="2330",
            bar_interval="1h",
            as_of_bar_ts=CUTOFF,
            feature_contract_version=MARKET_FEATURE_CONTRACT_VERSION,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=CUTOFF,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                source="unit-graph-6",
                timestamp_semantics="bar_close",
            ),
            available_at=CUTOFF,
            captured_at=CUTOFF,
            freshness={"status": "fresh", "data_age_minutes": 5.0},
            categorical_features={"venue": "TW", "bar_interval": "1h"},
            numeric_features={"return": 0.01},
        )
    )


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
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{rel_path}: import {alias.name}")
    source = path.read_text(encoding="utf-8")
    for marker in ("world_graph_store", "world_temporal_networkx", "openai", "httpx"):
        if marker in source:
            violations.append(f"{rel_path}: {marker} mentioned")
    return violations


class _InMemoryWorldGraphLedger:
    def __init__(self, *, now: datetime = READY) -> None:
        self.now = now
        self.entity_log: list[object] = []
        self.identity_log: list[object] = []
        self.structural_log: list[object] = []
        self.knowledge_log: list[object] = []
        self.revision_log: list[object] = []

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

    def append_entity_event(self, event: object) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldEntityEventEnvelope, WorldEntityEventId

        evidence = self._evidence(event, kind="world_entity_event")
        self.entity_log.append(WorldEntityEventEnvelope(event=event, evidence=evidence))
        return PersistedWorldRef(identity=WorldEntityEventId(event.event_id), receipt=evidence.receipt)  # type: ignore[attr-defined]

    def append_identity_event(self, event: object) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldEntityIdentityEventEnvelope, WorldEntityIdentityEventId

        evidence = self._evidence(event, kind="world_entity_identity_event")
        self.identity_log.append(WorldEntityIdentityEventEnvelope(event=event, evidence=evidence))
        return PersistedWorldRef(identity=WorldEntityIdentityEventId(event.event_id), receipt=evidence.receipt)  # type: ignore[attr-defined]

    def append_structural_relation_event(self, event: object) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldRelationEventEnvelope, WorldRelationEventId

        evidence = self._evidence(event, kind="world_relation_event")
        self.structural_log.append(WorldRelationEventEnvelope(event=event, evidence=evidence))
        return PersistedWorldRef(identity=WorldRelationEventId(event.event_id), receipt=evidence.receipt)  # type: ignore[attr-defined]

    def append_knowledge_relation_event(
        self,
        event: object,
        fence: object | None = None,
        expected_registry_version: int | None = None,
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import WorldRelationEventEnvelope, WorldRelationEventId

        evidence = self._evidence(event, kind="world_relation_event")
        self.knowledge_log.append(WorldRelationEventEnvelope(event=event, evidence=evidence))
        return PersistedWorldRef(identity=WorldRelationEventId(event.event_id), receipt=evidence.receipt)  # type: ignore[attr-defined]

    def append_revision_event(self, event: object) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import (
            WorldOntologyRevisionEventEnvelope,
            WorldOntologyRevisionEventId,
        )

        evidence = self._evidence(event, kind="world_ontology_revision_event")
        self.revision_log.append(WorldOntologyRevisionEventEnvelope(event=event, evidence=evidence))
        return PersistedWorldRef(identity=WorldOntologyRevisionEventId(event.event_id), receipt=evidence.receipt)  # type: ignore[attr-defined]

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
        return tuple(
            env
            for env in self.knowledge_log
            if getattr(env, "evidence", None) is not None and self._eligible(env.evidence, cutoff_at)
        )

    def list_revision_events_available_through(self, cutoff_at: datetime):
        return tuple(env for env in self.revision_log if self._eligible(env.evidence, cutoff_at))

    def seed_unproven_knowledge(self, event: KnowledgeWorldRelationAsserted) -> None:
        self.knowledge_log.append(event)

    def seed_unproven_structural(self, event: StructuralWorldRelationAsserted) -> None:
        self.structural_log.append(event)


class _InMemorySnapshotLedger:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        self.appends = 0

    def append(self, snapshot):
        from trader.application.world_model.graph_ports import WorldGraphSnapshotId

        receipt = _attested_receipt(
            kind="world_graph_snapshot",
            subject_id=snapshot.snapshot_id,
            content_sha256=snapshot.content_sha256,
            ready=READY,
        )
        self.rows[snapshot.snapshot_id] = snapshot
        self.appends += 1
        return PersistedWorldRef(identity=WorldGraphSnapshotId(snapshot.snapshot_id), receipt=receipt)

    def get(self, snapshot_id):
        return self.rows.get(str(snapshot_id))


class TemporalTraversalAdapter:
    """Test adapter: GRAPH-4 projector behind the application traversal port."""

    def enumerate_paths(self, structural, knowledge, root, *, max_depth=None, max_paths=None):
        from trader.application.world_model.graph_ports import WorldGraphPath, WorldGraphPathSet, WorldGraphPathStep
        from trader.infrastructure.graph.world_temporal_networkx import WorldTemporalGraph

        enumerated = WorldTemporalGraph.from_resolved_views(structural, knowledge).enumerate_paths(
            root,
            max_depth=max_depth,
            max_paths=max_paths,
        )
        return WorldGraphPathSet(
            paths=tuple(
                WorldGraphPath(
                    steps=tuple(
                        WorldGraphPathStep(
                            relation_id=step.relation_id,
                            kind=step.kind,
                            family=step.family,
                            direction=step.direction,
                            source_kind=step.source_kind,
                            target_kind=step.target_kind,
                            source_node_id=step.source_node_id,
                            target_node_id=step.target_node_id,
                            freshness_bucket=step.freshness_bucket,
                        )
                        for step in path.steps
                    )
                )
                for path in enumerated.paths
            ),
            status=enumerated.status,
            policy_version=enumerated.policy_version,
        )


def _seed_rfc_graph(
    ledger: _InMemoryWorldGraphLedger,
    mapping: WorldScopeMapping,
    *,
    knowledge: tuple[KnowledgeWorldRelation, ...] | None = None,
    identity: bool = True,
) -> WorldOntologyRevision:
    service = WorldOntologyService(ledger)
    structural = _rfc_structural()
    entities = _rfc_entities()
    link = _link()
    for entity in entities:
        proof = f"provider:entity:{entity.node_id}"
        service.assert_entity(AssertWorldEntity(entity=entity, source_refs=(proof,), effective_from=T0))
    if identity:
        service.link_identity(LinkWorldEntityIdentity(link=link))
    for relation in structural:
        service.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
    revision = _revision_for(mapping, entities=entities, structural=structural, identity_links=(link,) if identity else ())
    service.publish_revision(PublishWorldOntologyRevision(revision=revision))
    for item in knowledge if knowledge is not None else (_knowledge(), _about()):
        service.assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=item))
    return revision


def _service(ledger: _InMemoryWorldGraphLedger, snapshot_ledger: _InMemorySnapshotLedger | None = None):
    from trader.application.world_model.graph_snapshot import WorldGraphSnapshotService

    return WorldGraphSnapshotService(
        ledger=ledger,
        traversal=TemporalTraversalAdapter(),
        snapshot_ledger=snapshot_ledger,
    )


def _request(mapping: WorldScopeMapping, **overrides: object):
    from trader.application.world_model.graph_snapshot import WorldGraphSnapshotRequest

    values: dict[str, object] = {
        "episode": _episode(),
        "root_entity": _instrument(),
        "cutoff_at": CUTOFF,
        "scope_mapping": mapping,
        "scope_resolution": _resolution_for(mapping),
    }
    values.update(overrides)
    return WorldGraphSnapshotRequest(**values)  # type: ignore[arg-type]


def test_graph_snapshot_and_ports_do_not_import_store_networkx_runtime_or_network_io() -> None:
    assert _GRAPH_PORTS.exists()
    assert _GRAPH_SNAPSHOT.exists()
    assert _GRAPH_FEATURES.exists()
    assert _import_violations(_GRAPH_PORTS) == []
    assert _import_violations(_GRAPH_SNAPSHOT) == []
    assert _import_violations(_GRAPH_FEATURES) == []
    from trader.application.world_model.graph_ports import WorldGraphTraversalPort

    assert hasattr(WorldGraphTraversalPort, "enumerate_paths")
    parameters = inspect.signature(WorldGraphTraversalPort.enumerate_paths).parameters
    assert "structural" in parameters
    assert "knowledge" in parameters
    assert "root" in parameters
    assert "ledger" not in parameters
    assert "store" not in parameters


def test_same_input_and_cutoff_yield_the_same_snapshot_identity() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    revision = _seed_rfc_graph(ledger, mapping)
    service = _service(ledger)
    first = service.build(_request(mapping))
    second = service.build(_request(mapping))
    snapshot = first.snapshot
    assert first.snapshot.snapshot_id == second.snapshot.snapshot_id
    assert first.snapshot.content_sha256 == second.snapshot.content_sha256
    assert snapshot.snapshot_id.startswith("world_graph_snapshot:v1:")
    assert snapshot.root_episode_id == _episode().episode_id
    assert snapshot.root_entity == _instrument()
    assert snapshot.cutoff_at == CUTOFF
    assert snapshot.ontology_revision == revision.revision_id
    assert snapshot.ontology_hash == revision.content_sha256
    assert snapshot.scope_mapping_id == mapping.mapping_id
    assert snapshot.scope_mapping_hash == mapping.content_sha256
    assert snapshot.identity_map_hash == revision.identity_map_hash
    assert snapshot.traversal_policy_version == GRAPH_TRAVERSAL_POLICY_VERSION
    assert snapshot.status == "complete"
    assert dict(snapshot.missingness) == {}
    encoded = json.dumps(snapshot.to_dict())
    assert "networkx" not in encoded.lower()
    assert "MultiDiGraph" not in encoded
    assert snapshot.knowledge_relation_refs
    assert all(isinstance(item, WorldKnowledgeRelationRef) for item in snapshot.knowledge_relation_refs)
    assert all(item.availability_receipt_id.startswith("world-availability-receipt:v1:") for item in snapshot.knowledge_relation_refs)
    observes = next(item for item in first.knowledge_relations if item.kind == "OBSERVES")
    observes_receipt = next(
        ref.availability_receipt_id
        for ref in snapshot.knowledge_relation_refs
        if ref.relation_id == observes.relation_id
    )
    assert (
        WorldKnowledgeRelationRef.from_relation(observes, availability_receipt_id=observes_receipt)
        in snapshot.knowledge_relation_refs
    )
    traded = next(item for item in first.structural_relations if item.kind == "TRADED_ON")
    assert WorldStructuralRelationRef.from_relation(traded) in snapshot.structural_relation_refs
    assert _artifact_ref().artifact_id in snapshot.artifact_refs
    signatures = [path.signature for path in first.paths.paths]
    assert any("OBSERVES:reverse:0-4h" in item for item in signatures)
    assert all("2330" not in path.signature for path in first.paths.paths)
    assert first.paths.policy_version == "graph_traversal.v1"


def test_late_unproven_and_future_knowledge_stay_out_of_the_snapshot() -> None:
    mapping = _mapping()
    late_ledger = _InMemoryWorldGraphLedger(now=READY)
    _seed_rfc_graph(late_ledger, mapping, knowledge=())
    late_ledger.now = LATER
    WorldOntologyService(late_ledger).assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=_knowledge()))
    late_bundle = _service(late_ledger).build(_request(mapping))
    assert late_bundle.snapshot.knowledge_relation_refs == frozenset()
    assert all(item.kind != "OBSERVES" for item in late_bundle.knowledge_relations)

    unproven_ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(unproven_ledger, mapping, knowledge=())
    unproven_ledger.seed_unproven_knowledge(KnowledgeWorldRelationAsserted(relation=_knowledge()))
    unproven = _service(unproven_ledger).build(_request(mapping))
    assert unproven.knowledge_relations == ()
    assert unproven.snapshot.knowledge_relation_refs == frozenset()

    present_ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(present_ledger, mapping, knowledge=())
    future = _knowledge(effective_from=LATER, source_refs=(f"macro_world_observation:v1:{SHA}",))
    WorldOntologyService(present_ledger).assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=future))
    present = _service(present_ledger).build(_request(mapping))
    assert future not in present.knowledge_relations
    assert all(ref.relation_id != future.relation_id for ref in present.snapshot.knowledge_relation_refs)


def test_unmapped_and_ambiguous_slots_are_valid_members_without_fabricated_observes() -> None:
    mapping = WorldScopeMapping(mapping_id="world_scope_mapping.v1", entries=())
    ledger = _InMemoryWorldGraphLedger()
    WorldOntologyService(ledger).publish_revision(
        PublishWorldOntologyRevision(
            revision=_revision_for(mapping, entities=(), structural=(), identity_links=())
        )
    )
    unmapped = WorldScopeResolution(
        mapping_id=mapping.mapping_id,
        mapping_sha256=mapping.content_sha256,
        anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
        status="unmapped",
    )
    bundle = _service(ledger).build(_request(mapping, scope_resolution=unmapped))
    assert bundle.snapshot.status == "missing"
    assert bundle.snapshot.missingness["scope"] == "unmapped"
    assert bundle.snapshot.knowledge_relation_refs == frozenset()
    assert bundle.snapshot.structural_relation_refs == frozenset()
    assert bundle.knowledge_relations == ()
    assert all(item.kind != "OBSERVES" for item in bundle.knowledge_relations)

    ambiguous = WorldScopeResolution(
        mapping_id=mapping.mapping_id,
        mapping_sha256=mapping.content_sha256,
        anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
        status="ambiguous",
    )
    amb = _service(ledger).build(_request(mapping, scope_resolution=ambiguous))
    assert amb.snapshot.status == "missing"
    assert amb.snapshot.missingness["scope"] == "ambiguous"
    assert amb.snapshot.knowledge_relation_refs == frozenset()


def test_scope_mapping_mismatch_and_contradictory_heads_are_rejected() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    service = _service(ledger)
    other = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="US", instrument="AAPL"),
                venue={"kind": "venue", "entity_id": "mic:XNAS"},
                country={"kind": "country", "entity_id": "iso-3166:US"},
                region={"kind": "region", "entity_id": "iso-un-m49:021"},
                world={"kind": "world", "entity_id": "market"},
                provider_proofs=("provider:world-scope:xnas",),
                taxonomy_version="sessions_mic.v1",
            ),
        ),
    )
    with pytest.raises(ValueError, match="scope_mapping"):
        service.build(_request(other, scope_resolution=_resolution_for(other, instrument="AAPL")))

    drifted = WorldScopeResolution(
        mapping_id=mapping.mapping_id,
        mapping_sha256="f" * 64,
        anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
        status="unmapped",
    )
    with pytest.raises(ValueError, match="scope"):
        service.build(_request(mapping, scope_resolution=drifted))

    wrong_country = _InMemoryWorldGraphLedger()
    mapping_ok = _mapping()
    structural = (
        _structural(),
        _structural(
            kind="LOCATED_IN",
            source=_venue(),
            target=WorldEntityRef(kind="country", entity_id="iso-3166:JP"),
            source_refs=("world-scope-mapping:wrong-country",),
        ),
        _structural(
            kind="LOCATED_IN",
            source=WorldEntityRef(kind="country", entity_id="iso-3166:JP"),
            target=_region(),
            source_refs=("world-scope-mapping:jp-region",),
        ),
        _structural(
            kind="PART_OF_WORLD",
            source=_region(),
            target=_world(),
            source_refs=("world-scope-mapping:region-world",),
        ),
    )
    entities = (_instrument(), _venue(), WorldEntityRef(kind="country", entity_id="iso-3166:JP"), _region(), _world())
    service_wrong = WorldOntologyService(wrong_country)
    for entity in entities:
        service_wrong.assert_entity(
            AssertWorldEntity(entity=entity, source_refs=(f"provider:{entity.node_id}",), effective_from=T0)
        )
    for relation in structural:
        service_wrong.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
    service_wrong.publish_revision(
        PublishWorldOntologyRevision(revision=_revision_for(mapping_ok, entities=entities, structural=structural))
    )
    with pytest.raises(ValueError, match="topology|heads"):
        _service(wrong_country).build(_request(mapping_ok))


def test_path_budget_marks_partial_snapshot_and_does_not_invent_members() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    bundle = _service(ledger).build(_request(mapping, max_paths=1))
    assert bundle.paths.status == "graph_budget_exceeded"
    assert bundle.snapshot.status == "partial"
    assert bundle.snapshot.missingness["budget"] == "graph_budget_exceeded"
    member_ids = {ref.relation_id for ref in bundle.snapshot.structural_relation_refs} | {
        ref.relation_id for ref in bundle.snapshot.knowledge_relation_refs
    }
    path_ids = {step.relation_id for path in bundle.paths.paths for step in path.steps}
    assert member_ids == path_ids


def test_unpublished_ontology_is_a_missing_snapshot_without_traversal_authority() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    WorldOntologyService(ledger).assert_entity(
        AssertWorldEntity(entity=_instrument(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    bundle = _service(ledger).build(_request(mapping, scope_resolution=_resolution_for(mapping)))
    assert bundle.snapshot.status == "missing"
    assert bundle.snapshot.missingness["ontology"] == "unpublished"
    assert bundle.snapshot.structural_relation_refs == frozenset()
    assert bundle.snapshot.knowledge_relation_refs == frozenset()
    assert bundle.paths.paths == ()


def test_snapshot_ledger_append_is_explicit_and_idempotent_by_identity() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    snapshots = _InMemorySnapshotLedger()
    service = _service(ledger, snapshot_ledger=snapshots)
    bundle = service.build(_request(mapping))
    assert snapshots.appends == 0
    first = service.persist(bundle.snapshot)
    second = service.persist(bundle.snapshot)
    assert snapshots.appends == 2
    assert first.identity == second.identity
    loaded = snapshots.get(first.identity)
    assert loaded == bundle.snapshot
    replayed = json.loads(json.dumps(loaded.to_dict()))
    assert "networkx" not in json.dumps(replayed).lower()


def test_snapshot_membership_is_bound_to_published_revision_refs() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    revision = _seed_rfc_graph(ledger, mapping)
    extra_family = WorldEntityRef(kind="family", entity_id="taxonomy:v1:later-family")
    extra = _structural(
        kind="MEMBER_OF_FAMILY",
        source=_instrument(),
        target=extra_family,
        source_refs=("taxonomy:v1:later-family",),
    )
    ontology = WorldOntologyService(ledger)
    ontology.assert_entity(AssertWorldEntity(entity=extra_family, source_refs=("taxonomy:v1:later-family",), effective_from=T0))
    ontology.assert_structural_relation(AssertStructuralWorldRelation(relation=extra))
    bundle = _service(ledger).build(_request(mapping))
    extra_ref = WorldStructuralRelationRef.from_relation(extra)
    assert bundle.snapshot.ontology_revision == revision.revision_id
    assert bundle.snapshot.ontology_hash == revision.content_sha256
    assert extra_ref not in bundle.snapshot.structural_relation_refs
    assert extra not in bundle.structural_relations
    assert extra_family.node_id not in {item.entity.node_id for item in bundle.snapshot.entity_revision_refs}


def test_later_structural_relation_is_visible_only_after_superseding_revision() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    extra_family = WorldEntityRef(kind="family", entity_id="taxonomy:v1:later-family")
    extra = _structural(
        kind="MEMBER_OF_FAMILY",
        source=_instrument(),
        target=extra_family,
        source_refs=("taxonomy:v1:later-family",),
    )
    ontology = WorldOntologyService(ledger)
    ontology.assert_entity(AssertWorldEntity(entity=extra_family, source_refs=("taxonomy:v1:later-family",), effective_from=T0))
    ontology.assert_structural_relation(AssertStructuralWorldRelation(relation=extra))
    before = _service(ledger).build(_request(mapping))
    extra_ref = WorldStructuralRelationRef.from_relation(extra)
    assert extra_ref not in before.snapshot.structural_relation_refs

    successor = WorldOntologyRevision(
        revision_id="market_ontology.v2",
        entities=_rfc_entities() + (extra_family,),
        structural_relation_refs=tuple(
            WorldStructuralRelationRef.from_relation(item) for item in _rfc_structural() + (extra,)
        ),
        identity_link_refs=(_link().as_ref(),),
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
    )
    ontology.supersede_revision(
        SupersedeWorldOntologyRevision(revision_id="market_ontology.v1", successor_revision_id=successor.revision_id)
    )
    ontology.publish_revision(PublishWorldOntologyRevision(revision=successor))
    after = _service(ledger).build(_request(mapping))
    assert after.snapshot.ontology_revision == "market_ontology.v2"
    assert after.snapshot.ontology_hash == successor.content_sha256
    assert extra_ref in after.snapshot.structural_relation_refs
    assert extra in after.structural_relations
