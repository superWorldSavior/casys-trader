"""Typed macro-graph bridge lifecycle: classify durable state before reservation.

Append-only. Mapping/ontology hash changes of the same schema family roll to a
new generation. Collection-plan or producer identity drift stays unknown.
Shadow overlay only — never a Trader authority or a causal claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.world_feature_contract import WORLD_SCOPE_MAPPING_ID
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRun,
    MacroGraphBridgeRunSpec,
    WorldOntologyRevision,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MacroCollectionPlan,
    require_committed_macro_collection_plan,
)
from trader.domain.world_ontology_lifecycle import admits_market_ontology_family, market_ontology_revision_id
from trader.domain.world_scope import WorldScopeMapping


MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES = frozenset(
    {
        "missing",
        "matched_active",
        "matched_blocked",
        "drifted_active",
        "roll_generation",
        "unknown_drift",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


class UnknownMacroGraphBridgeDrift(ValueError):
    """Fail-closed drift. No admitted lineage migration exists."""

    code = "unknown_config_drift"

    def __init__(self, reason: str, *, context: Mapping[str, Any] | None = None) -> None:
        resolved = _required_text(reason, "reason")
        super().__init__(resolved)
        self.reason = resolved
        self.context = dict(context or ())


def derive_macro_graph_bridge_spec(
    *,
    mapping: WorldScopeMapping,
    ontology: WorldOntologyRevision,
    collection_plan: MacroCollectionPlan,
    producer_version: str = MACRO_PRODUCER_VERSION,
) -> MacroGraphBridgeRunSpec:
    """Derive the live bridge spec from loaded mapping, ontology, and collection plan."""

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if not isinstance(ontology, WorldOntologyRevision):
        raise TypeError("ontology must be WorldOntologyRevision")
    if not isinstance(collection_plan, MacroCollectionPlan):
        raise TypeError("collection_plan must be MacroCollectionPlan")
    if mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
        raise ValueError("bridge mapping_id must be world_scope_mapping.v1")
    if ontology.revision_id != market_ontology_revision_id(mapping):
        raise ValueError("bridge ontology revision_id must be derived from the mapping generation")
    if ontology.scope_mapping_id != mapping.mapping_id or ontology.scope_mapping_hash != mapping.content_sha256:
        raise ValueError("bridge ontology mapping identity does not match mapping")
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
        ontology_revision_id=ontology.revision_id,
        ontology_revision_hash=ontology.content_sha256,
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
        collection_plan_id=collection_plan.plan_id,
        collection_plan_hash=collection_plan.content_sha256,
        producer_version=producer_version,
    )


def committed_macro_graph_bridge_spec(
    *,
    mapping: WorldScopeMapping | None = None,
    ontology: WorldOntologyRevision | None = None,
    collection_plan: MacroCollectionPlan | None = None,
    producer_version: str = MACRO_PRODUCER_VERSION,
) -> MacroGraphBridgeRunSpec:
    if mapping is None or ontology is None or collection_plan is None:
        raise TypeError("bridge spec must be derived from mapping, ontology, and collection plan")
    return derive_macro_graph_bridge_spec(
        mapping=mapping,
        ontology=ontology,
        collection_plan=collection_plan,
        producer_version=producer_version,
    )


def require_committed_live_bridge_lineage(
    *,
    mapping: WorldScopeMapping,
    ontology: WorldOntologyRevision,
    collection_plan: MacroCollectionPlan,
    producer_version: str = MACRO_PRODUCER_VERSION,
) -> MacroGraphBridgeRunSpec:
    require_committed_macro_collection_plan(collection_plan)
    return derive_macro_graph_bridge_spec(
        mapping=mapping,
        ontology=ontology,
        collection_plan=collection_plan,
        producer_version=producer_version,
    )


def same_bridge_schema_family(left: MacroGraphBridgeRunSpec, right: MacroGraphBridgeRunSpec) -> bool:
    if not isinstance(left, MacroGraphBridgeRunSpec) or not isinstance(right, MacroGraphBridgeRunSpec):
        raise TypeError("specs must be MacroGraphBridgeRunSpec")
    return (
        left.schema_version == right.schema_version
        and left.scope_mapping_id == right.scope_mapping_id == WORLD_SCOPE_MAPPING_ID
        and left.collection_plan_id == right.collection_plan_id
        and left.collection_plan_hash == right.collection_plan_hash
        and left.producer_version == right.producer_version
        and admits_market_ontology_family(left.ontology_revision_id)
        and admits_market_ontology_family(right.ontology_revision_id)
    )


@dataclass(frozen=True)
class MacroGraphBridgeLifecycleDecision:
    status: str
    reason: str
    run: MacroGraphBridgeRun | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES:
            allowed = ", ".join(sorted(MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES))
            raise ValueError(f"lifecycle status must be one of: {allowed}")
        if self.run is not None and not isinstance(self.run, MacroGraphBridgeRun):
            raise TypeError("run must be MacroGraphBridgeRun or None")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))


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
    run = registry.active_run
    if run is None:
        return MacroGraphBridgeLifecycleDecision(status="missing", reason="no_active_generation")
    if run.spec == desired:
        if run.status == "blocked":
            return MacroGraphBridgeLifecycleDecision(status="matched_blocked", reason="spec_matches_blocked", run=run)
        return MacroGraphBridgeLifecycleDecision(status="matched_active", reason="spec_matches", run=run)
    if run.status == "active":
        return MacroGraphBridgeLifecycleDecision(status="drifted_active", reason="config_drift", run=run)
    if run.status == "blocked" and same_bridge_schema_family(run.spec, desired):
        return MacroGraphBridgeLifecycleDecision(status="roll_generation", reason="mapping_generation_roll", run=run)
    return MacroGraphBridgeLifecycleDecision(status="unknown_drift", reason="unknown_config_drift", run=run)


__all__ = [
    "MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES",
    "MacroGraphBridgeLifecycleDecision",
    "UnknownMacroGraphBridgeDrift",
    "classify_macro_graph_bridge",
    "committed_macro_graph_bridge_spec",
    "derive_macro_graph_bridge_spec",
    "require_committed_live_bridge_lineage",
    "same_bridge_schema_family",
]
