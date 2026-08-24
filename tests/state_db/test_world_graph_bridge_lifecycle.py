"""Real-store lifecycle: current spec, restart, fail-closed drift."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge
from trader.application.world_model.ontology_bootstrap import derive_market_ontology
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_context import EntityRef
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    MacroGraphBridgeRunSpec,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
    StructuralWorldRelation,
)
from trader.domain.world_graph_bridge_lifecycle import (
    UnknownMacroGraphBridgeDrift,
    committed_macro_graph_bridge_spec,
    require_committed_live_bridge_lineage,
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
READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64


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
        context_ref=EntityRef(kind="instrument", entity_id="2330"),
        graph_ref=entity,
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
        registry_version="world_macro_sources.v1",
        registry_content_sha256=digest,
        targets=(
            MacroCollectionTarget(
                scope=MacroScope(kind="venue", entity_id="mic:XTAI"),
                source_ids=("fed_policy_rate",),
            ),
        ),
    )


class _Scan:
    def __init__(self) -> None:
        self.reserve_calls = 0

    def reserve_activation_cursor(self, bridge_key, request_id) -> MacroObservationCursorReservation:
        del bridge_key
        self.reserve_calls += 1
        return MacroObservationCursorReservation(
            bridge_key=BRIDGE_KEY,
            request_id=str(request_id),
            cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
        )

    def list_available_after(self, cursor: MacroObservationCursor, limit: int):
        del cursor, limit
        return ()

    def cursor_for(self, observation_id: str) -> MacroObservationCursor:
        raise KeyError(observation_id)


def _store(tmp_path: Path) -> WorldGraphStore:
    return WorldGraphStore(tmp_path / "world_model.db", clock=lambda: READY)


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
    assert first.active_run.spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
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


def test_persisted_different_spec_is_unknown_drift_and_stays_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mapping = _mapping()
    scan = _Scan()
    first = _use_case(store, scan, mapping, _plan()).ensure(REQUEST_ID)
    assert first.active_run is not None
    drifted = _use_case(store, _Scan(), mapping, _plan(digest="c" * 64))
    with pytest.raises(UnknownMacroGraphBridgeDrift, match="unknown_config_drift"):
        drifted.ensure(REQUEST_ID)
    blocked = store.load(BRIDGE_KEY)
    assert blocked.active_run is not None
    assert blocked.active_run.status == "blocked"
    assert blocked.active_run.block_reason == "config_drift"
    assert blocked.active_run.spec == first.active_run.spec
    assert not any(event.event_type == "macro_graph_bridge_run_handed_off" for event in blocked.events)
    store.close()


def test_live_yaml_derives_committed_bridge_spec() -> None:
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
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )
    assert spec == committed_macro_graph_bridge_spec(
        mapping=mapping,
        ontology=revision,
        collection_plan=plan,
    )
    assert (
        require_committed_live_bridge_lineage(
            mapping=mapping,
            ontology=revision,
            collection_plan=plan,
        )
        == spec
    )
