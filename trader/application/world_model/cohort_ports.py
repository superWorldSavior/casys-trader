"""Consumer-owned ports for the prospective World cohort use case.

These contracts live next to the application service that consumes them.
Adapters implement them; this module does not import infrastructure, runtime,
or reporting. Signatures stay typed: no arbitrary mappings, no caller-controlled
availability clock, and no free status strings.

``append_event`` is compare-and-swap on ``expected_sequence``. Adapters must
repair a missing receipt for an identical existing event even when the head
has advanced, and must reject a conflicting sequence on insertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.domain.world_cohort import (
    WorldCohort,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
    WorldRuntimeIdentity,
)

__all__ = [
    "WorldCohortQuery",
    "WorldCohortRepository",
    "WorldOntologyHeadsProof",
    "WorldOntologyProofQuery",
    "WorldRuntimeIdentityPort",
]


class WorldCohortRepository(Protocol):
    def register(
        self,
        manifest: WorldCohortManifest,
        event: WorldCohortRegistered,
    ) -> WorldCohortEventEnvelope: ...

    def append_event(
        self,
        event: WorldCohortEvent,
        *,
        expected_sequence: int,
    ) -> WorldCohortEventEnvelope:
        """Persist ``event`` under compare-and-swap ``expected_sequence``.

        Adapter semantics:

        - Exact existing identical event (same ``event_id`` and payload): repair
          a missing availability receipt even if the log head has advanced.
          Do not insert a second copy and do not require ``expected_sequence``
          to match the current length.
        - Insertion: require the persisted event count to equal
          ``expected_sequence`` (the pre-command aggregate length). Assign the
          monotone sequence ``expected_sequence + 1``. Reject a conflicting
          sequence (stale snapshot or concurrent writer). This is the CAS gate.
        - Same ``event_id`` with a different payload is always a conflict.
        """
        ...

    def load(self, cohort_id: WorldCohortId) -> WorldCohort: ...


class WorldCohortQuery(Protocol):
    def list_slots(self, cohort_id: WorldCohortId) -> tuple[WorldCohortSlot, ...]: ...

    def list_collecting_cohorts(self) -> tuple[WorldCohort, ...]:
        """Reconstructed collecting aggregates. Never registers, arms, or starts."""
        ...

    def list_live_cohorts(self) -> tuple[WorldCohort, ...]:
        """REGISTERED, ARMED, or COLLECTING aggregates. Never mutates."""
        ...

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope: ...


class WorldRuntimeIdentityPort(Protocol):
    """Measure the running git/build/Python/NumPy identity. Never reads a committed SHA."""

    def measure(self) -> WorldRuntimeIdentity: ...


@dataclass(frozen=True)
class WorldOntologyHeadsProof:
    """Durable proof that one published ontology revision matches the declared mapping heads."""

    revision_id: str
    content_sha256: str
    scope_mapping_id: str
    scope_mapping_hash: str
    entity_heads_hash: str
    structural_heads_hash: str


class WorldOntologyProofQuery(Protocol):
    def proven_heads(
        self,
        *,
        revision_id: str,
        scope_mapping_id: str,
        scope_mapping_hash: str,
        at: datetime,
    ) -> WorldOntologyHeadsProof | None: ...
