"""Immutable request and result types for prospective pattern evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.domain.world_cohort import WorldCohortId
from trader.domain.world_episode import WorldPrediction, canonical_sha256, parse_utc_timestamp
from trader.domain.world_pattern import (
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternHypothesis,
    PatternOccurrence,
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
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


@dataclass(frozen=True)
class PatternEvaluationRequest:
    """As-of prospective matching. No outcome fields. Apply is a CLI concern."""

    as_of: datetime | str
    evaluation_cohort_id: str
    evaluation_dataset_fingerprint: str
    hypothesis_ids: Sequence[str] | None = None

    def __post_init__(self) -> None:
        as_of = parse_utc_timestamp(self.as_of, "as_of")
        cohort_id = WorldCohortId(_required_text(self.evaluation_cohort_id, "evaluation_cohort_id")).value
        fingerprint = _sha256_hex(self.evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "evaluation_cohort_id", cohort_id)
        object.__setattr__(self, "evaluation_dataset_fingerprint", fingerprint)
        object.__setattr__(self, "hypothesis_ids", _unique_ids(self.hypothesis_ids, "hypothesis_ids"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "evaluation_cohort_id": self.evaluation_cohort_id,
            "evaluation_dataset_fingerprint": self.evaluation_dataset_fingerprint,
            "hypothesis_ids": None if self.hypothesis_ids is None else list(self.hypothesis_ids),
            "expected_horizon_ids": list(PATTERN_EVALUATION_HORIZON_IDS),
        }


@dataclass(frozen=True)
class PatternEvaluationScanRequest:
    """Source-owned unlabeled scan window bound to one evaluation cohort.

    Lower bound is exclusive. The dataset fingerprint stays on
    ``PatternEvaluationRequest`` and the evaluating hypotheses; this scan
    carries only the cohort identity used to admit graph companions.
    """

    as_of: datetime | str
    not_before: datetime | str
    evaluation_cohort_id: str

    def __post_init__(self) -> None:
        as_of = parse_utc_timestamp(self.as_of, "as_of")
        not_before = parse_utc_timestamp(self.not_before, "not_before")
        if not (not_before < as_of):
            raise ValueError("as_of must be strictly later than not_before")
        cohort_id = WorldCohortId(_required_text(self.evaluation_cohort_id, "evaluation_cohort_id")).value
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "not_before", not_before)
        object.__setattr__(self, "evaluation_cohort_id", cohort_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "not_before": self.not_before.isoformat(),
            "evaluation_cohort_id": self.evaluation_cohort_id,
        }

@dataclass(frozen=True)
class PatternEvaluationMatch:
    """One in-memory shadow forecast and occurrence, constructed before any label."""

    hypothesis: PatternHypothesis
    occurrence: PatternOccurrence
    prediction: WorldPrediction
    semantic_signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis.hypothesis_id,
            "occurrence_id": self.occurrence.occurrence_id,
            "prediction_id": self.prediction.prediction_id,
            "episode_id": self.prediction.episode_id,
            "cutoff_at": self.occurrence.cutoff_at.isoformat(),
            "semantic_signature": self.semantic_signature,
            "expected_horizon_ids": list(self.occurrence.spec.expected_horizon_ids),
            "forecast_horizon_id": self.prediction.horizon_id,
        }


@dataclass(frozen=True)
class PatternEvaluationResult:
    matches: tuple[PatternEvaluationMatch, ...]
    hypotheses: tuple[PatternHypothesis, ...]
    eligible_records: int
    rejection_counts: Mapping[str, int]
    source_evidence_ids: tuple[str, ...]
    request: PatternEvaluationRequest

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [item.to_dict() for item in self.matches],
            "hypothesis_ids": [item.hypothesis_id for item in self.hypotheses],
            "eligible_records": self.eligible_records,
            "rejection_counts": dict(self.rejection_counts),
            "source_evidence_count": len(self.source_evidence_ids),
            "source_evidence_fingerprint": canonical_sha256(sorted(self.source_evidence_ids)),
            "request": self.request.to_dict(),
        }


@dataclass(frozen=True)
class PatternEvaluationPersistResult:
    result: PatternEvaluationResult
    persisted_prediction_ids: tuple[str, ...]
    persisted_occurrence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.result.to_dict()
        payload.update(
            {
                "apply": True,
                "persisted_prediction_ids": list(self.persisted_prediction_ids),
                "persisted_occurrence_ids": list(self.persisted_occurrence_ids),
            }
        )
        return payload


__all__ = (
    "PatternEvaluationMatch",
    "PatternEvaluationPersistResult",
    "PatternEvaluationRequest",
    "PatternEvaluationResult",
    "PatternEvaluationScanRequest",
)
