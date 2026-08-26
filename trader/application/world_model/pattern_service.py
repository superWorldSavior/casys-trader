"""Typed command handlers for pattern hypotheses and prospective occurrences.

The use case loads reconstructed aggregates, asks them to transition, then
persists domain events through consumer-owned ports. An occurrence must be
durable (recorded + AvailabilityEvidence) before any PatternOutcomeLink.
Authority stays shadow_only with decision_effect=none; causal_claim stays false.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.application.world_model.pattern_ports import (
    OccurrenceEventEnvelope,
    OccurrenceLedger,
    PatternAvailability,
    PatternHypothesisEventEnvelope,
    PatternHypothesisLedger,
    PatternPayloadConflict,
)
from trader.domain.world_episode import WorldOutcome, WorldPrediction
from trader.domain.world_graph import WorldEntityRef
from trader.domain.world_pattern import (
    PatternEvaluationClosed,
    PatternEvaluationStarted,
    PatternForecast,
    PatternHypothesis,
    PatternHypothesisEvent,
    PatternHypothesisId,
    PatternHypothesisInvalidated,
    PatternHypothesisSpec,
    PatternMatchedHop,
    PatternOccurrence,
    PatternOccurrenceEvent,
    PatternOccurrenceId,
    PatternOccurrenceInvalidated,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
)


PATTERN_AUTHORITY = "shadow_only"
PATTERN_DECISION_EFFECT = "none"
PATTERN_CAUSAL_CLAIM = False
PATTERN_RECOMMENDATION = "NO_GO"


def _require_command(command: object, expected: type) -> None:
    if not isinstance(command, expected):
        raise TypeError(f"{expected.__name__} is required")


def _hypothesis_id(value: PatternHypothesisId | str) -> PatternHypothesisId:
    return value if isinstance(value, PatternHypothesisId) else PatternHypothesisId(value)


def _occurrence_id(value: PatternOccurrenceId | str) -> PatternOccurrenceId:
    return value if isinstance(value, PatternOccurrenceId) else PatternOccurrenceId(value)


def _raise_conflict(exc: ValueError) -> None:
    message = str(exc)
    if "conflict" in message:
        raise PatternPayloadConflict(message) from exc
    raise exc


def validate_world_outcome_leaf(
    outcome: object,
    *,
    expected_horizon_id: str | None = None,
    expected_episode_id: str | None = None,
) -> WorldOutcome:
    """Reject anything that is not a canonical WorldOutcome leaf (ID/digest/horizon)."""

    if not isinstance(outcome, WorldOutcome):
        raise TypeError("outcome link requires a canonical WorldOutcome leaf")
    if outcome.event_id is None or outcome.payload_hash is None:
        raise ValueError("WorldOutcome leaf is missing canonical identity")
    if not str(outcome.event_id).startswith("world-outcome:v1:"):
        raise ValueError("invalid WorldOutcome leaf identity")
    if expected_horizon_id is not None and outcome.horizon.horizon_id != expected_horizon_id:
        raise ValueError("WorldOutcome leaf horizon does not match")
    if expected_episode_id is not None and outcome.episode_id != expected_episode_id:
        raise ValueError("WorldOutcome episode_id does not match the forecast")
    return outcome


@dataclass(frozen=True)
class RegisterPatternHypothesis:
    spec: PatternHypothesisSpec | Mapping[str, Any]
    registered_at: datetime | str


@dataclass(frozen=True)
class StartPatternEvaluation:
    hypothesis_id: PatternHypothesisId | str
    evaluation_cohort_id: str
    started_at: datetime | str
    evaluation_dataset_fingerprint: str


@dataclass(frozen=True)
class RecordPatternOccurrence:
    hypothesis_id: PatternHypothesisId | str
    cohort_id: str
    instrument: WorldEntityRef | Mapping[str, Any]
    cutoff_at: datetime | str
    exact_path: Sequence[PatternMatchedHop | Mapping[str, Any]]
    forecast: PatternForecast | WorldPrediction | Mapping[str, Any]
    artifact_refs: Sequence[str] = ()
    fact_refs: Sequence[str] = ()
    expected_horizon_ids: Sequence[str] | None = None


@dataclass(frozen=True)
class LinkPatternOutcome:
    occurrence_id: PatternOccurrenceId | str
    outcome: WorldOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WorldOutcome):
            raise TypeError("LinkPatternOutcome requires a canonical WorldOutcome leaf")


@dataclass(frozen=True)
class ClosePatternEvaluation:
    hypothesis_id: PatternHypothesisId | str
    closed_at: datetime | str


@dataclass(frozen=True)
class InvalidatePatternHypothesis:
    hypothesis_id: PatternHypothesisId | str
    invalidated_at: datetime | str
    reason: str


@dataclass(frozen=True)
class InvalidatePatternOccurrence:
    occurrence_id: PatternOccurrenceId | str
    invalidated_at: datetime | str
    reason: str


WORLD_PATTERN_COMMANDS = (
    RegisterPatternHypothesis,
    StartPatternEvaluation,
    RecordPatternOccurrence,
    LinkPatternOutcome,
    ClosePatternEvaluation,
    InvalidatePatternHypothesis,
    InvalidatePatternOccurrence,
)

WorldPatternCommand = (
    RegisterPatternHypothesis
    | StartPatternEvaluation
    | RecordPatternOccurrence
    | LinkPatternOutcome
    | ClosePatternEvaluation
    | InvalidatePatternHypothesis
    | InvalidatePatternOccurrence
)


def _hypothesis_event_for(
    before: PatternHypothesis,
    after: PatternHypothesis,
    event_type: type[PatternHypothesisEvent],
) -> PatternHypothesisEvent:
    if len(after.events) == len(before.events):
        matches = [event for event in after.events if isinstance(event, event_type)]
        if not matches:
            raise ValueError("no existing event matches the idempotent command")
        return matches[-1]
    if len(after.events) != len(before.events) + 1:
        raise ValueError("command must append at most one event")
    return after.events[-1]


def _occurrence_event_for(before: PatternOccurrence, after: PatternOccurrence) -> PatternOccurrenceEvent:
    if len(after.events) == len(before.events):
        return after.events[-1]
    if len(after.events) != len(before.events) + 1:
        raise ValueError("command must append at most one event")
    return after.events[-1]


@dataclass(frozen=True)
class WorldPatternService:
    hypotheses: PatternHypothesisLedger
    occurrences: OccurrenceLedger
    availability: PatternAvailability

    def handle(self, command: WorldPatternCommand) -> PatternHypothesisEventEnvelope | OccurrenceEventEnvelope:
        if not isinstance(command, WORLD_PATTERN_COMMANDS):
            raise TypeError(f"unsupported command: {type(command).__name__}")
        if isinstance(command, RegisterPatternHypothesis):
            return self.register(command)
        if isinstance(command, StartPatternEvaluation):
            return self.start(command)
        if isinstance(command, RecordPatternOccurrence):
            return self.record(command)
        if isinstance(command, LinkPatternOutcome):
            return self.link(command)
        if isinstance(command, ClosePatternEvaluation):
            return self.close(command)
        if isinstance(command, InvalidatePatternHypothesis):
            return self.invalidate(command)
        if isinstance(command, InvalidatePatternOccurrence):
            return self.invalidate_occurrence(command)
        raise TypeError(f"unsupported command: {type(command).__name__}")

    def register(self, command: RegisterPatternHypothesis) -> PatternHypothesisEventEnvelope:
        _require_command(command, RegisterPatternHypothesis)
        incoming = PatternHypothesis.register(command.spec, registered_at=command.registered_at)
        existing = self._try_load_hypothesis(incoming.hypothesis_id)
        if existing is None:
            return self.hypotheses.append_event(incoming.registered)
        if existing.registered != incoming.registered:
            raise PatternPayloadConflict("conflict: same hypothesis id with different content")
        return self.hypotheses.append_event(existing.registered)

    def start(self, command: StartPatternEvaluation) -> PatternHypothesisEventEnvelope:
        _require_command(command, StartPatternEvaluation)
        hypothesis = self.hypotheses.load(_hypothesis_id(command.hypothesis_id))
        try:
            next_hypothesis = hypothesis.start_evaluation(
                started_at=command.started_at,
                evaluation_dataset_fingerprint=command.evaluation_dataset_fingerprint,
                evaluation_cohort_id=command.evaluation_cohort_id,
            )
        except ValueError as exc:
            _raise_conflict(exc)
        event = _hypothesis_event_for(hypothesis, next_hypothesis, PatternEvaluationStarted)
        return self.hypotheses.append_event(event)

    def record(self, command: RecordPatternOccurrence) -> OccurrenceEventEnvelope:
        _require_command(command, RecordPatternOccurrence)
        hypothesis = self.hypotheses.load(_hypothesis_id(command.hypothesis_id))
        incoming = PatternOccurrence.record(
            hypothesis,
            cohort_id=command.cohort_id,
            instrument=command.instrument,
            cutoff_at=command.cutoff_at,
            exact_path=command.exact_path,
            forecast=command.forecast,
            artifact_refs=command.artifact_refs,
            fact_refs=command.fact_refs,
            expected_horizon_ids=command.expected_horizon_ids,
        )
        existing = self._try_load_occurrence(incoming.occurrence_id)
        if existing is None:
            return self.occurrences.append_event(incoming.recorded)
        if existing.recorded.occurrence != incoming.recorded.occurrence:
            raise PatternPayloadConflict("conflict: same occurrence id with different content")
        return self.occurrences.append_event(existing.recorded)

    def link(self, command: LinkPatternOutcome) -> OccurrenceEventEnvelope:
        _require_command(command, LinkPatternOutcome)
        occurrence = self.occurrences.load(_occurrence_id(command.occurrence_id))
        if self.availability.evidence_for(occurrence.recorded) is None:
            raise ValueError("occurrence is availability_unproven; recorded event must be durable before an outcome link")
        outcome = validate_world_outcome_leaf(
            command.outcome,
            expected_episode_id=occurrence.spec.forecast.episode_id,
        )
        current = occurrence.active_outcome_link(outcome.horizon.horizon_id)
        try:
            if current is None:
                next_occurrence = occurrence.link_outcome(outcome)
            elif (
                current.world_outcome_event_id == outcome.event_id
                and current.world_outcome_content_sha256 == outcome.payload_hash
            ):
                next_occurrence = occurrence
            else:
                next_occurrence = occurrence.supersede_outcome_link(outcome)
        except ValueError as exc:
            _raise_conflict(exc)
        event = _occurrence_event_for(occurrence, next_occurrence)
        if not isinstance(event, (PatternOutcomeLinked, PatternOutcomeLinkSuperseded)):
            raise TypeError("link must persist a PatternOutcomeLinked or supersession event")
        return self.occurrences.append_event(event)

    def close(self, command: ClosePatternEvaluation) -> PatternHypothesisEventEnvelope:
        _require_command(command, ClosePatternEvaluation)
        hypothesis = self.hypotheses.load(_hypothesis_id(command.hypothesis_id))
        try:
            next_hypothesis = hypothesis.close_evaluation(closed_at=command.closed_at)
        except ValueError as exc:
            _raise_conflict(exc)
        event = _hypothesis_event_for(hypothesis, next_hypothesis, PatternEvaluationClosed)
        return self.hypotheses.append_event(event)

    def invalidate(self, command: InvalidatePatternHypothesis) -> PatternHypothesisEventEnvelope:
        _require_command(command, InvalidatePatternHypothesis)
        hypothesis = self.hypotheses.load(_hypothesis_id(command.hypothesis_id))
        try:
            next_hypothesis = hypothesis.invalidate(invalidated_at=command.invalidated_at, reason=command.reason)
        except ValueError as exc:
            _raise_conflict(exc)
        event = _hypothesis_event_for(hypothesis, next_hypothesis, PatternHypothesisInvalidated)
        return self.hypotheses.append_event(event)

    def invalidate_occurrence(self, command: InvalidatePatternOccurrence) -> OccurrenceEventEnvelope:
        _require_command(command, InvalidatePatternOccurrence)
        occurrence = self.occurrences.load(_occurrence_id(command.occurrence_id))
        next_occurrence = occurrence.invalidate(invalidated_at=command.invalidated_at, reason=command.reason)
        event = _occurrence_event_for(occurrence, next_occurrence)
        if not isinstance(event, PatternOccurrenceInvalidated):
            raise TypeError("invalidate_occurrence must persist PatternOccurrenceInvalidated")
        return self.occurrences.append_event(event)

    def _try_load_hypothesis(self, hypothesis_id: str) -> PatternHypothesis | None:
        try:
            return self.hypotheses.load(_hypothesis_id(hypothesis_id))
        except LookupError:
            return None

    def _try_load_occurrence(self, occurrence_id: str) -> PatternOccurrence | None:
        try:
            return self.occurrences.load(_occurrence_id(occurrence_id))
        except LookupError:
            return None


__all__ = [
    "PATTERN_AUTHORITY",
    "PATTERN_CAUSAL_CLAIM",
    "PATTERN_DECISION_EFFECT",
    "PATTERN_RECOMMENDATION",
    "WORLD_PATTERN_COMMANDS",
    "ClosePatternEvaluation",
    "InvalidatePatternHypothesis",
    "InvalidatePatternOccurrence",
    "LinkPatternOutcome",
    "RecordPatternOccurrence",
    "RegisterPatternHypothesis",
    "StartPatternEvaluation",
    "WorldPatternCommand",
    "WorldPatternService",
    "validate_world_outcome_leaf",
]
