"""Consumer-owned ports for the source-only macro World observation use case.

These contracts live next to the application pipeline that consumes them.
Adapters implement them; this module does not import infrastructure, runtime,
or reporting. Signatures stay typed: no arbitrary mappings, no caller-controlled
availability clock, and no Univers/Brain/company objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trader.domain.world_availability import PersistedWorldRef
from trader.domain.world_macro import (
    MacroCollectionEvent,
    MacroCollectionEventId,
    MacroCollectionRunId,
    MacroObservationEnvelope,
    MacroScope,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroWorldObservation,
)

__all__ = [
    "MacroCollectionLedger",
    "MacroHistory",
    "MacroSourcePort",
    "WorldMacroObservationReader",
]


class MacroSourcePort(Protocol):
    def read_facts(
        self, scope: MacroScope, observed_at: datetime
    ) -> tuple[MacroSourceFact, ...]: ...


class MacroHistory(Protocol):
    def append_fact(
        self, fact: MacroSourceFact
    ) -> PersistedWorldRef[MacroSourceFactVersionId]: ...

    def append_observation(
        self, observation: MacroWorldObservation
    ) -> MacroObservationEnvelope: ...


class WorldMacroObservationReader(Protocol):
    def list_candidates_available_through(
        self, scope: MacroScope, cutoff_at: datetime
    ) -> tuple[MacroObservationEnvelope, ...]: ...


class MacroCollectionLedger(Protocol):
    def append_event(
        self, event: MacroCollectionEvent
    ) -> PersistedWorldRef[MacroCollectionEventId]: ...

    def load(self, run_id: MacroCollectionRunId) -> tuple[MacroCollectionEvent, ...]: ...
