"""GRAPH-3B: RegisterMacroObservationKnowledge and consumer-owned graph ports."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.domain.test_world_graph_bridge_lifecycle import _drifted_committed_specs
from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
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
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
    MacroGraphBridgeFence,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRunSpec,
    MacroGraphObservationLinked,
    MacroGraphObservationSkipped,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    MacroObservationKnowledgeLink,
    StaleBridgeEpoch,
    StructuralWorldRelation,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
    parse_world_relation_event,
)
from trader.domain.world_graph_bridge_lifecycle import UnknownMacroGraphBridgeDrift
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
    MacroWorldObservation,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 8, 23, 12, 5, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SHA = "a" * 64
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64
_GRAPH_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "graph_ports.py"
_BRIDGE = REPO_ROOT / "trader" / "application" / "world_model" / "graph_observation_bridge.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _scope(*, kind: str = "venue", entity_id: str = "mic:XTAI") -> MacroScope:
    return MacroScope(kind=kind, entity_id=entity_id)


def _fact(*, scope: MacroScope | None = None) -> MacroSourceFact:
    return MacroSourceFact(
        fact_kind="series_point",
        metric_key="policy_rate",
        scope=scope if scope is not None else _scope(),
        value=MacroNumericValue(number=4.25, unit="percent"),
        period="2026-08",
        occurred_at="2026-08-23T00:00:00Z",
        published_at="2026-08-23T12:30:00Z",
        ingested_at="2026-08-23T12:31:10Z",
        source=MacroFactSource(
            provider_id="dbnomics",
            adapter_version="world_dbnomics_series.v1",
            source_record_id="stable-provider-id",
            source_ref="https://source.example/record",
        ),
    )


def _observation(*, scope: MacroScope | None = None, cutoff_at: datetime = CUTOFF) -> MacroWorldObservation:
    resolved_scope = scope if scope is not None else _scope()
    fact = _fact(scope=resolved_scope)
    fact_ref = fact.fact_version_id.value
    return MacroWorldObservation(
        scope=resolved_scope,
        cutoff_at=cutoff_at,
        producer_version=MACRO_PRODUCER_VERSION,
        transform_version=MACRO_TRANSFORM_VERSION,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        fact_refs=(fact_ref,),
        features={"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
        dimensions=(
            MacroDimensionState(
                dimension="macro_regime",
                value="mixed",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(fact_ref,),
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value="stable",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(fact_ref,),
            ),
            MacroDimensionState(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(),
            ),
        ),
        coverage=MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("broad_usd_index",),
        ),
        valid_until=VALID_UNTIL,
    )


def _envelope(observation: MacroWorldObservation | None = None) -> MacroObservationEnvelope:
    resolved = observation if observation is not None else _observation()
    subject = WorldAvailabilitySubjectRef(
        kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
        subject_id=resolved.observation_id,
        content_sha256=resolved.content_sha256,
    )
    locator = WorldStorageLocator(kind="jsonl", store_id="world-macro-jsonl.v1", path="observations/2026-08-23.jsonl")
    identity = _receipt_identity_payload(
        schema_version="availability_receipt.v2",
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


def _plan(*, digest: str = "b" * 64) -> MacroCollectionPlan:
    return MacroCollectionPlan(
        registry_version="world_macro_sources.v1",
        registry_content_sha256=digest,
        targets=(
            MacroCollectionTarget(
                scope=_scope(),
                source_ids=("official_provider",),
            ),
        ),
    )


def _mapping() -> WorldScopeMapping:
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


def _revision(mapping: WorldScopeMapping) -> WorldOntologyRevision:
    entity = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")
    venue = WorldEntityRef(kind="venue", entity_id="mic:XTAI")
    structural = StructuralWorldRelation(
        kind="TRADED_ON",
        source=entity,
        target=venue,
        effective_from=T0,
        ontology_revision="market_ontology.v1",
        source_refs=("provider:instrument-master:2330",),
    )
    link = WorldEntityIdentityLink(
        v2_ref=EntityRef(kind="instrument", entity_id="2330"),
        v3_ref=entity,
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )
    return WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=(entity, venue),
        structural_relation_refs=(WorldStructuralRelationRef.from_relation(structural),),
        identity_link_refs=(link.as_ref(),),
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
    )


def _origin() -> MacroObservationCursor:
    return MacroObservationCursor(receipt_log_generation=1, ordinal=0)


def _cursor_for(envelope: MacroObservationEnvelope, ordinal: int) -> MacroObservationCursor:
    return MacroObservationCursor(
        receipt_log_generation=1,
        ordinal=ordinal,
        receipt_id=envelope.persisted.receipt.receipt_id,
        observation_id=envelope.observation.observation_id,
    )


def _attested_event_receipt(event: Any, *, kind: str, ready: datetime = READY) -> WorldAvailabilityReceipt:
    payload = event.to_dict()
    subject = WorldAvailabilitySubjectRef(
        kind=kind,
        subject_id=event.event_id,
        content_sha256=canonical_sha256(payload),
    )
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


class _Scan:
    def __init__(self) -> None:
        self.reservations: dict[tuple[str, str], MacroObservationCursorReservation] = {}
        self.envelopes: list[tuple[MacroObservationCursor, MacroObservationEnvelope]] = []
        self.reserve_calls = 0

    def seed(self, envelope: MacroObservationEnvelope, ordinal: int) -> MacroObservationCursor:
        cursor = _cursor_for(envelope, ordinal)
        self.envelopes.append((cursor, envelope))
        return cursor

    def reserve_activation_cursor(self, bridge_key, request_id) -> MacroObservationCursorReservation:
        self.reserve_calls += 1
        key = (str(bridge_key), str(request_id))
        existing = self.reservations.get(key)
        if existing is not None:
            return existing
        head = self.envelopes[-1][0] if self.envelopes else _origin()
        reservation = MacroObservationCursorReservation(
            bridge_key=str(bridge_key),
            request_id=str(request_id),
            cursor=head,
        )
        self.reservations[key] = reservation
        return reservation

    def list_available_after(self, cursor: MacroObservationCursor, limit: int):
        items = [
            envelope
            for item_cursor, envelope in self.envelopes
            if item_cursor.receipt_log_generation == cursor.receipt_log_generation
            and item_cursor.ordinal > cursor.ordinal
        ]
        return tuple(items[:limit])

    def cursor_for(self, observation_id: str) -> MacroObservationCursor:
        for cursor, envelope in self.envelopes:
            if envelope.observation.observation_id == observation_id:
                return cursor
        raise KeyError(observation_id)


class _Graph:
    def __init__(self) -> None:
        self.bridge: _Bridge | None = None
        self.knowledge: list[KnowledgeWorldRelationAsserted] = []
        self.receipts_issued: set[str] = set()
        self.fail_receipt_for: set[str] = set()
        self.fence_calls: list[MacroGraphBridgeFence | None] = []
        self.stale_before_payload = False
        self.stale_before_receipt = False

    def append_knowledge_relation_event(
        self,
        event: KnowledgeWorldRelationAsserted | KnowledgeWorldRelationRetired,
        fence: MacroGraphBridgeFence | None = None,
        expected_registry_version: int | None = None,
    ) -> PersistedWorldRef[str]:
        self.fence_calls.append(fence)
        parsed = parse_world_relation_event(event)
        if fence is None:
            raise ValueError("bridge knowledge append requires a fence")
        if self.bridge is not None:
            current = self.bridge.registry.fence
            run = self.bridge.registry.active_run
            allowed = {"active", "blocked"} if isinstance(parsed, KnowledgeWorldRelationRetired) else {"active"}
            if current != fence or run is None or run.status not in allowed:
                raise StaleBridgeEpoch("stale_bridge_epoch")
            if expected_registry_version != self.bridge.registry.version:
                raise StaleBridgeEpoch("stale_bridge_epoch")
        if self.stale_before_payload:
            raise StaleBridgeEpoch("stale_bridge_epoch")
        existing = next((item for item in self.knowledge if item.event_id == parsed.event_id), None)
        if existing is None:
            self.knowledge.append(parsed)
        elif existing.to_dict() != parsed.to_dict():
            raise ValueError("conflict: same relation event with different content")
        if self.stale_before_receipt:
            raise StaleBridgeEpoch("stale_bridge_epoch")
        if parsed.event_id in self.fail_receipt_for:
            raise OSError("relation receipt commit failed")
        self.receipts_issued.add(parsed.event_id)
        from trader.application.world_model.graph_ports import WorldRelationEventId

        return PersistedWorldRef(
            identity=WorldRelationEventId(parsed.event_id),
            receipt=_attested_event_receipt(parsed, kind="world_relation_event"),
        )

    def get_knowledge_relation_event(self, event_id):
        return next((item for item in self.knowledge if item.event_id == str(event_id)), None)

    def list_knowledge_relation_events_available_through(self, cutoff_at: datetime):
        from trader.application.world_model.graph_ports import WorldRelationEventEnvelope

        envelopes = []
        for event in self.knowledge:
            if event.event_id not in self.receipts_issued:
                continue
            receipt = _attested_event_receipt(event, kind="world_relation_event")
            evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=READY)
            if evidence.effective_ready_at > cutoff_at:
                continue
            envelopes.append(WorldRelationEventEnvelope(event=event, evidence=evidence))
        return tuple(envelopes)


class _Bridge:
    def __init__(self) -> None:
        self.registry = MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
        self.unproven: list[object] = []
        self.fail_receipt_types: set[str] = set()
        self.stale = False

    def load(self, bridge_key) -> MacroGraphBridgeRegistry:
        del bridge_key
        return self.registry

    def append_event(
        self,
        event,
        expected_registry_version: int,
        fence: MacroGraphBridgeFence | None = None,
    ) -> PersistedWorldRef[str]:
        from trader.application.world_model.graph_ports import MacroGraphBridgeEventId

        if self.stale:
            raise StaleBridgeEpoch("stale_bridge_epoch")
        if fence is not None:
            current = self.registry.fence
            if current is None or current != fence:
                raise StaleBridgeEpoch("stale_bridge_epoch")
            if self.registry.active_run is None or self.registry.active_run.status != "active":
                raise StaleBridgeEpoch("stale_bridge_epoch")
        if expected_registry_version != self.registry.version:
            existing = next((item for item in self.registry.events if item.event_id == event.event_id), None)
            if existing is not None and existing.to_dict() == event.to_dict():
                receipt = _attested_event_receipt(event, kind="macro_graph_bridge_event")
                return PersistedWorldRef(identity=MacroGraphBridgeEventId(event.event_id), receipt=receipt)
            raise ValueError("expected_version does not match the registry version")
        if event.event_type in self.fail_receipt_types:
            self.unproven.append(event)
            raise OSError("bridge event receipt commit failed")
        self.registry = MacroGraphBridgeRegistry(
            bridge_key=self.registry.bridge_key,
            events=(*self.registry.events, event),
        )
        receipt = _attested_event_receipt(event, kind="macro_graph_bridge_event")
        return PersistedWorldRef(identity=MacroGraphBridgeEventId(event.event_id), receipt=receipt)


def _use_case(
    scan: _Scan,
    graph: _Graph,
    bridge: _Bridge,
    mapping: WorldScopeMapping | None = None,
    *,
    collection_plan: MacroCollectionPlan | None = None,
    structural_revision: WorldOntologyRevision | None = None,
):
    from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge

    resolved = mapping if mapping is not None else _mapping()
    graph.bridge = bridge
    return RegisterMacroObservationKnowledge(
        scan=scan,
        graph=graph,
        bridge=bridge,
        scope_mapping=resolved,
        structural_revision=structural_revision if structural_revision is not None else _revision(resolved),
        collection_plan=collection_plan if collection_plan is not None else _plan(),
        bridge_key=BRIDGE_KEY,
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
    if "world_graph_store" in source or "world_macro_store" in source:
        violations.append(f"{rel_path}: store mentioned")
    return violations


def test_bridge_modules_do_not_import_store_networkx_runtime_or_reporting() -> None:
    assert _GRAPH_PORTS.exists()
    assert _BRIDGE.exists()
    assert _import_violations(_GRAPH_PORTS) == []
    assert _import_violations(_BRIDGE) == []


def test_use_case_cannot_inject_a_migration() -> None:
    from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge

    assert "migration" not in inspect.signature(RegisterMacroObservationKnowledge.__init__).parameters
    source = _BRIDGE.read_text(encoding="utf-8")
    assert "self._migration" not in source
    assert "classify_macro_graph_bridge(registry, desired=desired)" in source


def test_ports_expose_scan_bridge_ledger_and_fenced_knowledge_append() -> None:
    from trader.application.world_model.graph_ports import (
        MacroGraphBridgeLedger,
        MacroObservationScanPort,
        WorldGraphLedger,
    )

    reserve = inspect.signature(MacroObservationScanPort.reserve_activation_cursor)
    assert list(reserve.parameters) == ["self", "bridge_key", "request_id"]
    listed = inspect.signature(MacroObservationScanPort.list_available_after)
    assert list(listed.parameters) == ["self", "cursor", "limit"]
    assert hasattr(MacroObservationScanPort, "cursor_for")
    assert hasattr(WorldGraphLedger, "get_knowledge_relation_event")
    append_event = inspect.signature(MacroGraphBridgeLedger.append_event)
    assert "expected_registry_version" in append_event.parameters
    assert "fence" in append_event.parameters
    knowledge = inspect.signature(WorldGraphLedger.append_knowledge_relation_event)
    assert "fence" in knowledge.parameters
    assert "expected_registry_version" in knowledge.parameters
    assert "ready_at" not in knowledge.parameters


def test_capture_does_not_create_a_relation_until_the_use_case_runs() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.activate(REQUEST_ID)
    assert graph.knowledge == []
    scan.seed(_envelope(), 1)
    assert graph.knowledge == []
    use_case.reconcile(limit=8)
    assert len(graph.knowledge) == 1
    assert graph.knowledge[0].relation.kind == "OBSERVES"


def test_activation_retry_reuses_the_reserved_cursor_and_does_not_skip_the_gap() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    first = _envelope(_observation())
    scan.seed(first, 1)
    use_case = _use_case(scan, graph, bridge)
    first_registry = use_case.activate(REQUEST_ID)
    later = _envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:TW")))
    scan.seed(later, 2)
    retried = use_case.activate(REQUEST_ID)
    assert scan.reserve_calls == 2
    assert retried.active_run is not None
    assert retried.active_run.activation_cursor == first_registry.active_run.activation_cursor
    assert retried.active_run.activation_cursor.ordinal == 1
    use_case.reconcile(limit=8)
    assert len(graph.knowledge) == 1
    assert graph.knowledge[0].relation.target.entity_id == "iso-3166:TW"


def test_unregistered_scope_is_a_terminal_skip_and_unknown_local_scope_never_invents_a_relation() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.activate("macro_graph_bridge_request:v1:" + "d" * 64)
    scan.seed(_envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:FR"))), 1)
    registry = use_case.reconcile(limit=8)
    assert graph.knowledge == []
    skips = [event for event in registry.events if isinstance(event, MacroGraphObservationSkipped)]
    assert len(skips) == 1
    assert skips[0].reason == "scope_not_registered"


def test_post_receipt_relation_is_invisible_at_an_earlier_cutoff() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.activate(REQUEST_ID)
    scan.seed(_envelope(), 1)
    use_case.reconcile(limit=8)
    before = graph.list_knowledge_relation_events_available_through(datetime(2026, 8, 23, 11, 0, tzinfo=UTC))
    after = graph.list_knowledge_relation_events_available_through(datetime(2026, 8, 24, tzinfo=UTC))
    assert before == ()
    assert len(after) == 1


def test_config_drift_blocks_without_advancing_or_skipping() -> None:
    mapping = _mapping()
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge, mapping)
    use_case.activate(REQUEST_ID)
    scan.seed(_envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:FR"))), 1)
    from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge

    drifted = WorldScopeMapping(mapping_id="world_scope_mapping.v2", entries=mapping.entries)
    drifted_case = RegisterMacroObservationKnowledge(
        scan=scan,
        graph=graph,
        bridge=bridge,
        scope_mapping=drifted,
        structural_revision=_revision(drifted),
        collection_plan=_plan(),
        bridge_key=BRIDGE_KEY,
    )
    registry = drifted_case.reconcile(limit=8)
    assert registry.active_run is not None
    assert registry.active_run.status == "blocked"
    assert registry.active_run.block_reason == "config_drift"
    assert graph.knowledge == []
    assert not any(isinstance(event, MacroGraphObservationSkipped) for event in registry.events)
    assert registry.active_run.cursor.ordinal == 0


def test_identical_retry_is_noop_and_stops_at_unproven_relation_receipt() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.activate(REQUEST_ID)
    first = _envelope()
    second = _envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:TW")))
    scan.seed(first, 1)
    scan.seed(second, 2)
    graph.fail_receipt_for.add("pending")
    first_event = KnowledgeWorldRelationAsserted(
        relation=MacroObservationKnowledgeLink.from_envelope(first, _mapping(), _revision(_mapping())).relation
    )
    graph.fail_receipt_for.add(first_event.event_id)
    registry = use_case.reconcile(limit=8)
    assert len(graph.knowledge) == 1
    assert first_event.event_id not in graph.receipts_issued
    assert not any(isinstance(event, MacroGraphObservationLinked) for event in registry.events)
    graph.fail_receipt_for.clear()
    retried = use_case.reconcile(limit=8)
    assert first_event.event_id in graph.receipts_issued
    assert any(isinstance(event, MacroGraphObservationLinked) for event in retried.events)
    again = use_case.reconcile(limit=8)
    assert len(graph.knowledge) == 2
    assert again.active_run.cursor.ordinal == 2


def test_old_worker_is_fenced_before_payload_receipt_terminal_and_cursor() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.activate(REQUEST_ID)
    scan.seed(_envelope(), 1)
    graph.stale_before_payload = True
    with pytest.raises(StaleBridgeEpoch):
        use_case.reconcile(limit=8)
    assert graph.knowledge == []
    graph.stale_before_payload = False
    graph.stale_before_receipt = True
    with pytest.raises(StaleBridgeEpoch):
        use_case.reconcile(limit=8)
    assert len(graph.knowledge) == 1
    assert graph.knowledge[0].event_id not in graph.receipts_issued
    graph.stale_before_receipt = False
    bridge.stale = True
    with pytest.raises(StaleBridgeEpoch):
        use_case.reconcile(limit=8)


def test_ensure_missing_reserves_once_and_matched_active_is_noop() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    first = use_case.ensure(REQUEST_ID)
    assert first.active_run is not None
    assert first.active_run.status == "active"
    assert first.active_run.spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
    assert first.active_run.spec.collection_plan_id == _plan().plan_id
    assert first.active_run.spec.producer_version == MACRO_PRODUCER_VERSION
    assert scan.reserve_calls == 1
    again = use_case.ensure(REQUEST_ID)
    assert again.events == first.events
    assert scan.reserve_calls == 1


def test_ensure_matched_blocked_resumes_without_reserving() -> None:
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge)
    use_case.ensure(REQUEST_ID)
    blocked = bridge.registry.block(reason="config_drift", expected_version=bridge.registry.version)
    bridge.registry = blocked
    assert scan.reserve_calls == 1
    resumed = use_case.ensure(REQUEST_ID)
    assert resumed.active_run is not None
    assert resumed.active_run.status == "active"
    assert scan.reserve_calls == 1


def test_ensure_unknown_drift_blocks_without_activate_and_fails_closed() -> None:
    mapping = _mapping()
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    use_case = _use_case(scan, graph, bridge, mapping)
    use_case.ensure(REQUEST_ID)
    drifted = WorldScopeMapping(mapping_id="world_scope_mapping.v2", entries=mapping.entries)
    drifted_case = _use_case(scan, graph, bridge, drifted)
    with pytest.raises(UnknownMacroGraphBridgeDrift, match="unknown_config_drift"):
        drifted_case.ensure(REQUEST_ID)
    assert bridge.registry.active_run is not None
    assert bridge.registry.active_run.status == "blocked"
    assert bridge.registry.active_run.block_reason == "config_drift"
    assert scan.reserve_calls == 1


def _seed_owned_observes(scan: _Scan, graph: _Graph, bridge: _Bridge, spec: MacroGraphBridgeRunSpec):
    mapping = _mapping()
    reservation = scan.reserve_activation_cursor(BRIDGE_KEY, REQUEST_ID)
    bridge.registry = bridge.registry.activate(reservation=reservation, spec=spec, expected_version=0)
    graph.bridge = bridge
    envelope = _envelope()
    cursor = scan.seed(envelope, 1)
    link = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, _revision(mapping))
    asserted = KnowledgeWorldRelationAsserted(relation=link.relation)
    graph.append_knowledge_relation_event(
        asserted,
        fence=bridge.registry.fence,
        expected_registry_version=bridge.registry.version,
    )
    linked = bridge.registry.link_observation(
        observation_id=envelope.observation.observation_id,
        relation=link.relation,
        relation_event_id=asserted.event_id,
        cursor=cursor,
        expected_version=bridge.registry.version,
    )
    bridge.registry = linked
    about = KnowledgeWorldRelationAsserted(
        relation=KnowledgeWorldRelation(
            kind="ABOUT",
            source=KnowledgeArtifactRef(
                artifact_id="knowledge_artifact:v1:" + "4" * 64,
                content_sha256="f" * 64,
            ),
            target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"),
            effective_from=CUTOFF,
            ontology_revision=_revision(mapping).revision_id,
            source_refs=("artifact:proof",),
        )
    )
    graph.knowledge.append(about)
    return mapping, link, about


@pytest.mark.parametrize("side,field,durable,desired", _drifted_committed_specs())
def test_ensure_one_bit_identity_drift_does_not_retire_or_handoff(
    side: str,
    field: str,
    durable: MacroGraphBridgeRunSpec,
    desired: MacroGraphBridgeRunSpec,
) -> None:
    del side, field
    scan = _Scan()
    graph = _Graph()
    bridge = _Bridge()
    mapping, link, about = _seed_owned_observes(scan, graph, bridge, durable)
    predecessor_run_id = bridge.registry.active_run.run_id
    predecessor_event_ids = {event.event_id for event in bridge.registry.events}
    use_case = _use_case(scan, graph, bridge, mapping)
    use_case._spec = lambda: desired  # type: ignore[method-assign]
    with pytest.raises(UnknownMacroGraphBridgeDrift, match="unknown_config_drift"):
        use_case.ensure(REQUEST_ID)
    blocked = bridge.registry
    assert blocked.active_run is not None
    assert blocked.active_run.run_id == predecessor_run_id
    assert blocked.active_run.status == "blocked"
    assert blocked.active_run.epoch == 1
    assert not any(event.event_type == "macro_graph_bridge_run_handed_off" for event in blocked.events)
    assert predecessor_event_ids <= {event.event_id for event in blocked.events}
    retired = [item for item in graph.knowledge if isinstance(item, KnowledgeWorldRelationRetired)]
    assert retired == []
    asserted_ids = {
        item.relation.relation_id for item in graph.knowledge if isinstance(item, KnowledgeWorldRelationAsserted)
    }
    assert about.event_id in {item.event_id for item in graph.knowledge}
    assert link.relation.relation_id in asserted_ids
    assert scan.reserve_calls == 1
