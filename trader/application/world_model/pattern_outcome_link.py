"""Later-phase linker of observed 4h / 1d / 3d leaves onto recorded occurrences.

This is the only prospective-lifecycle service allowed to read WorldOutcome
leaves. Matching never calls it. Pending horizons stay pending. Replay of
the same active leaf is idempotent; a canonical correction flows through
WorldPatternService.link and must explicitly supersede the current leaf.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from trader.application.world_model.pattern_evaluation_ports import PatternOccurrenceCatalog
from trader.application.world_model.pattern_service import LinkPatternOutcome, WorldPatternService
from trader.domain.world_cohort import WorldCohortId
from trader.domain.world_episode import WorldOutcome, canonical_sha256, parse_utc_timestamp
from trader.domain.world_pattern import (
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternOccurrence,
    PatternOccurrenceId,
    PatternOutcomeLink,
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _unique_ids(value: Sequence[str] | None, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    items = tuple(_required_text(item, f"{field_name}[]") for item in value)
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


class PatternOutcomeLeafQuery(Protocol):
    """Read-only active observed WorldOutcome leaves. Never used during matching."""

    def load_active_observed_leaves(
        self,
        *,
        episode_ids: Sequence[str],
        horizon_ids: Sequence[str],
        as_of: datetime,
    ) -> tuple[WorldOutcome, ...]:
        """Return active observed leaves available through as_of. Never invent a leaf."""


@dataclass(frozen=True)
class PatternOutcomeLinkRequest:
    as_of: datetime | str
    evaluation_cohort_id: str | None = None
    hypothesis_ids: Sequence[str] | None = None
    occurrence_ids: Sequence[str] | None = None

    def __post_init__(self) -> None:
        as_of = parse_utc_timestamp(self.as_of, "as_of")
        cohort_id = self.evaluation_cohort_id
        if cohort_id is not None:
            cohort_id = WorldCohortId(_required_text(cohort_id, "evaluation_cohort_id")).value
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "evaluation_cohort_id", cohort_id)
        object.__setattr__(self, "hypothesis_ids", _unique_ids(self.hypothesis_ids, "hypothesis_ids"))
        object.__setattr__(self, "occurrence_ids", _unique_ids(self.occurrence_ids, "occurrence_ids"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "evaluation_cohort_id": self.evaluation_cohort_id,
            "hypothesis_ids": None if self.hypothesis_ids is None else list(self.hypothesis_ids),
            "occurrence_ids": None if self.occurrence_ids is None else list(self.occurrence_ids),
            "expected_horizon_ids": list(PATTERN_EVALUATION_HORIZON_IDS),
        }


@dataclass(frozen=True)
class PatternHorizonLinkStatus:
    occurrence_id: str
    hypothesis_id: str
    episode_id: str
    linked: Mapping[str, str]
    available: Mapping[str, str]
    pending: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "hypothesis_id": self.hypothesis_id,
            "episode_id": self.episode_id,
            "linked": dict(self.linked),
            "available": dict(self.available),
            "pending": list(self.pending),
        }


@dataclass(frozen=True)
class PatternOutcomeLinkResult:
    statuses: tuple[PatternHorizonLinkStatus, ...]
    linked_count: int
    available_count: int
    pending_count: int
    request: PatternOutcomeLinkRequest

    def to_dict(self) -> dict[str, Any]:
        return {
            "statuses": [item.to_dict() for item in self.statuses],
            "linked_count": self.linked_count,
            "available_count": self.available_count,
            "pending_count": self.pending_count,
            "request": self.request.to_dict(),
            "source_evidence_fingerprint": canonical_sha256(
                sorted(item.occurrence_id for item in self.statuses)
            ),
        }


def _index_leaves(leaves: Sequence[WorldOutcome]) -> dict[tuple[str, str], WorldOutcome]:
    by_key: dict[tuple[str, str], WorldOutcome] = {}
    for outcome in leaves:
        if not isinstance(outcome, WorldOutcome):
            raise TypeError("outcome leaf query must return WorldOutcome")
        key = (outcome.episode_id, outcome.horizon.horizon_id)
        if key in by_key:
            raise ValueError("ambiguous active observed outcome leaf")
        by_key[key] = outcome
    return by_key


def _same_active_leaf(link: PatternOutcomeLink, outcome: WorldOutcome) -> bool:
    return (
        link.world_outcome_event_id == outcome.event_id
        and link.world_outcome_content_sha256 == outcome.payload_hash
    )


def _status_for(
    occurrence: PatternOccurrence,
    leaves: Mapping[tuple[str, str], WorldOutcome],
) -> PatternHorizonLinkStatus:
    linked: dict[str, str] = {}
    available: dict[str, str] = {}
    pending: list[str] = []
    episode_id = occurrence.spec.forecast.episode_id
    for horizon_id in occurrence.spec.expected_horizon_ids:
        current = occurrence.active_outcome_link(horizon_id)
        outcome = leaves.get((episode_id, horizon_id))
        if current is not None:
            linked[horizon_id] = current.world_outcome_event_id
            if outcome is not None and outcome.event_id is not None and not _same_active_leaf(current, outcome):
                available[horizon_id] = outcome.event_id
            continue
        if outcome is None or outcome.event_id is None:
            pending.append(horizon_id)
        else:
            available[horizon_id] = outcome.event_id
    return PatternHorizonLinkStatus(
        occurrence_id=occurrence.occurrence_id,
        hypothesis_id=occurrence.hypothesis_id,
        episode_id=episode_id,
        linked=linked,
        available=available,
        pending=tuple(pending),
    )


def _result_for(
    occurrences: Sequence[PatternOccurrence],
    leaves: Mapping[tuple[str, str], WorldOutcome],
    request: PatternOutcomeLinkRequest,
) -> PatternOutcomeLinkResult:
    statuses = tuple(_status_for(item, leaves) for item in occurrences)
    return PatternOutcomeLinkResult(
        statuses=statuses,
        linked_count=sum(len(item.linked) for item in statuses),
        available_count=sum(len(item.available) for item in statuses),
        pending_count=sum(len(item.pending) for item in statuses),
        request=request,
    )


@dataclass(frozen=True)
class PatternOutcomeLinkService:
    catalog: PatternOccurrenceCatalog
    outcomes: PatternOutcomeLeafQuery

    def preview(self, request: PatternOutcomeLinkRequest) -> PatternOutcomeLinkResult:
        if not isinstance(request, PatternOutcomeLinkRequest):
            raise TypeError("PatternOutcomeLinkRequest is required")
        occurrences = self._load(request)
        return _result_for(occurrences, self._leaves(occurrences, request.as_of), request)

    def link(
        self,
        request: PatternOutcomeLinkRequest,
        *,
        patterns: WorldPatternService,
    ) -> PatternOutcomeLinkResult:
        if not isinstance(request, PatternOutcomeLinkRequest):
            raise TypeError("PatternOutcomeLinkRequest is required")
        occurrences = self._load(request)
        leaves = self._leaves(occurrences, request.as_of)
        updated: list[PatternOccurrence] = []
        for occurrence in occurrences:
            current = occurrence
            if current.status == "invalidated":
                updated.append(current)
                continue
            for horizon_id in current.spec.expected_horizon_ids:
                outcome = leaves.get((current.spec.forecast.episode_id, horizon_id))
                if outcome is None:
                    continue
                existing = current.active_outcome_link(horizon_id)
                if existing is not None and _same_active_leaf(existing, outcome):
                    continue
                envelope = patterns.link(
                    LinkPatternOutcome(occurrence_id=current.occurrence_id, outcome=outcome)
                )
                current = patterns.occurrences.load(PatternOccurrenceId(envelope.event.occurrence_id))
            updated.append(current)
        return _result_for(updated, {}, request)

    def _load(self, request: PatternOutcomeLinkRequest) -> tuple[PatternOccurrence, ...]:
        return self.catalog.list_recorded_occurrences(
            evaluation_cohort_id=request.evaluation_cohort_id,
            hypothesis_ids=request.hypothesis_ids,
            occurrence_ids=request.occurrence_ids,
        )

    def _leaves(
        self,
        occurrences: Sequence[PatternOccurrence],
        as_of: datetime,
    ) -> dict[tuple[str, str], WorldOutcome]:
        episode_ids = tuple(dict.fromkeys(item.spec.forecast.episode_id for item in occurrences))
        if not episode_ids:
            return {}
        return _index_leaves(
            self.outcomes.load_active_observed_leaves(
                episode_ids=episode_ids,
                horizon_ids=PATTERN_EVALUATION_HORIZON_IDS,
                as_of=as_of,
            )
        )


__all__ = (
    "PatternHorizonLinkStatus",
    "PatternOutcomeLeafQuery",
    "PatternOutcomeLinkRequest",
    "PatternOutcomeLinkResult",
    "PatternOutcomeLinkService",
)
