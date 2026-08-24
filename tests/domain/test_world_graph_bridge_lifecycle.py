from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_context import EntityRef
from trader.domain.world_feature_contract import (
    WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_REVISION,
    WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_SHA256,
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_GRAPH_V3_ONTOLOGY_SHA256,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_PREDECESSOR_ID,
    WORLD_SCOPE_MAPPING_PREDECESSOR_SHA256,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1,
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRunSpec,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
    StructuralWorldRelation,
    fold_knowledge_relation_events_at_cutoff,
    parse_macro_graph_bridge_event,
)
from trader.domain.world_graph_bridge_lifecycle import (
    COMMITTED_MACRO_GRAPH_BRIDGE_MIGRATION,
    COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC,
    COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC,
    MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES,
    MacroGraphBridgeLifecycleDecision,
    MacroGraphBridgeMigration,
    UnknownMacroGraphBridgeDrift,
    classify_macro_graph_bridge,
    committed_macro_graph_bridge_migration,
    committed_macro_graph_bridge_predecessor_spec,
    committed_macro_graph_bridge_successor_spec,
    observes_retirement,
    predecessor_owned_observes,
    remaining_owned_observes,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_PRODUCER_VERSION_V1,
    WORLD_MACRO_COLLECTION_PLAN_ID,
    WORLD_MACRO_COLLECTION_PLAN_SHA256,
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


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64
SHA = "a" * 64
MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_graph_bridge_lifecycle.py"


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


def _v1_spec(
    mapping: WorldScopeMapping | None = None,
    revision: WorldOntologyRevision | None = None,
) -> MacroGraphBridgeRunSpec:
    resolved_mapping = mapping if mapping is not None else _mapping()
    resolved_revision = revision if revision is not None else _revision(resolved_mapping)
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=resolved_mapping.mapping_id,
        scope_mapping_hash=resolved_mapping.content_sha256,
        ontology_revision_id=resolved_revision.revision_id,
        ontology_revision_hash=resolved_revision.content_sha256,
    )


def _v2_spec(
    mapping: WorldScopeMapping | None = None,
    revision: WorldOntologyRevision | None = None,
    plan: MacroCollectionPlan | None = None,
) -> MacroGraphBridgeRunSpec:
    resolved_mapping = mapping if mapping is not None else _mapping()
    resolved_revision = revision if revision is not None else _revision(resolved_mapping)
    resolved_plan = plan if plan is not None else _plan()
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=resolved_mapping.mapping_id,
        scope_mapping_hash=resolved_mapping.content_sha256,
        ontology_revision_id=resolved_revision.revision_id,
        ontology_revision_hash=resolved_revision.content_sha256,
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
        collection_plan_id=resolved_plan.plan_id,
        collection_plan_hash=resolved_plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )


def _reservation() -> MacroObservationCursorReservation:
    return MacroObservationCursorReservation(
        bridge_key=BRIDGE_KEY,
        request_id=REQUEST_ID,
        cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
    )


def _activated(spec: MacroGraphBridgeRunSpec | None = None) -> MacroGraphBridgeRegistry:
    return MacroGraphBridgeRegistry.empty(BRIDGE_KEY).activate(
        reservation=_reservation(),
        spec=spec if spec is not None else _v1_spec(),
        expected_version=0,
    )


def test_lifecycle_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "trader.application" not in source


def test_v1_run_spec_still_parses_and_omits_collection_plan() -> None:
    spec = _v1_spec()
    assert spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1 == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
    payload = spec.to_dict()
    assert "collection_plan_id" not in payload
    assert "collection_plan_hash" not in payload
    assert "producer_version" not in payload
    replayed = MacroGraphBridgeRunSpec.from_mapping(payload)
    assert replayed == spec
    assert replayed.collection_plan_id is None
    assert replayed.collection_plan_hash is None
    assert replayed.producer_version is None
    with pytest.raises(ValueError, match="producer"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=spec.scope_mapping_id,
            scope_mapping_hash=spec.scope_mapping_hash,
            ontology_revision_id=spec.ontology_revision_id,
            ontology_revision_hash=spec.ontology_revision_hash,
            producer_version=MACRO_PRODUCER_VERSION,
        )


def test_v2_run_spec_binds_collection_plan_identity_and_differs_from_v1() -> None:
    mapping = _mapping()
    revision = _revision(mapping)
    plan = _plan()
    v1 = _v1_spec(mapping, revision)
    v2 = _v2_spec(mapping, revision, plan)
    assert v2.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2
    assert v2.collection_plan_id == plan.plan_id
    assert v2.collection_plan_hash == plan.content_sha256
    assert v2.producer_version == MACRO_PRODUCER_VERSION
    assert v1 != v2
    assert v1.scope_mapping_hash == v2.scope_mapping_hash
    assert v1.ontology_revision_hash == v2.ontology_revision_hash
    payload = v2.to_dict()
    assert payload["collection_plan_id"] == plan.plan_id
    assert payload["collection_plan_hash"] == plan.content_sha256
    assert payload["producer_version"] == MACRO_PRODUCER_VERSION
    assert MacroGraphBridgeRunSpec.from_mapping(payload) == v2
    with pytest.raises(ValueError, match="collection_plan"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
        )
    with pytest.raises(ValueError, match="producer_version"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
            collection_plan_id=plan.plan_id,
            collection_plan_hash=plan.content_sha256,
        )
    with pytest.raises(ValueError, match="producer_version"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
            collection_plan_id=plan.plan_id,
            collection_plan_hash=plan.content_sha256,
            producer_version=MACRO_PRODUCER_VERSION_V1,
        )
    with pytest.raises(FrozenInstanceError):
        v2.collection_plan_id = "other"  # type: ignore[misc]


def test_persisted_v1_activated_event_still_parses() -> None:
    registry = _activated(_v1_spec())
    event = registry.events[0]
    payload = event.to_dict()
    assert payload["spec"]["schema_version"] == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1
    assert "collection_plan_id" not in payload["spec"]
    assert "producer_version" not in payload["spec"]
    replay = parse_macro_graph_bridge_event(payload)
    assert replay == event
    assert replay.spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1


def test_classify_missing_matched_and_unknown_drift() -> None:
    desired = _v2_spec()
    empty = MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
    missing = classify_macro_graph_bridge(empty, desired=desired)
    assert missing.status == "missing"
    assert missing.status in MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES
    assert isinstance(missing, MacroGraphBridgeLifecycleDecision)

    v1 = _v1_spec()
    active = _activated(v1)
    matched = classify_macro_graph_bridge(active, desired=v1)
    assert matched.status == "matched_active"
    blocked = active.block(reason="config_drift", expected_version=1)
    matched_blocked = classify_macro_graph_bridge(blocked, desired=v1)
    assert matched_blocked.status == "matched_blocked"

    drifted_active = classify_macro_graph_bridge(active, desired=desired)
    assert drifted_active.status == "drifted_active"
    unknown = classify_macro_graph_bridge(blocked, desired=desired)
    assert unknown.status == "unknown_drift"
    assert "unknown" in unknown.reason or "drift" in unknown.reason


def test_classify_admits_only_exact_predecessor_successor_migration() -> None:
    predecessor = _v1_spec()
    successor = _v2_spec()
    other = _v2_spec(plan=_plan(digest="c" * 64))
    migration = MacroGraphBridgeMigration(predecessor_spec=predecessor, successor_spec=successor)
    assert migration.admits(durable=predecessor, desired=successor)
    assert not migration.admits(durable=predecessor, desired=other)
    blocked = _activated(predecessor).block(reason="config_drift", expected_version=1)
    rejected = classify_macro_graph_bridge(blocked, desired=successor)
    assert rejected.status == "unknown_drift"
    with pytest.raises(ValueError, match="differ|successor"):
        MacroGraphBridgeMigration(predecessor_spec=predecessor, successor_spec=predecessor)
    assert "migration" not in inspect.signature(classify_macro_graph_bridge).parameters


def _flip_hex_bit(digest: str) -> str:
    return f"{int(digest[0], 16) ^ 1:x}{digest[1:]}"


def _flip_identity(value: str) -> str:
    return value[:-1] + chr(ord(value[-1]) ^ 1)


def _replace_run_spec(spec: MacroGraphBridgeRunSpec, **changes: str) -> MacroGraphBridgeRunSpec:
    # v2 construction admits only MACRO_PRODUCER_VERSION; one-bit producer drift bypasses it.
    try:
        return replace(spec, **changes)
    except ValueError:
        instance = object.__new__(MacroGraphBridgeRunSpec)
        for name in spec.__dataclass_fields__:
            object.__setattr__(instance, name, changes[name] if name in changes else getattr(spec, name))
        return instance


def _drifted_committed_specs() -> list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]]:
    predecessor = committed_macro_graph_bridge_predecessor_spec()
    successor = committed_macro_graph_bridge_successor_spec()
    cases: list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]] = []
    for field in ("scope_mapping_id", "scope_mapping_hash", "ontology_revision_id", "ontology_revision_hash"):
        value = getattr(predecessor, field)
        flipped = _flip_hex_bit(value) if field.endswith("_hash") else _flip_identity(value)
        cases.append(("predecessor", field, _replace_run_spec(predecessor, **{field: flipped}), successor))
    for field in (
        "scope_mapping_id",
        "scope_mapping_hash",
        "ontology_revision_id",
        "ontology_revision_hash",
        "collection_plan_id",
        "collection_plan_hash",
        "producer_version",
    ):
        value = getattr(successor, field)
        if field.endswith("_hash"):
            flipped = _flip_hex_bit(value)
        elif field == "collection_plan_id":
            prefix, digest = value.rsplit(":", 1)
            flipped = f"{prefix}:{_flip_hex_bit(digest)}"
        else:
            flipped = _flip_identity(value)
        cases.append(("successor", field, predecessor, _replace_run_spec(successor, **{field: flipped})))
    return cases


def test_committed_migration_is_the_frozen_live_lineage() -> None:
    assert inspect.signature(committed_macro_graph_bridge_migration).parameters == {}
    predecessor = committed_macro_graph_bridge_predecessor_spec()
    successor = committed_macro_graph_bridge_successor_spec()
    migration = committed_macro_graph_bridge_migration()
    assert predecessor == COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC
    assert successor == COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC
    assert migration == COMMITTED_MACRO_GRAPH_BRIDGE_MIGRATION
    assert predecessor.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V1
    assert predecessor.scope_mapping_id == WORLD_SCOPE_MAPPING_PREDECESSOR_ID == "world_scope_mapping.v1"
    assert predecessor.scope_mapping_hash == WORLD_SCOPE_MAPPING_PREDECESSOR_SHA256
    assert predecessor.ontology_revision_id == WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_REVISION == "market_ontology.v1"
    assert predecessor.ontology_revision_hash == WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_SHA256
    assert predecessor.collection_plan_id is None
    assert predecessor.collection_plan_hash is None
    assert predecessor.producer_version is None
    assert successor.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2
    assert successor.scope_mapping_id == WORLD_SCOPE_MAPPING_ID == "world_scope_mapping.v2"
    assert successor.scope_mapping_hash == WORLD_SCOPE_MAPPING_SHA256
    assert successor.ontology_revision_id == WORLD_GRAPH_V3_ONTOLOGY_REVISION == "market_ontology.v2"
    assert successor.ontology_revision_hash == WORLD_GRAPH_V3_ONTOLOGY_SHA256
    assert successor.collection_plan_id == WORLD_MACRO_COLLECTION_PLAN_ID
    assert successor.collection_plan_hash == WORLD_MACRO_COLLECTION_PLAN_SHA256
    assert successor.collection_plan_id == f"macro_collection_plan:v1:{WORLD_MACRO_COLLECTION_PLAN_SHA256}"
    assert successor.producer_version == MACRO_PRODUCER_VERSION == "macro_source_only.v2"
    assert migration.predecessor_spec == predecessor
    assert migration.successor_spec == successor
    with pytest.raises(TypeError):
        committed_macro_graph_bridge_migration(predecessor_spec=predecessor, successor_spec=successor)


def test_classify_admits_only_the_committed_live_lineage() -> None:
    predecessor = committed_macro_graph_bridge_predecessor_spec()
    successor = committed_macro_graph_bridge_successor_spec()
    blocked = _activated(predecessor).block(reason="config_drift", expected_version=1)
    admitted = classify_macro_graph_bridge(blocked, desired=successor)
    assert admitted.status == "drifted_blocked_admitted"
    assert admitted.migration == committed_macro_graph_bridge_migration()
    rejected = classify_macro_graph_bridge(blocked, desired=_v2_spec())
    assert rejected.status == "unknown_drift"


@pytest.mark.parametrize("side,field,durable,desired", _drifted_committed_specs())
def test_one_bit_identity_drift_stays_unknown(
    side: str,
    field: str,
    durable: MacroGraphBridgeRunSpec,
    desired: MacroGraphBridgeRunSpec,
) -> None:
    del side, field
    blocked = _activated(durable).block(reason="config_drift", expected_version=1)
    decision = classify_macro_graph_bridge(blocked, desired=desired)
    assert decision.status == "unknown_drift"
    assert decision.migration is None
    assert blocked.active_run is not None
    assert blocked.active_run.status == "blocked"
    assert not any(event.event_type == "macro_graph_bridge_run_handed_off" for event in blocked.events)


def test_owned_observes_and_deterministic_retirement_are_retry_idempotent() -> None:
    mapping = _mapping()
    revision = _revision(mapping)
    spec = _v1_spec(mapping, revision)
    registry = _activated(spec)
    observation_id = "macro_world_observation:v1:" + "1" * 64
    relation = KnowledgeWorldRelation(
        kind="OBSERVES",
        source=WorldObservationRef(observation_id="world_observation:v1:" + "2" * 64),
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"),
        effective_from=CUTOFF,
        effective_until=VALID_UNTIL,
        ontology_revision=revision.revision_id,
        source_refs=(f"{observation_id}/{'e' * 64}",),
    )
    asserted = KnowledgeWorldRelationAsserted(relation=relation)
    cursor = MacroObservationCursor(
        receipt_log_generation=1,
        ordinal=1,
        receipt_id="world-availability-receipt:v1:" + "3" * 64,
        observation_id=observation_id,
    )
    linked = registry.link_observation(
        observation_id=observation_id,
        relation=relation,
        relation_event_id=asserted.event_id,
        cursor=cursor,
        expected_version=1,
    )
    owned = predecessor_owned_observes(linked, linked.active_run.run_id)
    assert len(owned) == 1
    assert owned[0].relation_id == relation.relation_id
    first = observes_retirement(relation=relation, linked=owned[0], run_id=linked.active_run.run_id)
    second = observes_retirement(relation=relation, linked=owned[0], run_id=linked.active_run.run_id)
    assert first == second
    assert first.event_id == second.event_id
    assert first.relation_id == relation.relation_id
    assert first.retired_at > relation.effective_from
    folded = fold_knowledge_relation_events_at_cutoff(
        (asserted, first),
        cutoff_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    assert folded == ()
    remaining = remaining_owned_observes(owned, (asserted,))
    assert remaining == owned
    assert remaining_owned_observes(owned, (asserted, first)) == ()
    about = KnowledgeWorldRelation(
        kind="ABOUT",
        source=KnowledgeArtifactRef(
            artifact_id="knowledge_artifact:v1:" + "4" * 64,
            content_sha256="f" * 64,
        ),
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAI"),
        effective_from=CUTOFF,
        ontology_revision=revision.revision_id,
        source_refs=("artifact:proof",),
    )
    with pytest.raises(ValueError, match="OBSERVES"):
        observes_retirement(relation=about, linked=owned[0], run_id=linked.active_run.run_id)


def test_unknown_drift_error_is_auditable() -> None:
    error = UnknownMacroGraphBridgeDrift("unknown_config_drift")
    assert error.code == "unknown_config_drift"
    assert "unknown_config_drift" in str(error)
