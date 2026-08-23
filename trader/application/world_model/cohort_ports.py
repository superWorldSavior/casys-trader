"""Consumer-owned ports for the prospective World cohort use case.

These contracts live next to the application service that consumes them.
Adapters implement them; this module does not import infrastructure, runtime,
or reporting. Signatures stay typed: no arbitrary mappings, no caller-controlled
availability clock, and no free status strings.
"""

from __future__ import annotations

from typing import Protocol

from trader.domain.world_cohort import (
    WorldCohort,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
)

__all__ = [
    "WorldCohortQuery",
    "WorldCohortRepository",
]


class WorldCohortRepository(Protocol):
    def register(
        self,
        manifest: WorldCohortManifest,
        event: WorldCohortRegistered,
    ) -> WorldCohortEventEnvelope: ...

    def append_event(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope: ...

    def load(self, cohort_id: WorldCohortId) -> WorldCohort: ...


class WorldCohortQuery(Protocol):
    def list_slots(self, cohort_id: WorldCohortId) -> tuple[WorldCohortSlot, ...]: ...

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope: ...
