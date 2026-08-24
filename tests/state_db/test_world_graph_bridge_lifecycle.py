"""Real-store lifecycle: v1 persistence, append-only OBSERVES remediation, handoff."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.application.test_world_graph_observation_bridge import _envelope, _observation, _scope
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge
from trader.application.world_model.ontology_bootstrap import derive_market_ontology
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_context import EntityRef
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1,
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
    MacroGraphBridgeRunSpec,
    MacroGraphObservationLinked,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
    StructuralWorldRelation,

    parse_macro_graph_bridge_event,
)
from trader.domain.world_graph_bridge_lifecycle import (
    UnknownMacroGraphBridgeDrift,
    committed_macro_graph_bridge_predecessor_spec,
    committed_macro_graph_bridge_successor_spec,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MacroCollectionPlan,
    MacroCollectionTarget,
    MacroScope,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
LATER = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64


def _digest(label: str) -> str:
    return canonical_sha256({"label": label})


def _mapping(*, mapping_id: str = "world_scope_mapping.v1") -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id=mapping_id,
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


def _revision(mapping: WorldScopeMapping, *, revision_id: str = "market_ontology.v1") -> WorldOntologyRevision:
    entity = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")
    venue = WorldEntityRef(kind="venue", entity_id="mic:XTAI")
    structural = StructuralWorldRelation(
        kind="TRADED_ON",
        source=entity,
        target=venue,
        effective_from=T0,
        ontology_revision=revision_id,
        source_refs=("provider:instrument-master:2330",),
    )
    link = WorldEntityIdentityLink(
        v2_ref=EntityRef(kind="instrument", entity_id="2330"),
        v3_ref=entity,
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )
    return WorldOntologyRevision(
        revision_id=revision_id,
        entities=(entity, venue),
        structural_relation_refs=(WorldStructuralRelationRef.from_relation(structural),),
        identity_link_refs=(link.as_ref(),),
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
    )


def _plan(*, digest: str = "b" * 64) -> MacroCollectionPlan:
    return MacroCollectionPlan(
        registry_version="macro_sources.v1",
        registry_content_sha256=digest,
        targets=(
            MacroCollectionTarget(
                scope=MacroScope(kind="venue", entity_id="mic:XTAI"),
                source_ids=("official_provider",),
            ),
        ),
    )


def _v1_spec(mapping: WorldScopeMapping, revision: WorldOntologyRevision) -> MacroGraphBridgeRunSpec:
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
        ontology_revision_id=revision.revision_id,
        ontology_revision_hash=revision.content_sha256,
    )


def _reservation() -> MacroObservationCursorReservation:
    return MacroObservationCursorReservation(
        bridge_key=BRIDGE_KEY,
        request_id=REQUEST_ID,
        cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
    )


def _cursor(ordinal: int, observation_id: str) -> MacroObservationCursor:
    return MacroObservationCursor(
        receipt_log_generation=1,
        ordinal=ordinal,
        receipt_id="world-availability-receipt:v1:" + _digest(f"receipt:{ordinal}"),
        observation_id=observation_id,
    )


def _observes(index: int, revision: WorldOntologyRevision) -> tuple[str, KnowledgeWorldRelation]:
    observation_id = "macro_world_observation:v1:" + _digest(f"observation:{index}")
    relation = KnowledgeWorldRelation(
        kind="OBSERVES",
        source=WorldObservationRef(observation_id="world_observation:v1:" + _digest(f"world-obs:{index}")),
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"),
        effective_from=CUTOFF,
        effective_until=VALID_UNTIL,
        ontology_revision=revision.revision_id,
        source_refs=(f"{observation_id}/{_digest(f'obs-hash:{index}')}",),
    )
    return observation_id, relation


class _Scan:
    def __init__(self) -> None:
        self.reserve_calls = 0
        self.envelopes: list[tuple[MacroObservationCursor, object]] = []
        self.listed_after: list[MacroObservationCursor] = []

    def reserve_activation_cursor(self, bridge_key, request_id) -> MacroObservationCursorReservation:
        del bridge_key
        self.reserve_calls += 1
        return MacroObservationCursorReservation(
            bridge_key=BRIDGE_KEY,
            request_id=str(request_id),
            cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
        )

    def list_available_after(self, cursor: MacroObservationCursor, limit: int):
        self.listed_after.append(cursor)
        items = [envelope for item_cursor, envelope in self.envelopes if item_cursor.ordinal > cursor.ordinal]
        return tuple(items[:limit])

    def cursor_for(self, observation_id: str) -> MacroObservationCursor:
        for cursor, envelope in self.envelopes:
            if getattr(envelope, "observation").observation_id == observation_id:
                return cursor
        raise KeyError(observation_id)


def _store(tmp_path: Path) -> WorldGraphStore:
    return WorldGraphStore(tmp_path / "world_model.db", clock=lambda: READY)


def _live_successor() -> tuple[WorldScopeMapping, WorldOntologyRevision, MacroCollectionPlan]:
    config = REPO_ROOT / "config"
    mapping = WorldScopeResolver.load(config / "world_scope_mapping.yaml").mapping
    _, _, revision = derive_market_ontology(mapping)
    bundle = load_world_macro_operator_configs(config_dir=config)
    plan = MacroCollectionPlan.from_registry(bundle.registry)
    spec = MacroGraphBridgeRunSpec(
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
        ontology_revision_id=revision.revision_id,
        ontology_revision_hash=revision.content_sha256,
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )
    assert spec == committed_macro_graph_bridge_successor_spec()
    return mapping, revision, plan


def _persist_v1_contaminated(
    store: WorldGraphStore,
    *,
    mapping: WorldScopeMapping,
    revision: WorldOntologyRevision,
    spec: MacroGraphBridgeRunSpec | None = None,
):
    spec = spec if spec is not None else _v1_spec(mapping, revision)
    registry = store.load(BRIDGE_KEY)
    activated = registry.activate(reservation=_reservation(), spec=spec, expected_version=0)
    store.append_event(activated.events[-1], expected_registry_version=0, fence=None)
    registry = store.load(BRIDGE_KEY)
    last_cursor = registry.active_run.cursor
    owned_ids: list[str] = []
    for index in range(1, 16):
        observation_id, relation = _observes(index, revision)
        asserted = KnowledgeWorldRelationAsserted(relation=relation)
        store.append_knowledge_relation_event(
            asserted,
            fence=registry.fence,
            expected_registry_version=registry.version,
        )
        cursor = _cursor(index, observation_id)
        linked = registry.link_observation(
            observation_id=observation_id,
            relation=relation,
            relation_event_id=asserted.event_id,
            cursor=cursor,
            expected_version=registry.version,
        )
        store.append_event(linked.events[-1], expected_registry_version=registry.version, fence=registry.fence)
        registry = store.load(BRIDGE_KEY)
        last_cursor = cursor
        owned_ids.append(relation.relation_id)
    skip_id = "macro_world_observation:v1:" + _digest("skip:16")
    skip_cursor = _cursor(16, skip_id)
    skipped = registry.skip_observation(
        observation_id=skip_id,
        reason="scope_not_registered",
        cursor=skip_cursor,
        expected_version=registry.version,
    )
    store.append_event(skipped.events[-1], expected_registry_version=registry.version, fence=registry.fence)
    registry = store.load(BRIDGE_KEY)
    advanced = registry.advance_cursor(skip_cursor, expected_version=registry.version)
    store.append_event(advanced.events[-1], expected_registry_version=registry.version, fence=registry.fence)
    about = KnowledgeWorldRelationAsserted(
        relation=KnowledgeWorldRelation(
            kind="ABOUT",
            source=KnowledgeArtifactRef(artifact_id="knowledge_artifact:v1:" + _digest("about"), content_sha256="f" * 64),
            target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"),
            effective_from=CUTOFF,
            ontology_revision=revision.revision_id,
            source_refs=("artifact:unrelated",),
        )
    )
    store.append_knowledge_relation_event(about)
    return store.load(BRIDGE_KEY), owned_ids, about.relation.relation_id, last_cursor, skip_cursor


def _use_case(
    store: WorldGraphStore,
    scan: _Scan,
    mapping: WorldScopeMapping,
    plan: MacroCollectionPlan,
    *,
    structural_revision: WorldOntologyRevision | None = None,
):
    return RegisterMacroObservationKnowledge(
        scan=scan,
        graph=store,
        bridge=store,
        scope_mapping=mapping,
        structural_revision=structural_revision if structural_revision is not None else _revision(mapping),
        collection_plan=plan,
        bridge_key=BRIDGE_KEY,
    )


def test_cold_start_then_steady_restart_does_not_reserve_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scan = _Scan()
    mapping = _mapping()
    use_case = _use_case(store, scan, mapping, _plan())
    first = use_case.ensure(REQUEST_ID)
    assert first.active_run is not None
    assert first.active_run.status == "active"
    assert first.active_run.spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2
    assert first.active_run.spec.collection_plan_id == _plan().plan_id
    assert first.active_run.spec.producer_version == MACRO_PRODUCER_VERSION
    assert scan.reserve_calls == 1
    store.close()
    restarted = _store(tmp_path)
    scan_again = _Scan()
    again = _use_case(restarted, scan_again, mapping, _plan()).ensure(REQUEST_ID)
    assert again.events == first.events
    assert scan_again.reserve_calls == 0
    restarted.close()


def test_persisted_v1_parses_and_unknown_drift_stays_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mapping = _mapping()
    revision = _revision(mapping)
    registry, _owned, _about, _last, skip_cursor = _persist_v1_contaminated(store, mapping=mapping, revision=revision)
    payload = registry.events[0].to_dict()
    assert payload["spec"]["schema_version"] == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1
    assert "collection_plan_id" not in payload["spec"]
    assert "producer_version" not in payload["spec"]
    assert parse_macro_graph_bridge_event(payload).spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1
    assert registry.active_run.cursor == skip_cursor
    scan = _Scan()
    with pytest.raises(UnknownMacroGraphBridgeDrift, match="unknown_config_drift"):
        _use_case(store, scan, mapping, _plan()).ensure(REQUEST_ID)
    blocked = store.load(BRIDGE_KEY)
    assert blocked.active_run.status == "blocked"
    assert blocked.active_run.cursor == skip_cursor
    assert scan.reserve_calls == 0
    store.close()


def test_crash_retry_retires_only_owned_observes_then_handoffs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mapping = _mapping()
    revision = _revision(mapping)
    successor_mapping, successor_revision, successor_plan = _live_successor()
    successor_spec = committed_macro_graph_bridge_successor_spec()
    persisted, owned_ids, about_id, _last, skip_cursor = _persist_v1_contaminated(
        store,
        mapping=mapping,
        revision=revision,
        spec=committed_macro_graph_bridge_predecessor_spec(),
    )
    scan = _Scan()
    use_case = _use_case(
        store,
        scan,
        successor_mapping,
        successor_plan,
        structural_revision=successor_revision,
    )
    original = store.append_knowledge_relation_event
    seen = {"retired": 0}

    def crash_after_four(event, fence=None, expected_registry_version=None):
        parsed = event if isinstance(event, (KnowledgeWorldRelationAsserted, KnowledgeWorldRelationRetired)) else event
        if isinstance(parsed, KnowledgeWorldRelationRetired):
            seen["retired"] += 1
            if seen["retired"] > 4:
                raise OSError("mid-remediation crash")
        return original(event, fence=fence, expected_registry_version=expected_registry_version)

    store.append_knowledge_relation_event = crash_after_four  # type: ignore[method-assign]
    with pytest.raises(OSError, match="mid-remediation crash"):
        use_case.ensure(REQUEST_ID)
    blocked = store.load(BRIDGE_KEY)
    assert blocked.active_run.status == "blocked"
    assert blocked.active_run.cursor == skip_cursor
    store.append_knowledge_relation_event = original  # type: ignore[method-assign]
    handed = use_case.ensure(REQUEST_ID)
    assert handed.active_run is not None
    assert handed.active_run.status == "active"
    assert handed.active_run.epoch == 2
    assert handed.active_run.cursor == skip_cursor
    assert handed.active_run.spec == successor_spec
    assert handed.active_run.cursor.ordinal == 16
    assert not any(
        isinstance(event, MacroGraphObservationLinked) and event.run_id == handed.active_run.run_id
        for event in handed.events
    )
    events = [envelope.event for envelope in store.list_knowledge_relation_events_available_through(LATER)]
    retired = [event for event in events if isinstance(event, KnowledgeWorldRelationRetired)]
    asserted = [event for event in events if isinstance(event, KnowledgeWorldRelationAsserted)]
    assert {event.relation_id for event in retired} == set(owned_ids)
    assert len(retired) == 15
    assert about_id in {event.relation.relation_id for event in asserted if event.relation.kind == "ABOUT"}
    assert about_id not in {event.relation_id for event in retired}
    about_event = next(event for event in asserted if event.relation.relation_id == about_id)
    assert store.get_knowledge_relation_event(about_event.event_id) == about_event
    retried = use_case.ensure(REQUEST_ID)
    assert retried.events == handed.events
    assert scan.reserve_calls == 0
    replayed_first = parse_macro_graph_bridge_event(persisted.events[0].to_dict())
    assert replayed_first.spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1
    assert replayed_first.spec == committed_macro_graph_bridge_predecessor_spec()
    store.close()


def test_handoff_inherits_ordinal_16_and_reconcile_consumes_only_17(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mapping = _mapping()
    revision = _revision(mapping)
    successor_mapping, successor_revision, successor_plan = _live_successor()
    _persisted, owned_ids, about_id, _last, skip_cursor = _persist_v1_contaminated(
        store,
        mapping=mapping,
        revision=revision,
        spec=committed_macro_graph_bridge_predecessor_spec(),
    )
    scan = _Scan()
    use_case = _use_case(
        store,
        scan,
        successor_mapping,
        successor_plan,
        structural_revision=successor_revision,
    )
    handed = use_case.ensure(REQUEST_ID)
    assert handed.active_run is not None
    assert handed.active_run.status == "active"
    assert handed.active_run.epoch == 2
    assert handed.active_run.cursor == skip_cursor
    assert handed.active_run.cursor.ordinal == 16
    assert handed.active_run.spec == committed_macro_graph_bridge_successor_spec()
    assert not any(
        isinstance(event, MacroGraphObservationLinked) and event.run_id == handed.active_run.run_id
        for event in handed.events
    )

    trap = _envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:TW")))
    newer = _envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:US")))
    scan.envelopes.append((_cursor(5, trap.observation.observation_id), trap))
    scan.envelopes.append((_cursor(17, newer.observation.observation_id), newer))
    scan.listed_after.clear()
    reconciled = use_case.reconcile(limit=32)
    assert scan.listed_after
    assert scan.listed_after[0].ordinal == 16
    assert reconciled.active_run is not None
    assert reconciled.active_run.cursor.ordinal == 17
    successor_links = [
        event
        for event in reconciled.events
        if isinstance(event, MacroGraphObservationLinked) and event.run_id == reconciled.active_run.run_id
    ]
    assert len(successor_links) == 1
    assert successor_links[0].observation_id == newer.observation.observation_id
    assert successor_links[0].cursor.ordinal == 17
    assert trap.observation.observation_id not in {event.observation_id for event in successor_links}
    events = [envelope.event for envelope in store.list_knowledge_relation_events_available_through(LATER)]
    retired = [event for event in events if isinstance(event, KnowledgeWorldRelationRetired)]
    asserted = [event for event in events if isinstance(event, KnowledgeWorldRelationAsserted)]
    assert {event.relation_id for event in retired} == set(owned_ids)
    assert about_id not in {event.relation_id for event in retired}
    new_observes = [
        event
        for event in asserted
        if event.relation.kind == "OBSERVES" and event.relation.relation_id == successor_links[0].relation_id
    ]
    assert len(new_observes) == 1

    store.close()
    restarted = _store(tmp_path)
    scan_again = _Scan()
    scan_again.envelopes = list(scan.envelopes)
    again = _use_case(
        restarted,
        scan_again,
        successor_mapping,
        successor_plan,
        structural_revision=successor_revision,
    )
    ensured = again.ensure(REQUEST_ID)
    assert ensured.events == reconciled.events
    assert scan_again.reserve_calls == 0
    replayed = again.reconcile(limit=32)
    assert replayed.events == reconciled.events
    assert replayed.active_run is not None
    assert replayed.active_run.cursor.ordinal == 17
    replay_links = [
        event
        for event in replayed.events
        if isinstance(event, MacroGraphObservationLinked) and event.run_id == replayed.active_run.run_id
    ]
    assert replay_links == successor_links
    restarted.close()
