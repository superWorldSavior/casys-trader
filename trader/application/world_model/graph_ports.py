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
    "WorldGraphSnapshotId",
    "WorldGraphSnapshotLedger",
    "WorldOntologyRevisionEventEnvelope",
    "WorldOntologyRevisionEventId",
    "WorldRelationEventEnvelope",
    "WorldRelationEventId",
]
