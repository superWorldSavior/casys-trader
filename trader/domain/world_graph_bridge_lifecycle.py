"""Typed macro-graph bridge lifecycle: classify durable state before reservation.

Append-only. No wildcard drift repair. Shadow overlay only — never a Trader
authority or a causal claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationEvent,
    KnowledgeWorldRelationRetired,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRun,
    MacroGraphBridgeRunSpec,
    MacroGraphObservationLinked,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    WORLD_MACRO_COLLECTION_PLAN_ID,
    WORLD_MACRO_COLLECTION_PLAN_SHA256,
)


MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES = frozenset(
    {
        "missing",
        "matched_active",
        "matched_blocked",
        "drifted_active",
        "drifted_blocked_admitted",
        "unknown_drift",
    }
)
COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC = MacroGraphBridgeRunSpec(
    scope_mapping_id=WORLD_SCOPE_MAPPING_PREDECESSOR_ID,
    scope_mapping_hash=WORLD_SCOPE_MAPPING_PREDECESSOR_SHA256,
    ontology_revision_id=WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_REVISION,
    ontology_revision_hash=WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_SHA256,
)
COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC = MacroGraphBridgeRunSpec(
    scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
    scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
    ontology_revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    ontology_revision_hash=WORLD_GRAPH_V3_ONTOLOGY_SHA256,
    schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
    collection_plan_id=WORLD_MACRO_COLLECTION_PLAN_ID,
    collection_plan_hash=WORLD_MACRO_COLLECTION_PLAN_SHA256,
    producer_version=MACRO_PRODUCER_VERSION,
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


class UnknownMacroGraphBridgeDrift(ValueError):
    """Fail-closed drift that is not an admitted predecessor/successor migration."""

    code = "unknown_config_drift"

    def __init__(self, reason: str, *, context: Mapping[str, Any] | None = None) -> None:
        resolved = _required_text(reason, "reason")
        super().__init__(resolved)
        self.reason = resolved
        self.context = dict(context or ())


@dataclass(frozen=True)
class MacroGraphBridgeMigration:
    """Exact committed predecessor spec plus exact desired successor spec."""

    predecessor_spec: MacroGraphBridgeRunSpec | Mapping[str, Any]
    successor_spec: MacroGraphBridgeRunSpec | Mapping[str, Any]

    def __post_init__(self) -> None:
        predecessor = MacroGraphBridgeRunSpec.from_mapping(self.predecessor_spec)
        successor = MacroGraphBridgeRunSpec.from_mapping(self.successor_spec)
        if predecessor == successor:
            raise ValueError("successor_spec must differ from predecessor_spec")
        object.__setattr__(self, "predecessor_spec", predecessor)
        object.__setattr__(self, "successor_spec", successor)

    def admits(self, *, durable: MacroGraphBridgeRunSpec, desired: MacroGraphBridgeRunSpec) -> bool:
        if not isinstance(durable, MacroGraphBridgeRunSpec):
            raise TypeError("durable must be MacroGraphBridgeRunSpec")
        if not isinstance(desired, MacroGraphBridgeRunSpec):
            raise TypeError("desired must be MacroGraphBridgeRunSpec")
        return durable == self.predecessor_spec and desired == self.successor_spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "predecessor_spec": self.predecessor_spec.to_dict(),
            "successor_spec": self.successor_spec.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeMigration) -> MacroGraphBridgeMigration:
        if isinstance(value, MacroGraphBridgeMigration):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("migration must be MacroGraphBridgeMigration or a mapping")
        return cls(predecessor_spec=value.get("predecessor_spec"), successor_spec=value.get("successor_spec"))


COMMITTED_MACRO_GRAPH_BRIDGE_MIGRATION = MacroGraphBridgeMigration(
    predecessor_spec=COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC,
    successor_spec=COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC,
)


def committed_macro_graph_bridge_predecessor_spec() -> MacroGraphBridgeRunSpec:
    return COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC


def committed_macro_graph_bridge_successor_spec() -> MacroGraphBridgeRunSpec:
    return COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC


@dataclass(frozen=True)
class MacroGraphBridgeLifecycleDecision:
    status: str
    reason: str
    run: MacroGraphBridgeRun | None = None
    migration: MacroGraphBridgeMigration | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES:
            allowed = ", ".join(sorted(MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES))
            raise ValueError(f"lifecycle status must be one of: {allowed}")
        if self.run is not None and not isinstance(self.run, MacroGraphBridgeRun):
            raise TypeError("run must be MacroGraphBridgeRun or None")
        if self.migration is not None and not isinstance(self.migration, MacroGraphBridgeMigration):
            raise TypeError("migration must be MacroGraphBridgeMigration or None")
        if status == "drifted_blocked_admitted" and self.migration is None:
            raise ValueError("admitted drift requires a migration")
        if status != "drifted_blocked_admitted" and self.migration is not None:
            raise ValueError("migration is only valid on admitted blocked drift")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))


def committed_macro_graph_bridge_migration() -> MacroGraphBridgeMigration:
    """Admit only the frozen live v1 → v2 lineage. Callers cannot inject another pair."""

    return COMMITTED_MACRO_GRAPH_BRIDGE_MIGRATION


def classify_macro_graph_bridge(
    registry: MacroGraphBridgeRegistry,
    *,
    desired: MacroGraphBridgeRunSpec,
) -> MacroGraphBridgeLifecycleDecision:
    """Load-time classification. Never reserves a cursor."""

    if not isinstance(registry, MacroGraphBridgeRegistry):
        raise TypeError("registry must be MacroGraphBridgeRegistry")
    if not isinstance(desired, MacroGraphBridgeRunSpec):
        raise TypeError("desired must be MacroGraphBridgeRunSpec")
    migration = committed_macro_graph_bridge_migration()
    run = registry.active_run
    if run is None:
        return MacroGraphBridgeLifecycleDecision(status="missing", reason="no_active_generation")
    if run.spec == desired:
        if run.status == "blocked":
            return MacroGraphBridgeLifecycleDecision(status="matched_blocked", reason="spec_matches_blocked", run=run)
        return MacroGraphBridgeLifecycleDecision(status="matched_active", reason="spec_matches", run=run)
    if run.status == "active":
        return MacroGraphBridgeLifecycleDecision(status="drifted_active", reason="config_drift", run=run)
    if migration.admits(durable=run.spec, desired=desired):
        return MacroGraphBridgeLifecycleDecision(
            status="drifted_blocked_admitted",
            reason="admitted_predecessor_successor_migration",
            run=run,
            migration=migration,
        )
    return MacroGraphBridgeLifecycleDecision(status="unknown_drift", reason="unknown_config_drift", run=run)


def predecessor_owned_observes(
    registry: MacroGraphBridgeRegistry,
    run_id: str,
) -> tuple[MacroGraphObservationLinked, ...]:
    if not isinstance(registry, MacroGraphBridgeRegistry):
        raise TypeError("registry must be MacroGraphBridgeRegistry")
    identity = _required_text(run_id, "run_id")
    return tuple(
        event
        for event in registry.events
        if isinstance(event, MacroGraphObservationLinked) and event.run_id == identity
    )


def observes_retirement(
    *,
    relation: KnowledgeWorldRelation,
    linked: MacroGraphObservationLinked,
    run_id: str,
) -> KnowledgeWorldRelationRetired:
    if not isinstance(relation, KnowledgeWorldRelation):
        raise TypeError("relation must be KnowledgeWorldRelation")
    if not isinstance(linked, MacroGraphObservationLinked):
        raise TypeError("linked must be MacroGraphObservationLinked")
    if relation.kind != "OBSERVES":
        raise ValueError("retirement is limited to OBSERVES knowledge relations")
    if relation.relation_id != linked.relation_id:
        raise ValueError("linked relation_id does not match the asserted relation")
    identity = _required_text(run_id, "run_id")
    if linked.run_id != identity:
        raise ValueError("linked event is not owned by the predecessor run")
    return KnowledgeWorldRelationRetired(
        relation_id=relation.relation_id,
        retired_at=relation.effective_from + timedelta(microseconds=1),
        source_refs=(
            f"{identity}/{linked.event_id}",
            f"{linked.observation_id}/{linked.relation_event_id}",
        ),
    )


def remaining_owned_observes(
    links: Sequence[MacroGraphObservationLinked],
    knowledge_events: Sequence[KnowledgeWorldRelationEvent | Mapping[str, Any]],
) -> tuple[MacroGraphObservationLinked, ...]:
    retired_ids = {
        event.relation_id
        for event in knowledge_events
        if isinstance(event, KnowledgeWorldRelationRetired)
    }
    return tuple(link for link in links if link.relation_id not in retired_ids)


__all__ = [
    "COMMITTED_MACRO_GRAPH_BRIDGE_MIGRATION",
    "COMMITTED_MACRO_GRAPH_BRIDGE_PREDECESSOR_SPEC",
    "COMMITTED_MACRO_GRAPH_BRIDGE_SUCCESSOR_SPEC",
    "MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES",
    "MacroGraphBridgeLifecycleDecision",
    "MacroGraphBridgeMigration",
    "UnknownMacroGraphBridgeDrift",
    "classify_macro_graph_bridge",
    "committed_macro_graph_bridge_migration",
    "committed_macro_graph_bridge_predecessor_spec",
    "committed_macro_graph_bridge_successor_spec",
    "observes_retirement",
    "predecessor_owned_observes",
    "remaining_owned_observes",
]
