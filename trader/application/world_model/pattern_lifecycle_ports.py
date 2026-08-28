"""Consumer-owned port for pattern-discovery lifecycle events.

Adapters implement this contract. Application code never imports a store,
runtime, or reporting module. ``PatternDiscoveryCompleted`` is the existing
domain event; this module does not redefine it.

Exact replay returns False. A different payload at the same ``event_id`` or
the same ``(evaluation_cohort_id, started_event_id)`` is the existing
``PatternPayloadConflict``. This proof does not stamp an availability receipt.
"""

from __future__ import annotations

from typing import Protocol

from trader.domain.world_pattern import PatternDiscoveryCompleted


class PatternLifecycleLedger(Protocol):
    def append_discovery_completed(self, event: PatternDiscoveryCompleted) -> bool:
        """Persist a discovery-completed event.

        Adapter semantics:

        - Exact existing identical event (same identity and payload): return
          False. Do not insert a second copy and do not stamp a receipt.
        - Same ``event_id`` or same ``(evaluation_cohort_id, started_event_id)``
          with a different payload is a typed conflict.
        - A new durable row returns True.
        """
        ...

    def get_discovery_completed(
        self,
        evaluation_cohort_id: str,
        started_event_id: str,
    ) -> PatternDiscoveryCompleted | None:
        """Return the completed event for that cohort/start, or None."""
        ...


__all__ = ["PatternLifecycleLedger"]
