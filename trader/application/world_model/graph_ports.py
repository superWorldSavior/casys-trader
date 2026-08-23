"""Consumer-owned ports for World Model graph commands and point-in-time reads.

Adapters implement these contracts. Application code never imports a store,
NetworkX, runtime, or reporting module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType, Protocol

from trader.domain.world_availability import AvailabilityEvidence, PersistedWorldRef
from trader.domain.world_graph import (
    GRAPH_TRAVERSAL_POLICY_VERSION,
    KnowledgeWorldRelationEvent,
    MacroGraphBridgeEvent,
    MacroGraphBridgeFence,
    MacroGraphBridgeRegistry,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    StructuralWorldRelationEvent,
    WorldEntityEvent,
    WorldEntityIdentityEvent,
    WorldGraphSnapshot,
    WorldOntologyRevisionEvent,
    WorldRelationEvent,
)
from trader.domain.world_macro import MacroObservationEnvelope


WorldEntityEventId = NewType("WorldEntityEventId", str)
WorldEntityIdentityEventId = NewType("WorldEntityIdentityEventId", str)
WorldRelationEventId = NewType("WorldRelationEventId", str)
WorldOntologyRevisionEventId = NewType("WorldOntologyRevisionEventId", str)
WorldGraphSnapshotId = NewType("WorldGraphSnapshotId", str)
MacroGraphBridgeKey = NewType("MacroGraphBridgeKey", str)
BridgeRequestId = NewType("BridgeRequestId", str)
MacroGraphBridgeEventId = NewType("MacroGraphBridgeEventId", str)


@dataclass(frozen=True)
class WorldEntityEventEnvelope:
    event: WorldEntityEvent
    evidence: AvailabilityEvidence


@dataclass(frozen=True)
class WorldEntityIdentityEventEnvelope:
    event: WorldEntityIdentityEvent
    evidence: AvailabilityEvidence


@dataclass(frozen=True)
class WorldRelationEventEnvelope:
    event: WorldRelationEvent
    evidence: AvailabilityEvidence


@dataclass(frozen=True)
class WorldOntologyRevisionEventEnvelope:
    event: WorldOntologyRevisionEvent
    evidence: AvailabilityEvidence


class WorldGraphLedger(Protocol):
    def append_entity_event(
        self, event: WorldEntityEvent
    ) -> PersistedWorldRef[WorldEntityEventId]: ...

    def append_structural_relation_event(
        self, event: StructuralWorldRelationEvent
    ) -> PersistedWorldRef[WorldRelationEventId]: ...

    def append_knowledge_relation_event(
        self,
        event: KnowledgeWorldRelationEvent,
        fence: MacroGraphBridgeFence | None = None,
        expected_registry_version: int | None = None,
    ) -> PersistedWorldRef[WorldRelationEventId]: ...

    def append_identity_event(
        self, event: WorldEntityIdentityEvent
    ) -> PersistedWorldRef[WorldEntityIdentityEventId]: ...

    def append_revision_event(
        self, event: WorldOntologyRevisionEvent
    ) -> PersistedWorldRef[WorldOntologyRevisionEventId]: ...

    def list_entity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityEventEnvelope, ...]: ...

    def list_identity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityIdentityEventEnvelope, ...]: ...

    def list_structural_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]: ...

    def list_knowledge_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]: ...

    def list_revision_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldOntologyRevisionEventEnvelope, ...]: ...


class WorldGraphSnapshotLedger(Protocol):
    def append(
        self, snapshot: WorldGraphSnapshot
    ) -> PersistedWorldRef[WorldGraphSnapshotId]: ...

    def get(self, snapshot_id: WorldGraphSnapshotId) -> WorldGraphSnapshot | None: ...


@dataclass(frozen=True)
class WorldGraphPathStep:
    """One admissible hop. Signature tokens never include instrument or company IDs."""

    relation_id: str
    kind: str
    family: str
    direction: str
    source_kind: str
    target_kind: str
    source_node_id: str
    target_node_id: str
    freshness_bucket: str

    def signature_token(self) -> str:
        return f"{self.source_kind}|{self.kind}:{self.direction}:{self.freshness_bucket}|{self.target_kind}"


@dataclass(frozen=True)
class WorldGraphPath:
    steps: tuple[WorldGraphPathStep, ...]

    @property
    def signature(self) -> str:
        return ">".join(step.signature_token() for step in self.steps)

    @property
    def depth(self) -> int:
        return len(self.steps)

    @property
    def relation_ids(self) -> tuple[str, ...]:
        return tuple(step.relation_id for step in self.steps)


@dataclass(frozen=True)
class WorldGraphPathSet:
    paths: tuple[WorldGraphPath, ...]
    status: str = "complete"
    policy_version: str = GRAPH_TRAVERSAL_POLICY_VERSION

    @property
    def truncated(self) -> bool:
        return self.status == "graph_budget_exceeded"


class WorldGraphTraversalPort(Protocol):
    """Fresh projection of resolved PIT views. Never a persistence or lifecycle authority."""

    def enumerate_paths(
        self,
        structural: object,
        knowledge: object,
        root: object,
        *,
        max_depth: int | None = None,
        max_paths: int | None = None,
    ) -> WorldGraphPathSet: ...


class MacroObservationScanPort(Protocol):
    def reserve_activation_cursor(
        self, bridge_key: MacroGraphBridgeKey, request_id: BridgeRequestId
    ) -> MacroObservationCursorReservation: ...

    def list_available_after(
        self, cursor: MacroObservationCursor, limit: int
    ) -> tuple[MacroObservationEnvelope, ...]: ...

    def cursor_for(self, observation_id: str) -> MacroObservationCursor: ...


class MacroGraphBridgeLedger(Protocol):
    def append_event(
        self,
        event: MacroGraphBridgeEvent,
        expected_registry_version: int,
        fence: MacroGraphBridgeFence | None,
    ) -> PersistedWorldRef[MacroGraphBridgeEventId]: ...

    def load(self, bridge_key: MacroGraphBridgeKey) -> MacroGraphBridgeRegistry: ...


__all__ = [
    "BridgeRequestId",
    "MacroGraphBridgeEventId",
    "MacroGraphBridgeKey",
    "MacroGraphBridgeLedger",
    "MacroObservationScanPort",
    "WorldEntityEventEnvelope",
    "WorldEntityEventId",
    "WorldEntityIdentityEventEnvelope",
    "WorldEntityIdentityEventId",
    "WorldGraphLedger",
    "WorldGraphPath",
    "WorldGraphPathSet",
    "WorldGraphPathStep",
    "WorldGraphSnapshotId",
    "WorldGraphSnapshotLedger",
    "WorldGraphTraversalPort",
    "WorldOntologyRevisionEventEnvelope",
    "WorldOntologyRevisionEventId",
    "WorldRelationEventEnvelope",
    "WorldRelationEventId",
]
