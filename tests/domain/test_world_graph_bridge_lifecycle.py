from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_context import EntityRef
from trader.domain.world_feature_contract import (
    MARKET_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
)
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    MacroGraphBridgeRegistry,
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
    MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES,
    MacroGraphBridgeLifecycleDecision,
    UnknownMacroGraphBridgeDrift,
    classify_macro_graph_bridge,
    committed_macro_graph_bridge_spec,
    derive_macro_graph_bridge_spec,
    require_committed_live_bridge_lineage,
    same_bridge_schema_family,
)
from trader.domain.world_ontology_lifecycle import market_ontology_revision_id
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


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64
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


def _revision(mapping: WorldScopeMapping, *, revision_id: str | None = None) -> WorldOntologyRevision:
    if revision_id is None:
        revision_id = (
            market_ontology_revision_id(mapping)
            if mapping.mapping_id == WORLD_SCOPE_MAPPING_ID
            else MARKET_ONTOLOGY_REVISION
        )
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


def _spec(
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
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
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
        spec=spec if spec is not None else _spec(),
        expected_version=0,
    )


def test_lifecycle_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "trader.application" not in source
    assert "def handoff" not in source
    assert "MacroGraphBridgeRunHandedOff" not in source
    assert "macro_graph_bridge_run_handed_off" not in source
    assert "committed_macro_graph_bridge_predecessor" not in source
    assert "committed_macro_graph_bridge_successor" not in source


def test_production_has_no_macro_graph_bridge_handoff_event_or_method() -> None:
    roots = (
        REPO_ROOT / "trader" / "domain",
        REPO_ROOT / "trader" / "application" / "world_model",
        REPO_ROOT / "trader" / "infrastructure",
        REPO_ROOT / "trader" / "runtime",
    )
    forbidden = (
        "MacroGraphBridgeRunHandedOff",
        "macro_graph_bridge_run_handed_off",
        "def handoff(",
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(REPO_ROOT)
            for token in forbidden:
                if token in text:
                    violations.append(f"{relative}: {token}")
    assert violations == []


def test_current_run_spec_binds_collection_plan_identity() -> None:
    mapping = _mapping()
    revision = _revision(mapping)
    plan = _plan()
    spec = _spec(mapping, revision, plan)
    assert spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA == "macro_graph_bridge_run_spec.v1"
    assert spec.collection_plan_id == plan.plan_id
    assert spec.collection_plan_hash == plan.content_sha256
    assert spec.producer_version == MACRO_PRODUCER_VERSION == "world_macro_source.v1"
    payload = spec.to_dict()
    assert payload["collection_plan_id"] == plan.plan_id
    assert payload["collection_plan_hash"] == plan.content_sha256
    assert payload["producer_version"] == MACRO_PRODUCER_VERSION
    assert payload["schema_version"] == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
    assert MacroGraphBridgeRunSpec.from_mapping(payload) == spec
    with pytest.raises((TypeError, ValueError), match="collection_plan"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
        )
    with pytest.raises((TypeError, ValueError), match="producer_version"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
            collection_plan_id=plan.plan_id,
            collection_plan_hash=plan.content_sha256,
        )
    with pytest.raises(ValueError, match="producer_version"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
            collection_plan_id=plan.plan_id,
            collection_plan_hash=plan.content_sha256,
            producer_version="not_admitted",
        )
    with pytest.raises(ValueError, match="schema_version"):
        MacroGraphBridgeRunSpec(
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
            ontology_revision_id=revision.revision_id,
            ontology_revision_hash=revision.content_sha256,
            schema_version="macro_graph_bridge_run_spec.v2",
            collection_plan_id=plan.plan_id,
            collection_plan_hash=plan.content_sha256,
            producer_version=MACRO_PRODUCER_VERSION,
        )
    with pytest.raises(FrozenInstanceError):
        spec.collection_plan_id = "other"  # type: ignore[misc]


def test_persisted_activated_event_round_trips_current_spec() -> None:
    registry = _activated()
    event = registry.events[0]
    payload = event.to_dict()
    assert payload["spec"]["schema_version"] == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
    assert payload["spec"]["collection_plan_id"] == _plan().plan_id
    assert payload["spec"]["producer_version"] == MACRO_PRODUCER_VERSION
    from trader.domain.world_graph import parse_macro_graph_bridge_event

    replay = parse_macro_graph_bridge_event(payload)
    assert replay == event
    assert replay.spec == _spec()


def test_classify_missing_matched_and_unknown_drift() -> None:
    desired = _spec()
    empty = MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
    missing = classify_macro_graph_bridge(empty, desired=desired)
    assert missing.status == "missing"
    assert missing.status in MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES
    assert isinstance(missing, MacroGraphBridgeLifecycleDecision)

    active = _activated(desired)
    matched = classify_macro_graph_bridge(active, desired=desired)
    assert matched.status == "matched_active"
    blocked = active.block(reason="config_drift", expected_version=1)
    matched_blocked = classify_macro_graph_bridge(blocked, desired=desired)
    assert matched_blocked.status == "matched_blocked"

    other = _spec(plan=_plan(digest="c" * 64))
    drifted_active = classify_macro_graph_bridge(active, desired=other)
    assert drifted_active.status == "drifted_active"
    unknown = classify_macro_graph_bridge(blocked, desired=other)
    assert unknown.status == "unknown_drift"
    assert unknown.reason == "unknown_config_drift"
    assert "migration" not in inspect.signature(classify_macro_graph_bridge).parameters


def _flip_hex_bit(digest: str) -> str:
    return f"{int(digest[0], 16) ^ 1:x}{digest[1:]}"


def _flip_identity(value: str) -> str:
    return value[:-1] + chr(ord(value[-1]) ^ 1)


def _replace_run_spec(spec: MacroGraphBridgeRunSpec, **changes: str) -> MacroGraphBridgeRunSpec:
    try:
        return replace(spec, **changes)
    except ValueError:
        instance = object.__new__(MacroGraphBridgeRunSpec)
        for name in spec.__dataclass_fields__:
            object.__setattr__(instance, name, changes[name] if name in changes else getattr(spec, name))
        return instance


def _generation_roll_fields() -> tuple[str, ...]:
    return ("scope_mapping_hash", "ontology_revision_id", "ontology_revision_hash")


def _unknown_drift_fields() -> tuple[str, ...]:
    return ("scope_mapping_id", "collection_plan_id", "collection_plan_hash", "producer_version")


def _flip_spec_field(spec: MacroGraphBridgeRunSpec, field: str) -> MacroGraphBridgeRunSpec:
    value = getattr(spec, field)
    if field.endswith("_hash"):
        flipped = _flip_hex_bit(value)
    elif field == "collection_plan_id":
        prefix, digest = value.rsplit(":", 1)
        flipped = f"{prefix}:{_flip_hex_bit(digest)}"
    else:
        flipped = _flip_identity(value)
    return _replace_run_spec(spec, **{field: flipped})


def _flip_cases(fields: tuple[str, ...]) -> list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]]:
    current = _spec()
    cases: list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]] = []
    for field in fields:
        cases.append(("durable", field, _flip_spec_field(current, field), current))
        cases.append(("desired", field, current, _flip_spec_field(current, field)))
    return cases


def _drifted_committed_specs() -> list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]]:
    return _flip_cases(_unknown_drift_fields())


def _generation_roll_specs() -> list[tuple[str, str, MacroGraphBridgeRunSpec, MacroGraphBridgeRunSpec]]:
    return _flip_cases(_generation_roll_fields())


def test_bridge_spec_is_derived_from_loaded_mapping_and_ontology() -> None:
    mapping = _mapping()
    revision = _revision(mapping)
    plan = _plan()
    spec = derive_macro_graph_bridge_spec(mapping=mapping, ontology=revision, collection_plan=plan)
    assert spec == committed_macro_graph_bridge_spec(mapping=mapping, ontology=revision, collection_plan=plan)
    assert spec.schema_version == MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA
    assert spec.scope_mapping_id == WORLD_SCOPE_MAPPING_ID == mapping.mapping_id
    assert spec.scope_mapping_hash == mapping.content_sha256
    assert spec.ontology_revision_id == market_ontology_revision_id(mapping)
    assert spec.ontology_revision_hash == revision.content_sha256
    assert spec.collection_plan_id == plan.plan_id
    assert spec.collection_plan_hash == plan.content_sha256
    assert spec.producer_version == MACRO_PRODUCER_VERSION == "world_macro_source.v1"
    with pytest.raises(TypeError, match="derived from mapping"):
        committed_macro_graph_bridge_spec()
    with pytest.raises(ValueError, match="committed identity"):
        require_committed_live_bridge_lineage(
            mapping=mapping,
            ontology=revision,
            collection_plan=plan,
        )
    family = _revision(mapping, revision_id=MARKET_ONTOLOGY_REVISION)
    with pytest.raises(ValueError, match="derived from the mapping generation"):
        derive_macro_graph_bridge_spec(mapping=mapping, ontology=family, collection_plan=plan)


def test_blocked_mapping_generation_rolls_and_unknown_plan_stays_closed() -> None:
    current = _spec()
    rolled = _flip_spec_field(current, "scope_mapping_hash")
    assert same_bridge_schema_family(current, rolled)
    blocked = _activated(current).block(reason="config_drift", expected_version=1)
    decision = classify_macro_graph_bridge(blocked, desired=rolled)
    assert decision.status == "roll_generation"
    other_plan = _spec(plan=_plan(digest="c" * 64))
    unknown = classify_macro_graph_bridge(blocked, desired=other_plan)
    assert unknown.status == "unknown_drift"
    active = classify_macro_graph_bridge(_activated(current), desired=other_plan)
    assert active.status == "drifted_active"


@pytest.mark.parametrize("side,field,durable,desired", _generation_roll_specs())
def test_blocked_generation_hash_rolls(
    side: str,
    field: str,
    durable: MacroGraphBridgeRunSpec,
    desired: MacroGraphBridgeRunSpec,
) -> None:
    del side, field
    blocked = _activated(durable).block(reason="config_drift", expected_version=1)
    decision = classify_macro_graph_bridge(blocked, desired=desired)
    assert decision.status == "roll_generation"
    assert blocked.active_run is not None
    assert blocked.active_run.status == "blocked"
    assert not any(event.event_type == "macro_graph_bridge_run_handed_off" for event in blocked.events)


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
    assert blocked.active_run is not None
    assert blocked.active_run.status == "blocked"
    assert not any(event.event_type == "macro_graph_bridge_run_handed_off" for event in blocked.events)


def test_unknown_drift_error_is_auditable() -> None:
    error = UnknownMacroGraphBridgeDrift("unknown_config_drift")
    assert error.code == "unknown_config_drift"
    assert "unknown_config_drift" in str(error)
