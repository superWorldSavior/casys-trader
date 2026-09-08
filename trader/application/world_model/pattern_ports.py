"""Consumer-owned ports for pattern-hypothesis commands and occurrence persistence.

Adapters implement these contracts. Application code never imports a store,
runtime, or reporting module. ``first_seen_at`` is restart-conservative and
lives in the ledger port; callers never pass a clock or ``ready_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_pattern import (
    PatternHypothesis,
    PatternHypothesisEvent,
    PatternHypothesisId,
    PatternOccurrence,
    PatternOccurrenceEvent,
    PatternOccurrenceId,
)

_HYPOTHESIS_SUBJECT_KIND = "pattern_hypothesis_event"
_OCCURRENCE_SUBJECT_KIND = "pattern_occurrence_event"


class PatternPayloadConflict(ValueError):
    """Same pattern identity or event id replayed with a different payload."""


def _payload_hash(event: PatternHypothesisEvent | PatternOccurrenceEvent) -> str:
    return canonical_sha256(event.to_dict())


def _bind_evidence(
    event: PatternHypothesisEvent | PatternOccurrenceEvent,
    evidence: AvailabilityEvidence | None,
    *,
    subject_kind: str,
) -> AvailabilityEvidence | None:
    if evidence is None:
        return None
    if not isinstance(evidence, AvailabilityEvidence):
        raise TypeError("envelope evidence must be AvailabilityEvidence")
    subject = evidence.receipt.subject
    if subject.kind != subject_kind:
        raise ValueError(f"availability evidence subject kind must be {subject_kind}")
    if subject.subject_id != event.event_id:
        raise ValueError("availability evidence subject_id must match the event_id")
    if subject.content_sha256 != _payload_hash(event):
        raise ValueError("availability evidence content_sha256 must match the event payload hash")
    return evidence


@dataclass(frozen=True)
class PatternHypothesisEventEnvelope:
    event: PatternHypothesisEvent
    evidence: AvailabilityEvidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            _bind_evidence(self.event, self.evidence, subject_kind=_HYPOTHESIS_SUBJECT_KIND),
        )
        object.__setattr__(self, "payload_hash", _payload_hash(self.event))

    @property
    def availability_status(self) -> str:
        return "eligible" if self.evidence is not None else "availability_unproven"

    def require_proven(self) -> AvailabilityEvidence:
        if self.evidence is None:
            raise ValueError("event envelope is availability_unproven")
        return self.evidence


@dataclass(frozen=True)
class OccurrenceEventEnvelope:
    event: PatternOccurrenceEvent
    evidence: AvailabilityEvidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            _bind_evidence(self.event, self.evidence, subject_kind=_OCCURRENCE_SUBJECT_KIND),
        )
        object.__setattr__(self, "payload_hash", _payload_hash(self.event))

    @property
    def availability_status(self) -> str:
        return "eligible" if self.evidence is not None else "availability_unproven"

    def require_proven(self) -> AvailabilityEvidence:
        if self.evidence is None:
            raise ValueError("event envelope is availability_unproven")
        return self.evidence


class PatternHypothesisLedger(Protocol):
    def append_event(self, event: PatternHypothesisEvent) -> PatternHypothesisEventEnvelope:
        """Persist a hypothesis event and return its envelope.

        Adapter semantics:

        - ``first_seen_at`` is assigned by this ledger port, never by the
          service clock. A restart must keep the original first_seen
          (restart-conservative) and must not mutate ``ready_at``.
        - Exact existing identical event (same ``event_id`` and payload): repair
          a missing availability receipt. Do not insert a second copy.
        - Same ``event_id`` with a different payload is a typed conflict.
        """
        ...

    def load(self, hypothesis_id: PatternHypothesisId) -> PatternHypothesis: ...

    def list_hypotheses(self) -> tuple[PatternHypothesis, ...]:
        """Inspection path: reconstruct every hypothesis from the full event stream."""
        ...


class OccurrenceLedger(Protocol):
    def append_event(self, event: PatternOccurrenceEvent) -> OccurrenceEventEnvelope:
        """Persist an occurrence event and return its envelope.

        Adapter semantics match ``PatternHypothesisLedger.append_event``:
        restart-conservative ``first_seen_at``, identical retry repairs the
        receipt, conflicting payload is a typed conflict.
        """
        ...

    def load(self, occurrence_id: PatternOccurrenceId) -> PatternOccurrence: ...


class PatternAvailability(Protocol):
    def evidence_for(
        self,
        event: PatternHypothesisEvent | PatternOccurrenceEvent,
    ) -> AvailabilityEvidence | None:
        """Return store-attested evidence for ``event``, or None if unproven.

        Callers do not pass ``ready_at`` or ``first_seen_at``.
        """
        ...


__all__ = [
    "OccurrenceEventEnvelope",
    "OccurrenceLedger",
    "PatternAvailability",
    "PatternHypothesisEventEnvelope",
    "PatternHypothesisLedger",
    "PatternPayloadConflict",
]
