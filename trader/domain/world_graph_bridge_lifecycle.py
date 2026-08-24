"""Typed macro-graph bridge lifecycle: classify durable state before reservation.

Append-only. No wildcard drift repair, no runtime migration, no handoff.
Shadow overlay only — never a Trader authority or a causal claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.world_feature_contract import (
    MARKET_ONTOLOGY_REVISION,
    MARKET_ONTOLOGY_SHA256,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRun,
    MacroGraphBridgeRunSpec,
    WorldOntologyRevision,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    WORLD_MACRO_COLLECTION_PLAN_ID,
    WORLD_MACRO_COLLECTION_PLAN_SHA256,
    MacroCollectionPlan,
    require_committed_macro_collection_plan,
)
from trader.domain.world_scope import WorldScopeMapping


MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES = frozenset(
    {
        "missing",
        "matched_active",
        "matched_blocked",
        "drifted_active",
        "unknown_drift",
    }
)
COMMITTED_MACRO_GRAPH_BRIDGE_SPEC = MacroGraphBridgeRunSpec(
    scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
    scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
    ontology_revision_id=MARKET_ONTOLOGY_REVISION,
    ontology_revision_hash=MARKET_ONTOLOGY_SHA256,
    schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
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
    """Fail-closed drift. No admitted lineage migration exists."""

    code = "unknown_config_drift"

    def __init__(self, reason: str, *, context: Mapping[str, Any] | None = None) -> None:
        resolved = _required_text(reason, "reason")
        super().__init__(resolved)
        self.reason = resolved
        self.context = dict(context or ())


def committed_macro_graph_bridge_spec() -> MacroGraphBridgeRunSpec:
    return COMMITTED_MACRO_GRAPH_BRIDGE_SPEC


def require_committed_live_bridge_lineage(
    *,
    mapping: WorldScopeMapping,
    ontology: WorldOntologyRevision,
    collection_plan: MacroCollectionPlan,
    producer_version: str = MACRO_PRODUCER_VERSION,
) -> MacroGraphBridgeRunSpec:
    """Fail closed when derived live identities are not the frozen current spec."""

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if not isinstance(ontology, WorldOntologyRevision):
        raise TypeError("ontology must be WorldOntologyRevision")
    if not isinstance(collection_plan, MacroCollectionPlan):
        raise TypeError("collection_plan must be MacroCollectionPlan")
    require_committed_macro_collection_plan(collection_plan)
    derived = MacroGraphBridgeRunSpec(
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
        ontology_revision_id=ontology.revision_id,
        ontology_revision_hash=ontology.content_sha256,
        schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
        collection_plan_id=collection_plan.plan_id,
        collection_plan_hash=collection_plan.content_sha256,
        producer_version=producer_version,
    )
    committed = committed_macro_graph_bridge_spec()
    if derived != committed:
        raise ValueError("derived live bridge spec drifted from committed current lineage")
    return derived


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
    return MacroGraphBridgeLifecycleDecision(status="unknown_drift", reason="unknown_config_drift", run=run)


__all__ = [
    "COMMITTED_MACRO_GRAPH_BRIDGE_SPEC",
    "MACRO_GRAPH_BRIDGE_LIFECYCLE_STATUSES",
    "MacroGraphBridgeLifecycleDecision",
    "UnknownMacroGraphBridgeDrift",
    "classify_macro_graph_bridge",
    "committed_macro_graph_bridge_spec",
    "require_committed_live_bridge_lineage",
]
