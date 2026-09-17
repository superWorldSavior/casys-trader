"""Typed pattern-hypothesis and occurrence aggregates. Stdlib/domain only.

A formulated chain is a ``PatternHypothesis`` with a bounded register/start/close
/invalidate journal. Each application at a cutoff is a separate
``PatternOccurrence`` that links canonical ``WorldOutcome`` leaves without copying
labels. Discovery and prospective confirmation stay distinct datasets.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, NamedTuple

from trader.domain.world_driver import (
    DriverState,
    driver_state_required_for_relation,
)
from trader.domain.world_episode import (
    PREDICTION_CLASSES,
    WorldOutcome,
    WorldPrediction,
    canonical_sha256,
    parse_utc_timestamp,
)
from trader.domain.world_family_catalog import normalize_family_id
from trader.domain.world_graph import (
    GRAPH_PATH_NODE_KINDS,
    GRAPH_TRAVERSAL_V1_DIRECTIONS,
    WORLD_ENTITY_KINDS,
    PatternHypothesisRef,
    WorldEntityRef,
    validate_id_free_traversal_hop,
)


PATTERN_HYPOTHESIS_SCHEMA = "pattern_hypothesis.v1"
PATTERN_OCCURRENCE_SCHEMA = "pattern_occurrence.v1"
PATTERN_OUTCOME_LINK_SCHEMA = "pattern_outcome_link.v1"
PATTERN_HYPOTHESIS_EVENT_SCHEMA = "pattern_hypothesis_event.v1"
PATTERN_OCCURRENCE_EVENT_SCHEMA = "pattern_occurrence_event.v1"
PATTERN_DISCOVERY_COMPLETED_SCHEMA = "pattern_discovery_completed.v1"
PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE = "pattern_discovery_completed"

PATTERN_HYPOTHESIS_STATUSES = frozenset({"registered", "evaluating", "evaluation_closed", "invalidated"})
PATTERN_OCCURRENCE_STATUSES = frozenset({"recorded", "invalidated"})
PATTERN_HYPOTHESIS_EVENT_TYPES = frozenset(
    {
        "pattern_hypothesis_registered",
        "pattern_evaluation_started",
        "pattern_evaluation_closed",
        "pattern_hypothesis_invalidated",
    }
)
PATTERN_OCCURRENCE_EVENT_TYPES = frozenset(
    {
        "pattern_occurrence_recorded",
        "pattern_outcome_linked",
        "pattern_outcome_link_superseded",
        "pattern_occurrence_invalidated",
    }
)
PATTERN_PATH_DIRECTIONS = frozenset({"forward", "reverse"})
PATTERN_FRESHNESS_BUCKETS = frozenset({"0-4h", "4-24h", "1-7d", "older"})
PATTERN_GRAPH_NODE_KINDS = GRAPH_PATH_NODE_KINDS
PATTERN_ASSOCIATION_METRIC = "total_variation.v1"
EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY = "explicit_graph_pattern.v1"
PATTERN_EVALUATION_HORIZON_IDS = ("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1")
_HYPOTHESIS_TERMINAL = frozenset({"evaluation_closed", "invalidated"})
_MAX_PATTERN_STEPS = 8
_ASSOCIATION_ABS_TOL = 1e-12
_DISTRIBUTION_ABS_TOL = 1e-12

_HYPOTHESIS_ID_PREFIX = "pattern_hypothesis:v1"
_OCCURRENCE_ID_PREFIX = "pattern_occurrence:v1"
_LINK_ID_PREFIX = "pattern_outcome_link:v1"
_HYPOTHESIS_EVENT_PREFIX = "pattern_hypothesis_event:v1"
_OCCURRENCE_EVENT_PREFIX = "pattern_occurrence_event:v1"
_DISCOVERY_COMPLETED_PREFIX = "pattern_discovery_completed:v1"
_WORLD_COHORT_ID_PREFIX = "world_cohort:v1"
_WORLD_COHORT_EVENT_PREFIX = "world_cohort_event:v1"
_OUTCOME_EVENT_PREFIX = "world-outcome:v1"
_PREDICTION_ID_PREFIX = "world-prediction:v1"

_FORBIDDEN_OUTCOME_LABEL_KEYS = frozenset(
    {
        "move_class",
        "direction",
        "simple_return",
        "anchor_close",
        "endpoint_close",
        "target_at",
        "available_at",
        "computed_at",
        "endpoint_bar_ts",
        "training_eligible",
        "status",
        "label",
        "ready_at",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


def _validate_prefixed_id(value: Any, prefix: str, field_name: str) -> str:
    text = _required_text(value, field_name)
    expected = f"{prefix}:"
    if not text.startswith(expected):
        raise ValueError(f"{field_name} must start with '{expected}'")
    _sha256_hex(text[len(expected) :], field_name)
    return text


def _prefixed_id(prefix: str, payload: Any) -> str:
    return f"{prefix}:{canonical_sha256(payload)}"


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    return tuple(_required_text(item, f"{field_name}[]") for item in value)


def _unique_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    items = _immutable_text_tuple(value, field_name)
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


def _selected_hypothesis_ids(value: Sequence[Any] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("selected_hypothesis_ids must be a sequence of PatternHypothesisId")
    items = tuple(
        item.value if isinstance(item, PatternHypothesisId) else PatternHypothesisId(item).value for item in value
    )
    if len(items) != len(set(items)):
        raise ValueError("selected_hypothesis_ids must not contain duplicates")
    return items


def _entity_kind(value: Any, field_name: str) -> str:
    kind = _required_text(value, field_name).lower()
    if kind not in WORLD_ENTITY_KINDS:
        allowed = ", ".join(sorted(WORLD_ENTITY_KINDS))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return kind


def _freshness_bucket(value: Any) -> str:
    bucket = _required_text(value, "freshness_bucket")
    if bucket not in PATTERN_FRESHNESS_BUCKETS:
        allowed = ", ".join(sorted(PATTERN_FRESHNESS_BUCKETS))
        raise ValueError(f"freshness_bucket must be one of: {allowed}")
    return bucket


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a positive integer")
    if value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _finite_positive(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number greater than 0")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field_name} must be a finite number greater than 0")
    return number


def _class_counts(value: Any, field_name: str, *, expected_total: int) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping keyed by DOWN, FLAT, and UP")
    if set(value) != set(PREDICTION_CLASSES):
        raise ValueError(f"{field_name} must contain exactly DOWN, FLAT, and UP")
    counts: dict[str, int] = {}
    for label in PREDICTION_CLASSES:
        counts[label] = _non_negative_int(value[label], f"{field_name}.{label}")
    if sum(counts.values()) != expected_total:
        raise ValueError(f"{field_name} must sum to {expected_total}")
    return MappingProxyType(counts)


def laplace_smoothed_distribution(counts: Mapping[str, int], *, alpha: float) -> Mapping[str, float]:
    total = sum(int(counts[label]) for label in PREDICTION_CLASSES)
    denominator = total + (alpha * len(PREDICTION_CLASSES))
    return MappingProxyType({label: (float(counts[label]) + alpha) / denominator for label in PREDICTION_CLASSES})


def total_variation_distance(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float:
    return 0.5 * sum(abs(float(left[label]) - float(right[label])) for label in PREDICTION_CLASSES)


def _move_distribution(value: Any) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError("move_distribution must be a mapping keyed by DOWN, FLAT, and UP")
    if set(value) != set(PREDICTION_CLASSES):
        raise ValueError("move_distribution must contain exactly DOWN, FLAT, and UP")
    normalized: dict[str, float] = {}
    for label in PREDICTION_CLASSES:
        raw = value[label]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError(f"move_distribution {label} must be a finite number")
        probability = float(raw)
        if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise ValueError(f"move_distribution {label} must be in [0, 1]")
        normalized[label] = probability
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-9):
        raise ValueError("move_distribution must sum to 1")
    return MappingProxyType(normalized)


def _set_event_id(event: Any, prefix: str, payload: Mapping[str, Any], *, schema_version: str) -> None:
    provided_schema = _required_text(getattr(event, "schema_version"), "schema_version")
    if provided_schema != schema_version:
        raise ValueError(f"schema_version must be {schema_version}")
    computed = _prefixed_id(prefix, payload)
    provided = getattr(event, "event_id")
    if provided is not None and _required_text(provided, "event_id") != computed:
        raise ValueError("event_id does not match the canonical event identity")
    object.__setattr__(event, "schema_version", provided_schema)
    object.__setattr__(event, "event_id", computed)


def _as_entity(value: WorldEntityRef | Mapping[str, Any], field_name: str) -> WorldEntityRef:
    if isinstance(value, WorldEntityRef):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be WorldEntityRef or a mapping")
    return WorldEntityRef.from_mapping(value)


@dataclass(frozen=True)
class PatternHypothesisId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _HYPOTHESIS_ID_PREFIX, "hypothesis_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PatternOccurrenceId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _OCCURRENCE_ID_PREFIX, "occurrence_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PatternHypothesisEventId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _HYPOTHESIS_EVENT_PREFIX, "event_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PatternOccurrenceEventId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _OCCURRENCE_EVENT_PREFIX, "event_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PatternOutcomeLinkId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _LINK_ID_PREFIX, "link_id"))

    def __str__(self) -> str:
        return self.value


class _PatternHopIdentity(NamedTuple):
    ordinal: int
    source_kind: str
    relation_kind: str
    direction: str
    target_kind: str
    freshness_bucket: str
    evidence_rule_version: str
    driver_state: tuple[object, ...] | None
    family_ref: str | None


def _resolved_hop_driver_state(relation_kind: str, driver_state: Any) -> DriverState | None:
    if driver_state_required_for_relation(relation_kind):
        if driver_state is None:
            raise ValueError("OBSERVES/ABOUT hops require a DriverState")
        if isinstance(driver_state, DriverState):
            return driver_state
        if isinstance(driver_state, Mapping):
            return DriverState.from_mapping(driver_state)
        raise TypeError("driver_state must be DriverState or a mapping")
    if driver_state is not None:
        raise ValueError("structural hops require driver_state=None")
    return None


def _pattern_hop_validated(
    *,
    ordinal: Any,
    source_kind: Any,
    relation_kind: Any,
    direction: Any,
    target_kind: Any,
    freshness_bucket: Any,
    evidence_rule_version: Any,
    driver_state: Any = None,
    family_ref: Any = None,
) -> tuple[_PatternHopIdentity, DriverState | None]:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("ordinal must be an integer")
    if ordinal < 0:
        raise ValueError("ordinal must be non-negative")
    hop = validate_id_free_traversal_hop(
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
    )
    resolved = _resolved_hop_driver_state(hop.relation_kind, driver_state)
    if hop.relation_kind == "MEMBER_OF_FAMILY":
        if family_ref is None:
            raise ValueError("MEMBER_OF_FAMILY hops require family_ref")
        resolved_family: str | None = normalize_family_id(family_ref)
    else:
        if family_ref is not None:
            raise ValueError("only MEMBER_OF_FAMILY hops carry family_ref")
        resolved_family = None
    identity = _PatternHopIdentity(
        ordinal=ordinal,
        source_kind=hop.source_kind,
        relation_kind=hop.relation_kind,
        direction=hop.direction,
        target_kind=hop.target_kind,
        freshness_bucket=_freshness_bucket(freshness_bucket),
        evidence_rule_version=_required_text(evidence_rule_version, "evidence_rule_version"),
        driver_state=None if resolved is None else resolved.identity_tuple(),
        family_ref=resolved_family,
    )
    return identity, resolved


def _pattern_hop_identity(
    *,
    ordinal: Any,
    source_kind: Any,
    relation_kind: Any,
    direction: Any,
    target_kind: Any,
    freshness_bucket: Any,
    evidence_rule_version: Any,
    driver_state: Any = None,
    family_ref: Any = None,
) -> _PatternHopIdentity:
    return _pattern_hop_validated(
        ordinal=ordinal,
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
        freshness_bucket=freshness_bucket,
        evidence_rule_version=evidence_rule_version,
        driver_state=driver_state,
        family_ref=family_ref,
    )[0]


def _pattern_hop_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if "predicate" in value or "lag_window" in value or "subject_kind" in value or "object_kind" in value:
        raise ValueError(
            "pattern hops are an ID-free WorldTemporalPathStep projection; predicate and lag_window are gone"
        )
    if driver_state_required_for_relation(value.get("relation_kind")) and "driver_state" not in value:
        raise ValueError("overlay hop mapping is missing driver_state")
    return {
        "ordinal": value.get("ordinal"),
        "source_kind": value.get("source_kind"),
        "relation_kind": value.get("relation_kind"),
        "direction": value.get("direction"),
        "target_kind": value.get("target_kind"),
        "freshness_bucket": value.get("freshness_bucket"),
        "evidence_rule_version": value.get("evidence_rule_version"),
        "driver_state": value.get("driver_state"),
        "family_ref": value.get("family_ref"),
    }


@dataclass(frozen=True)
class PatternStep:
    """ID-free WorldTemporalPathStep projection. Never inserted as a factual graph edge."""

    ordinal: int
    source_kind: str
    relation_kind: str
    direction: str
    target_kind: str
    freshness_bucket: str
    evidence_rule_version: str
    driver_state: DriverState | Mapping[str, Any] | None = None
    family_ref: str | None = None

    def __post_init__(self) -> None:
        hop, resolved = _pattern_hop_validated(
            ordinal=self.ordinal,
            source_kind=self.source_kind,
            relation_kind=self.relation_kind,
            direction=self.direction,
            target_kind=self.target_kind,
            freshness_bucket=self.freshness_bucket,
            evidence_rule_version=self.evidence_rule_version,
            driver_state=self.driver_state,
            family_ref=self.family_ref,
        )
        object.__setattr__(self, "ordinal", hop.ordinal)
        object.__setattr__(self, "source_kind", hop.source_kind)
        object.__setattr__(self, "relation_kind", hop.relation_kind)
        object.__setattr__(self, "direction", hop.direction)
        object.__setattr__(self, "target_kind", hop.target_kind)
        object.__setattr__(self, "freshness_bucket", hop.freshness_bucket)
        object.__setattr__(self, "evidence_rule_version", hop.evidence_rule_version)
        object.__setattr__(self, "driver_state", resolved)
        object.__setattr__(self, "family_ref", hop.family_ref)

    def identity_tuple(self) -> tuple[object, ...]:
        return _pattern_hop_identity(
            ordinal=self.ordinal,
            source_kind=self.source_kind,
            relation_kind=self.relation_kind,
            direction=self.direction,
            target_kind=self.target_kind,
            freshness_bucket=self.freshness_bucket,
            evidence_rule_version=self.evidence_rule_version,
            driver_state=self.driver_state,
            family_ref=self.family_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        # Canonical bytes feed hypothesis_id: a field added later must stay
        # absent when None, otherwise every stored hypothesis becomes
        # unreadable (2026-09-17: family_ref broke the 2026-08-28 ledger).
        payload = {
            "ordinal": self.ordinal,
            "source_kind": self.source_kind,
            "relation_kind": self.relation_kind,
            "direction": self.direction,
            "target_kind": self.target_kind,
            "freshness_bucket": self.freshness_bucket,
            "evidence_rule_version": self.evidence_rule_version,
            "driver_state": None if self.driver_state is None else self.driver_state.to_dict(),
        }
        if self.family_ref is not None:
            payload["family_ref"] = self.family_ref
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternStep) -> PatternStep:
        if isinstance(value, PatternStep):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern step must be PatternStep or a mapping")
        return cls(**_pattern_hop_mapping(value))


def _steps_tuple(value: Sequence[Any] | None) -> tuple[PatternStep, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("steps must be a sequence of PatternStep")
    steps = tuple(item if isinstance(item, PatternStep) else PatternStep.from_mapping(item) for item in value)
    if not steps:
        raise ValueError("steps must not be empty")
    if len(steps) > _MAX_PATTERN_STEPS:
        raise ValueError(f"steps are bounded to {_MAX_PATTERN_STEPS}")
    for index, step in enumerate(steps):
        if step.ordinal != index:
            raise ValueError("steps must be ordered with consecutive ordinals starting at 0")
    return steps


@dataclass(frozen=True)
class PatternTarget:
    entity_kind: str
    horizon_id: str
    move_distribution: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_kind", _entity_kind(self.entity_kind, "entity_kind"))
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))
        object.__setattr__(self, "move_distribution", _move_distribution(self.move_distribution))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_kind": self.entity_kind,
            "horizon_id": self.horizon_id,
            "move_distribution": dict(self.move_distribution),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternTarget) -> PatternTarget:
        if isinstance(value, PatternTarget):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern target must be PatternTarget or a mapping")
        return cls(
            entity_kind=value.get("entity_kind"),
            horizon_id=value.get("horizon_id"),
            move_distribution=value.get("move_distribution") or {},
        )


@dataclass(frozen=True)
class PatternFormationStats:
    """Persisted Laplace/TV formation evidence. Counts are unique time-specific anchors."""

    support: int
    population_support: int
    class_counts: Mapping[str, Any]
    population_class_counts: Mapping[str, Any]
    smoothing_alpha: float
    association_metric: str
    association_score: float
    pattern_distribution: Mapping[str, Any] | None = None
    population_distribution: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        support = _positive_int(self.support, "support")
        population_support = _positive_int(self.population_support, "population_support")
        if population_support < support:
            raise ValueError("population_support must be greater than or equal to support")
        class_counts = _class_counts(self.class_counts, "class_counts", expected_total=support)
        population_class_counts = _class_counts(
            self.population_class_counts, "population_class_counts", expected_total=population_support
        )
        alpha = _finite_positive(self.smoothing_alpha, "smoothing_alpha")
        metric = _required_text(self.association_metric, "association_metric")
        if metric != PATTERN_ASSOCIATION_METRIC:
            raise ValueError(f"association_metric must be {PATTERN_ASSOCIATION_METRIC}")
        if isinstance(self.association_score, bool) or not isinstance(self.association_score, (int, float)):
            raise TypeError("association_score must be a finite number in [0, 1]")
        score = float(self.association_score)
        if not math.isfinite(score) or score < 0.0 or score > 1.0:
            raise ValueError("association_score must be in [0, 1]")
        pattern_distribution = laplace_smoothed_distribution(class_counts, alpha=alpha)
        population_distribution = laplace_smoothed_distribution(population_class_counts, alpha=alpha)
        expected_score = total_variation_distance(pattern_distribution, population_distribution)
        if not math.isclose(score, expected_score, rel_tol=0.0, abs_tol=_ASSOCIATION_ABS_TOL):
            raise ValueError("association_score must equal 0.5 * sum(|pattern - population|)")
        if self.pattern_distribution is not None:
            provided_pattern = _move_distribution(self.pattern_distribution)
            if any(
                not math.isclose(
                    provided_pattern[label], pattern_distribution[label], rel_tol=0.0, abs_tol=_DISTRIBUTION_ABS_TOL
                )
                for label in PREDICTION_CLASSES
            ):
                raise ValueError("pattern_distribution must equal the Laplace-smoothed class_counts")
        if self.population_distribution is not None:
            provided_population = _move_distribution(self.population_distribution)
            if any(
                not math.isclose(
                    provided_population[label],
                    population_distribution[label],
                    rel_tol=0.0,
                    abs_tol=_DISTRIBUTION_ABS_TOL,
                )
                for label in PREDICTION_CLASSES
            ):
                raise ValueError("population_distribution must equal the Laplace-smoothed population_class_counts")
        object.__setattr__(self, "support", support)
        object.__setattr__(self, "population_support", population_support)
        object.__setattr__(self, "class_counts", class_counts)
        object.__setattr__(self, "population_class_counts", population_class_counts)
        object.__setattr__(self, "smoothing_alpha", alpha)
        object.__setattr__(self, "association_metric", metric)
        object.__setattr__(self, "association_score", score)
        object.__setattr__(self, "pattern_distribution", pattern_distribution)
        object.__setattr__(self, "population_distribution", population_distribution)

    def to_dict(self) -> dict[str, Any]:
        return {
            "support": self.support,
            "population_support": self.population_support,
            "class_counts": {label: self.class_counts[label] for label in PREDICTION_CLASSES},
            "population_class_counts": {label: self.population_class_counts[label] for label in PREDICTION_CLASSES},
            "smoothing_alpha": self.smoothing_alpha,
            "association_metric": self.association_metric,
            "association_score": self.association_score,
            "pattern_distribution": {label: self.pattern_distribution[label] for label in PREDICTION_CLASSES},
            "population_distribution": {label: self.population_distribution[label] for label in PREDICTION_CLASSES},
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternFormationStats) -> PatternFormationStats:
        if isinstance(value, PatternFormationStats):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern formation stats must be PatternFormationStats or a mapping")
        return cls(
            support=value.get("support"),
            population_support=value.get("population_support"),
            class_counts=value.get("class_counts") or {},
            population_class_counts=value.get("population_class_counts") or {},
            smoothing_alpha=value.get("smoothing_alpha"),
            association_metric=value.get("association_metric"),
            association_score=value.get("association_score"),
            pattern_distribution=value.get("pattern_distribution"),
            population_distribution=value.get("population_distribution"),
        )


@dataclass(frozen=True)
class PatternHypothesisSpec:
    """Immutable formulated chain. Identity changes if the chain or evaluation contract changes."""

    evaluation_start_not_before: datetime | str
    target: PatternTarget | Mapping[str, Any]
    steps: Sequence[PatternStep | Mapping[str, Any]]
    formation_cutoff: datetime | str
    formation_dataset_fingerprint: str
    feature_contract_id: str
    feature_contract_fingerprint: str
    feature_mask_id: str
    feature_mask_fingerprint: str
    model_identity: str
    ontology_revision: str
    stats: PatternFormationStats | Mapping[str, Any]
    source_refs: Sequence[str] = ()
    causal_claim: bool = False
    schema_version: str = PATTERN_HYPOTHESIS_SCHEMA
    hypothesis_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != PATTERN_HYPOTHESIS_SCHEMA:
            raise ValueError(f"schema_version must be {PATTERN_HYPOTHESIS_SCHEMA}")
        evaluation_start_not_before = parse_utc_timestamp(
            self.evaluation_start_not_before, "evaluation_start_not_before"
        )
        formation_cutoff = parse_utc_timestamp(self.formation_cutoff, "formation_cutoff")
        if not (formation_cutoff < evaluation_start_not_before):
            raise ValueError("formation cutoff must precede evaluation")
        target = self.target if isinstance(self.target, PatternTarget) else PatternTarget.from_mapping(self.target)
        steps = _steps_tuple(self.steps)
        stats = (
            self.stats
            if isinstance(self.stats, PatternFormationStats)
            else PatternFormationStats.from_mapping(self.stats)
        )
        if any(
            not math.isclose(
                float(target.move_distribution[label]),
                float(stats.pattern_distribution[label]),
                rel_tol=0.0,
                abs_tol=_DISTRIBUTION_ABS_TOL,
            )
            for label in PREDICTION_CLASSES
        ):
            raise ValueError("target.move_distribution must equal the Laplace-smoothed pattern distribution")
        if self.causal_claim is not False:
            raise ValueError("causal_claim must remain false")
        identity = {
            "schema_version": schema_version,
            "evaluation_start_not_before": _iso(evaluation_start_not_before),
            "target": target.to_dict(),
            "steps": [step.to_dict() for step in steps],
            "formation_cutoff": _iso(formation_cutoff),
            "formation_dataset_fingerprint": _sha256_hex(
                self.formation_dataset_fingerprint, "formation_dataset_fingerprint"
            ),
            "feature_contract_id": _required_text(self.feature_contract_id, "feature_contract_id"),
            "feature_contract_fingerprint": _sha256_hex(
                self.feature_contract_fingerprint, "feature_contract_fingerprint"
            ),
            "feature_mask_id": _required_text(self.feature_mask_id, "feature_mask_id"),
            "feature_mask_fingerprint": _sha256_hex(self.feature_mask_fingerprint, "feature_mask_fingerprint"),
            "model_identity": _required_text(self.model_identity, "model_identity"),
            "ontology_revision": _required_text(self.ontology_revision, "ontology_revision"),
            "stats": stats.to_dict(),
        }
        hypothesis_id = _prefixed_id(_HYPOTHESIS_ID_PREFIX, identity)
        if self.hypothesis_id is not None and _required_text(self.hypothesis_id, "hypothesis_id") != hypothesis_id:
            raise ValueError("hypothesis_id does not match the canonical pattern hypothesis")
        PatternHypothesisRef(hypothesis_id=hypothesis_id)
        source_refs = _unique_text_tuple(self.source_refs, "source_refs")
        content_payload = {
            **identity,
            "hypothesis_id": hypothesis_id,
            "source_refs": list(source_refs),
            "causal_claim": False,
        }
        digest = canonical_sha256(content_payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical pattern hypothesis")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start_not_before)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "formation_cutoff", formation_cutoff)
        object.__setattr__(self, "formation_dataset_fingerprint", identity["formation_dataset_fingerprint"])
        object.__setattr__(self, "feature_contract_id", identity["feature_contract_id"])
        object.__setattr__(self, "feature_contract_fingerprint", identity["feature_contract_fingerprint"])
        object.__setattr__(self, "feature_mask_id", identity["feature_mask_id"])
        object.__setattr__(self, "feature_mask_fingerprint", identity["feature_mask_fingerprint"])
        object.__setattr__(self, "model_identity", identity["model_identity"])
        object.__setattr__(self, "ontology_revision", identity["ontology_revision"])
        object.__setattr__(self, "stats", stats)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "causal_claim", False)
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "evaluation_start_not_before": _iso(self.evaluation_start_not_before),
            "target": self.target.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "formation_cutoff": _iso(self.formation_cutoff),
            "formation_dataset_fingerprint": self.formation_dataset_fingerprint,
            "feature_contract_id": self.feature_contract_id,
            "feature_contract_fingerprint": self.feature_contract_fingerprint,
            "feature_mask_id": self.feature_mask_id,
            "feature_mask_fingerprint": self.feature_mask_fingerprint,
            "model_identity": self.model_identity,
            "ontology_revision": self.ontology_revision,
            "stats": self.stats.to_dict(),
            "source_refs": list(self.source_refs),
            "causal_claim": self.causal_claim,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternHypothesisSpec) -> PatternHypothesisSpec:
        if isinstance(value, PatternHypothesisSpec):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern hypothesis spec must be PatternHypothesisSpec or a mapping")
        return cls(
            evaluation_start_not_before=value.get("evaluation_start_not_before"),
            target=value.get("target") or {},
            steps=value.get("steps") or (),
            formation_cutoff=value.get("formation_cutoff"),
            formation_dataset_fingerprint=value.get("formation_dataset_fingerprint"),
            feature_contract_id=value.get("feature_contract_id"),
            feature_contract_fingerprint=value.get("feature_contract_fingerprint"),
            feature_mask_id=value.get("feature_mask_id"),
            feature_mask_fingerprint=value.get("feature_mask_fingerprint"),
            model_identity=value.get("model_identity"),
            ontology_revision=value.get("ontology_revision"),
            stats=value.get("stats") or {},
            source_refs=value.get("source_refs") or (),
            causal_claim=False if value.get("causal_claim") is None else value.get("causal_claim"),
            schema_version=value.get("schema_version", PATTERN_HYPOTHESIS_SCHEMA),
            hypothesis_id=value.get("hypothesis_id"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class PatternHypothesisRegistered:
    spec: PatternHypothesisSpec | Mapping[str, Any]
    registered_at: datetime | str
    event_id: str | None = None
    schema_version: str = PATTERN_HYPOTHESIS_EVENT_SCHEMA
    event_type: str = "pattern_hypothesis_registered"

    def __post_init__(self) -> None:
        spec = (
            self.spec if isinstance(self.spec, PatternHypothesisSpec) else PatternHypothesisSpec.from_mapping(self.spec)
        )
        registered_at = parse_utc_timestamp(self.registered_at, "registered_at")
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "registered_at", registered_at)
        object.__setattr__(self, "event_type", "pattern_hypothesis_registered")
        _set_event_id(
            self,
            _HYPOTHESIS_EVENT_PREFIX,
            {
                "event_type": "pattern_hypothesis_registered",
                "schema_version": PATTERN_HYPOTHESIS_EVENT_SCHEMA,
                "hypothesis_id": spec.hypothesis_id,
                "content_sha256": spec.content_sha256,
                "registered_at": _iso(registered_at),
            },
            schema_version=PATTERN_HYPOTHESIS_EVENT_SCHEMA,
        )

    @property
    def hypothesis_id(self) -> str:
        return self.spec.hypothesis_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "hypothesis_id": self.hypothesis_id,
            "registered_at": _iso(self.registered_at),
            "spec": self.spec.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternHypothesisRegistered) -> PatternHypothesisRegistered:
        if isinstance(value, PatternHypothesisRegistered):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("registered event must be a mapping")
        spec = value.get("spec") or value
        return cls(
            spec=spec,
            registered_at=value.get("registered_at"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_HYPOTHESIS_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternEvaluationStarted:
    hypothesis_id: str
    evaluation_cohort_id: str
    started_at: datetime | str
    evaluation_dataset_fingerprint: str
    event_id: str | None = None
    schema_version: str = PATTERN_HYPOTHESIS_EVENT_SCHEMA
    event_type: str = "pattern_evaluation_started"

    def __post_init__(self) -> None:
        hypothesis_id = PatternHypothesisId(self.hypothesis_id).value
        evaluation_cohort_id = _required_text(self.evaluation_cohort_id, "evaluation_cohort_id")
        started_at = parse_utc_timestamp(self.started_at, "started_at")
        fingerprint = _sha256_hex(self.evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint")
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "evaluation_cohort_id", evaluation_cohort_id)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "evaluation_dataset_fingerprint", fingerprint)
        object.__setattr__(self, "event_type", "pattern_evaluation_started")
        _set_event_id(
            self,
            _HYPOTHESIS_EVENT_PREFIX,
            {
                "event_type": "pattern_evaluation_started",
                "schema_version": PATTERN_HYPOTHESIS_EVENT_SCHEMA,
                "hypothesis_id": hypothesis_id,
                "evaluation_cohort_id": evaluation_cohort_id,
                "started_at": _iso(started_at),
                "evaluation_dataset_fingerprint": fingerprint,
            },
            schema_version=PATTERN_HYPOTHESIS_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "hypothesis_id": self.hypothesis_id,
            "evaluation_cohort_id": self.evaluation_cohort_id,
            "started_at": _iso(self.started_at),
            "evaluation_dataset_fingerprint": self.evaluation_dataset_fingerprint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternEvaluationStarted) -> PatternEvaluationStarted:
        if isinstance(value, PatternEvaluationStarted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("evaluation started event must be a mapping")
        return cls(
            hypothesis_id=value.get("hypothesis_id"),
            evaluation_cohort_id=value.get("evaluation_cohort_id"),
            started_at=value.get("started_at"),
            evaluation_dataset_fingerprint=value.get("evaluation_dataset_fingerprint"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_HYPOTHESIS_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternEvaluationClosed:
    hypothesis_id: str
    closed_at: datetime | str
    event_id: str | None = None
    schema_version: str = PATTERN_HYPOTHESIS_EVENT_SCHEMA
    event_type: str = "pattern_evaluation_closed"

    def __post_init__(self) -> None:
        hypothesis_id = PatternHypothesisId(self.hypothesis_id).value
        closed_at = parse_utc_timestamp(self.closed_at, "closed_at")
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "closed_at", closed_at)
        object.__setattr__(self, "event_type", "pattern_evaluation_closed")
        _set_event_id(
            self,
            _HYPOTHESIS_EVENT_PREFIX,
            {
                "event_type": "pattern_evaluation_closed",
                "schema_version": PATTERN_HYPOTHESIS_EVENT_SCHEMA,
                "hypothesis_id": hypothesis_id,
                "closed_at": _iso(closed_at),
            },
            schema_version=PATTERN_HYPOTHESIS_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "hypothesis_id": self.hypothesis_id,
            "closed_at": _iso(self.closed_at),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternEvaluationClosed) -> PatternEvaluationClosed:
        if isinstance(value, PatternEvaluationClosed):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("evaluation closed event must be a mapping")
        return cls(
            hypothesis_id=value.get("hypothesis_id"),
            closed_at=value.get("closed_at"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_HYPOTHESIS_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternHypothesisInvalidated:
    hypothesis_id: str
    invalidated_at: datetime | str
    reason: str
    event_id: str | None = None
    schema_version: str = PATTERN_HYPOTHESIS_EVENT_SCHEMA
    event_type: str = "pattern_hypothesis_invalidated"

    def __post_init__(self) -> None:
        hypothesis_id = PatternHypothesisId(self.hypothesis_id).value
        invalidated_at = parse_utc_timestamp(self.invalidated_at, "invalidated_at")
        reason = _required_text(self.reason, "reason")
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "invalidated_at", invalidated_at)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "event_type", "pattern_hypothesis_invalidated")
        _set_event_id(
            self,
            _HYPOTHESIS_EVENT_PREFIX,
            {
                "event_type": "pattern_hypothesis_invalidated",
                "schema_version": PATTERN_HYPOTHESIS_EVENT_SCHEMA,
                "hypothesis_id": hypothesis_id,
                "invalidated_at": _iso(invalidated_at),
                "reason": reason,
            },
            schema_version=PATTERN_HYPOTHESIS_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "hypothesis_id": self.hypothesis_id,
            "invalidated_at": _iso(self.invalidated_at),
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternHypothesisInvalidated) -> PatternHypothesisInvalidated:
        if isinstance(value, PatternHypothesisInvalidated):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("hypothesis invalidated event must be a mapping")
        return cls(
            hypothesis_id=value.get("hypothesis_id"),
            invalidated_at=value.get("invalidated_at"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_HYPOTHESIS_EVENT_SCHEMA),
        )


PatternHypothesisEvent = (
    PatternHypothesisRegistered | PatternEvaluationStarted | PatternEvaluationClosed | PatternHypothesisInvalidated
)
_HYPOTHESIS_EVENT_CLASSES = (
    PatternHypothesisRegistered,
    PatternEvaluationStarted,
    PatternEvaluationClosed,
    PatternHypothesisInvalidated,
)
_HYPOTHESIS_EVENT_PARSERS = {
    "pattern_hypothesis_registered": PatternHypothesisRegistered.from_mapping,
    "pattern_evaluation_started": PatternEvaluationStarted.from_mapping,
    "pattern_evaluation_closed": PatternEvaluationClosed.from_mapping,
    "pattern_hypothesis_invalidated": PatternHypothesisInvalidated.from_mapping,
}


def parse_pattern_hypothesis_event(value: Mapping[str, Any] | PatternHypothesisEvent) -> PatternHypothesisEvent:
    if isinstance(value, _HYPOTHESIS_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("pattern hypothesis event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _HYPOTHESIS_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown pattern hypothesis event_type: {event_type}") from exc
    return parser(value)


def _fold_hypothesis_events(events: Sequence[PatternHypothesisEvent]) -> str:
    if not events:
        raise ValueError("PatternHypothesis requires a registered event")
    if not isinstance(events[0], PatternHypothesisRegistered):
        raise ValueError("first event must be a registered hypothesis event")
    registered = events[0]
    status = "registered"
    for event in events[1:]:
        if event.hypothesis_id != registered.hypothesis_id:
            raise ValueError("event hypothesis_id mismatch")
        if status in _HYPOTHESIS_TERMINAL:
            raise ValueError("no events are allowed after a terminal hypothesis status")
        if isinstance(event, PatternHypothesisRegistered):
            raise ValueError("duplicate registered event")
        if isinstance(event, PatternEvaluationStarted):
            if status != "registered":
                raise ValueError("evaluation can only start from registered")
            if event.evaluation_dataset_fingerprint == registered.spec.formation_dataset_fingerprint:
                raise ValueError("in-sample confirmation is forbidden: evaluation dataset must differ from formation")
            if event.started_at < registered.spec.evaluation_start_not_before:
                raise ValueError("evaluation cannot start before evaluation_start_not_before")
            status = "evaluating"
            continue
        if isinstance(event, PatternEvaluationClosed):
            if status != "evaluating":
                raise ValueError("evaluation can only close from evaluating")
            status = "evaluation_closed"
            continue
        if isinstance(event, PatternHypothesisInvalidated):
            if status not in {"registered", "evaluating"}:
                raise ValueError("hypothesis can only be invalidated from registered or evaluating")
            status = "invalidated"
            continue
        raise TypeError(f"unsupported hypothesis event: {type(event).__name__}")
    return status


@dataclass(frozen=True)
class PatternHypothesis:
    """Event-sourced formulated chain. Status is derived; occurrences are never loaded."""

    events: Sequence[PatternHypothesisEvent | Mapping[str, Any]]

    def __post_init__(self) -> None:
        events = tuple(parse_pattern_hypothesis_event(item) for item in self.events)
        _fold_hypothesis_events(events)
        object.__setattr__(self, "events", events)

    @classmethod
    def register(
        cls,
        spec: PatternHypothesisSpec | Mapping[str, Any],
        *,
        registered_at: datetime | str,
    ) -> PatternHypothesis:
        return cls(events=(PatternHypothesisRegistered(spec=spec, registered_at=registered_at),))

    @classmethod
    def from_events(cls, events: Sequence[PatternHypothesisEvent | Mapping[str, Any]]) -> PatternHypothesis:
        return cls(events=events)

    @property
    def registered(self) -> PatternHypothesisRegistered:
        event = self.events[0]
        assert isinstance(event, PatternHypothesisRegistered)
        return event

    @property
    def spec(self) -> PatternHypothesisSpec:
        return self.registered.spec

    @property
    def hypothesis_id(self) -> str:
        return self.spec.hypothesis_id

    @property
    def status(self) -> str:
        last = self.events[-1]
        if isinstance(last, PatternEvaluationClosed):
            return "evaluation_closed"
        if isinstance(last, PatternHypothesisInvalidated):
            return "invalidated"
        if isinstance(last, PatternEvaluationStarted) or any(
            isinstance(event, PatternEvaluationStarted) for event in self.events
        ):
            return "evaluating"
        return "registered"

    @property
    def evaluation_dataset_fingerprint(self) -> str | None:
        for event in reversed(self.events):
            if isinstance(event, PatternEvaluationStarted):
                return event.evaluation_dataset_fingerprint
        return None

    @property
    def evaluation_cohort_id(self) -> str | None:
        for event in reversed(self.events):
            if isinstance(event, PatternEvaluationStarted):
                return event.evaluation_cohort_id
        return None

    def start_evaluation(
        self,
        *,
        started_at: datetime | str,
        evaluation_dataset_fingerprint: str,
        evaluation_cohort_id: str,
    ) -> PatternHypothesis:
        event = PatternEvaluationStarted(
            hypothesis_id=self.hypothesis_id,
            evaluation_cohort_id=evaluation_cohort_id,
            started_at=started_at,
            evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,
        )
        if event.evaluation_dataset_fingerprint == self.spec.formation_dataset_fingerprint:
            raise ValueError("in-sample confirmation is forbidden: evaluation dataset must differ from formation")
        if event.started_at < self.spec.evaluation_start_not_before:
            raise ValueError("evaluation cannot start before evaluation_start_not_before")
        if self.status == "evaluating":
            current = next(item for item in self.events if isinstance(item, PatternEvaluationStarted))
            if (
                current.started_at == event.started_at
                and current.evaluation_dataset_fingerprint == event.evaluation_dataset_fingerprint
                and current.evaluation_cohort_id == event.evaluation_cohort_id
            ):
                return self
            raise ValueError("conflict: evaluation already started with a different dataset")
        if self.status != "registered":
            raise ValueError("evaluation can only start from registered")
        return PatternHypothesis(events=(*self.events, event))

    def close_evaluation(self, *, closed_at: datetime | str) -> PatternHypothesis:
        if self.status == "evaluation_closed":
            return self
        if self.status != "evaluating":
            raise ValueError("evaluation can only close from evaluating")
        event = PatternEvaluationClosed(hypothesis_id=self.hypothesis_id, closed_at=closed_at)
        return PatternHypothesis(events=(*self.events, event))

    def invalidate(self, *, invalidated_at: datetime | str, reason: str) -> PatternHypothesis:
        if self.status == "invalidated":
            return self
        if self.status not in {"registered", "evaluating"}:
            raise ValueError("hypothesis can only be invalidated from registered or evaluating")
        event = PatternHypothesisInvalidated(
            hypothesis_id=self.hypothesis_id,
            invalidated_at=invalidated_at,
            reason=reason,
        )
        return PatternHypothesis(events=(*self.events, event))


def reconcile_pattern_hypothesis(existing: PatternHypothesis, incoming: PatternHypothesis) -> PatternHypothesis:
    if existing.hypothesis_id != incoming.hypothesis_id:
        raise ValueError("conflict: hypothesis identity mismatch")
    if existing.events != incoming.events:
        raise ValueError("conflict: same hypothesis id with different content")
    return existing


@dataclass(frozen=True)
class PatternMatchedHop:
    """Occurrence hop: the same ID-free projection plus occurrence evidence refs."""

    ordinal: int
    source_kind: str
    relation_kind: str
    direction: str
    target_kind: str
    freshness_bucket: str
    evidence_rule_version: str
    driver_state: DriverState | Mapping[str, Any] | None = None
    family_ref: str | None = None
    evidence_refs: Sequence[str] = ()

    def __post_init__(self) -> None:
        hop, resolved = _pattern_hop_validated(
            ordinal=self.ordinal,
            source_kind=self.source_kind,
            relation_kind=self.relation_kind,
            direction=self.direction,
            target_kind=self.target_kind,
            freshness_bucket=self.freshness_bucket,
            evidence_rule_version=self.evidence_rule_version,
            driver_state=self.driver_state,
            family_ref=self.family_ref,
        )
        object.__setattr__(self, "ordinal", hop.ordinal)
        object.__setattr__(self, "source_kind", hop.source_kind)
        object.__setattr__(self, "relation_kind", hop.relation_kind)
        object.__setattr__(self, "direction", hop.direction)
        object.__setattr__(self, "target_kind", hop.target_kind)
        object.__setattr__(self, "freshness_bucket", hop.freshness_bucket)
        object.__setattr__(self, "evidence_rule_version", hop.evidence_rule_version)
        object.__setattr__(self, "driver_state", resolved)
        object.__setattr__(self, "family_ref", hop.family_ref)
        object.__setattr__(self, "evidence_refs", _unique_text_tuple(self.evidence_refs, "evidence_refs"))

    def identity_tuple(self) -> tuple[object, ...]:
        return _pattern_hop_identity(
            ordinal=self.ordinal,
            source_kind=self.source_kind,
            relation_kind=self.relation_kind,
            direction=self.direction,
            target_kind=self.target_kind,
            freshness_bucket=self.freshness_bucket,
            evidence_rule_version=self.evidence_rule_version,
            driver_state=self.driver_state,
            family_ref=self.family_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        # Same canonical-bytes rule as PatternStep: later-added fields stay
        # absent when None so stored occurrences keep rehydrating.
        payload = {
            "ordinal": self.ordinal,
            "source_kind": self.source_kind,
            "relation_kind": self.relation_kind,
            "direction": self.direction,
            "target_kind": self.target_kind,
            "freshness_bucket": self.freshness_bucket,
            "evidence_rule_version": self.evidence_rule_version,
            "driver_state": None if self.driver_state is None else self.driver_state.to_dict(),
            "evidence_refs": list(self.evidence_refs),
        }
        if self.family_ref is not None:
            payload["family_ref"] = self.family_ref
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternMatchedHop) -> PatternMatchedHop:
        if isinstance(value, PatternMatchedHop):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("matched hop must be PatternMatchedHop or a mapping")
        payload = _pattern_hop_mapping(value)
        payload["evidence_refs"] = value.get("evidence_refs") or ()
        return cls(**payload)


def _path_tuple(value: Sequence[Any] | None) -> tuple[PatternMatchedHop, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("exact_path must be a sequence of PatternMatchedHop")
    hops = tuple(
        item if isinstance(item, PatternMatchedHop) else PatternMatchedHop.from_mapping(item) for item in value
    )
    for index, hop in enumerate(hops):
        if hop.ordinal != index:
            raise ValueError("exact_path must be ordered with consecutive ordinals starting at 0")
    return hops


@dataclass(frozen=True)
class PatternForecast:
    """Shadow prediction identity carried by an occurrence. Not an outcome label."""

    prediction_id: str
    episode_id: str
    horizon_id: str
    model_identity: str
    probabilities: Mapping[str, Any]
    predicted_class: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "prediction_id",
            _validate_prefixed_id(self.prediction_id, _PREDICTION_ID_PREFIX, "prediction_id"),
        )
        object.__setattr__(self, "episode_id", _required_text(self.episode_id, "episode_id"))
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))
        object.__setattr__(self, "model_identity", _required_text(self.model_identity, "model_identity"))
        probabilities = _move_distribution(self.probabilities)
        predicted_class = _required_text(self.predicted_class, "predicted_class").upper()
        if predicted_class not in PREDICTION_CLASSES:
            raise ValueError("predicted_class must be one of: DOWN, FLAT, UP")
        if probabilities[predicted_class] != max(probabilities.values()):
            raise ValueError("predicted_class must be a highest-probability class")
        object.__setattr__(self, "probabilities", probabilities)
        object.__setattr__(self, "predicted_class", predicted_class)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "episode_id": self.episode_id,
            "horizon_id": self.horizon_id,
            "model_identity": self.model_identity,
            "probabilities": dict(self.probabilities),
            "predicted_class": self.predicted_class,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternForecast) -> PatternForecast:
        if isinstance(value, PatternForecast):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern forecast must be PatternForecast or a mapping")
        return cls(
            prediction_id=value.get("prediction_id"),
            episode_id=value.get("episode_id"),
            horizon_id=value.get("horizon_id"),
            model_identity=value.get("model_identity"),
            probabilities=value.get("probabilities") or {},
            predicted_class=value.get("predicted_class"),
        )

    @classmethod
    def from_world_prediction(cls, prediction: WorldPrediction, *, model_identity: str) -> PatternForecast:
        if not isinstance(prediction, WorldPrediction):
            raise TypeError("forecast requires a WorldPrediction")
        if prediction.predicted_class is None or prediction.probabilities is None:
            raise ValueError("pattern forecast requires a shadow_only WorldPrediction")
        return cls(
            prediction_id=prediction.prediction_id,
            episode_id=prediction.episode_id,
            horizon_id=prediction.horizon_id,
            model_identity=model_identity,
            probabilities=prediction.probabilities,
            predicted_class=prediction.predicted_class,
        )


@dataclass(frozen=True)
class PatternOccurrenceSpec:
    """Bounded application of a hypothesis at one cutoff/instrument. No ready_at, no labels."""

    hypothesis_id: str
    hypothesis_content_sha256: str
    cohort_id: str
    instrument: WorldEntityRef | Mapping[str, Any]
    cutoff_at: datetime | str
    exact_path: Sequence[PatternMatchedHop | Mapping[str, Any]]
    forecast: PatternForecast | Mapping[str, Any]
    expected_horizon_ids: Sequence[str]
    feature_contract_id: str
    feature_contract_fingerprint: str
    feature_mask_id: str
    feature_mask_fingerprint: str
    evaluation_dataset_fingerprint: str
    formation_dataset_fingerprint: str
    evaluation_start_not_before: datetime | str
    artifact_refs: Sequence[str] = ()
    fact_refs: Sequence[str] = ()
    schema_version: str = PATTERN_OCCURRENCE_SCHEMA
    occurrence_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != PATTERN_OCCURRENCE_SCHEMA:
            raise ValueError(f"schema_version must be {PATTERN_OCCURRENCE_SCHEMA}")
        hypothesis_id = PatternHypothesisId(self.hypothesis_id).value
        instrument = _as_entity(self.instrument, "instrument")
        if instrument.kind != "instrument":
            raise ValueError("occurrence instrument must be a namespaced instrument")
        cutoff_at = parse_utc_timestamp(self.cutoff_at, "cutoff_at")
        evaluation_start_not_before = parse_utc_timestamp(
            self.evaluation_start_not_before, "evaluation_start_not_before"
        )
        exact_path = _path_tuple(self.exact_path)
        forecast = (
            self.forecast if isinstance(self.forecast, PatternForecast) else PatternForecast.from_mapping(self.forecast)
        )
        expected = _unique_text_tuple(self.expected_horizon_ids, "expected_horizon_ids")
        if not expected:
            raise ValueError("expected_horizon_ids must not be empty")
        if forecast.horizon_id not in expected:
            raise ValueError("forecast horizon_id must be an expected occurrence horizon")
        evaluation_fp = _sha256_hex(self.evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint")
        formation_fp = _sha256_hex(self.formation_dataset_fingerprint, "formation_dataset_fingerprint")
        if evaluation_fp == formation_fp:
            raise ValueError("in-sample confirmation is forbidden: evaluation dataset must differ from formation")
        if cutoff_at < evaluation_start_not_before:
            raise ValueError("occurrence cutoff is before evaluation start")
        identity = {
            "schema_version": schema_version,
            "hypothesis_id": hypothesis_id,
            "hypothesis_content_sha256": _sha256_hex(self.hypothesis_content_sha256, "hypothesis_content_sha256"),
            "cohort_id": _required_text(self.cohort_id, "cohort_id"),
            "instrument": instrument.to_dict(),
            "cutoff_at": _iso(cutoff_at),
            "exact_path": [hop.to_dict() for hop in exact_path],
            "forecast": forecast.to_dict(),
            "expected_horizon_ids": list(expected),
            "feature_contract_id": _required_text(self.feature_contract_id, "feature_contract_id"),
            "feature_contract_fingerprint": _sha256_hex(
                self.feature_contract_fingerprint, "feature_contract_fingerprint"
            ),
            "feature_mask_id": _required_text(self.feature_mask_id, "feature_mask_id"),
            "feature_mask_fingerprint": _sha256_hex(self.feature_mask_fingerprint, "feature_mask_fingerprint"),
            "evaluation_dataset_fingerprint": evaluation_fp,
            "formation_dataset_fingerprint": formation_fp,
            "evaluation_start_not_before": _iso(evaluation_start_not_before),
            "artifact_refs": list(_unique_text_tuple(self.artifact_refs, "artifact_refs")),
            "fact_refs": list(_unique_text_tuple(self.fact_refs, "fact_refs")),
        }
        occurrence_id = _prefixed_id(_OCCURRENCE_ID_PREFIX, identity)
        if self.occurrence_id is not None and _required_text(self.occurrence_id, "occurrence_id") != occurrence_id:
            raise ValueError("occurrence_id does not match the canonical pattern occurrence")
        content_payload = {**identity, "occurrence_id": occurrence_id}
        digest = canonical_sha256(content_payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical pattern occurrence")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "hypothesis_content_sha256", identity["hypothesis_content_sha256"])
        object.__setattr__(self, "cohort_id", identity["cohort_id"])
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "exact_path", exact_path)
        object.__setattr__(self, "forecast", forecast)
        object.__setattr__(self, "expected_horizon_ids", expected)
        object.__setattr__(self, "feature_contract_id", identity["feature_contract_id"])
        object.__setattr__(self, "feature_contract_fingerprint", identity["feature_contract_fingerprint"])
        object.__setattr__(self, "feature_mask_id", identity["feature_mask_id"])
        object.__setattr__(self, "feature_mask_fingerprint", identity["feature_mask_fingerprint"])
        object.__setattr__(self, "evaluation_dataset_fingerprint", evaluation_fp)
        object.__setattr__(self, "formation_dataset_fingerprint", formation_fp)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start_not_before)
        object.__setattr__(self, "artifact_refs", tuple(identity["artifact_refs"]))
        object.__setattr__(self, "fact_refs", tuple(identity["fact_refs"]))
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "occurrence_id": self.occurrence_id,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_content_sha256": self.hypothesis_content_sha256,
            "cohort_id": self.cohort_id,
            "instrument": self.instrument.to_dict(),
            "cutoff_at": _iso(self.cutoff_at),
            "exact_path": [hop.to_dict() for hop in self.exact_path],
            "forecast": self.forecast.to_dict(),
            "expected_horizon_ids": list(self.expected_horizon_ids),
            "feature_contract_id": self.feature_contract_id,
            "feature_contract_fingerprint": self.feature_contract_fingerprint,
            "feature_mask_id": self.feature_mask_id,
            "feature_mask_fingerprint": self.feature_mask_fingerprint,
            "evaluation_dataset_fingerprint": self.evaluation_dataset_fingerprint,
            "formation_dataset_fingerprint": self.formation_dataset_fingerprint,
            "evaluation_start_not_before": _iso(self.evaluation_start_not_before),
            "artifact_refs": list(self.artifact_refs),
            "fact_refs": list(self.fact_refs),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOccurrenceSpec) -> PatternOccurrenceSpec:
        if isinstance(value, PatternOccurrenceSpec):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern occurrence spec must be PatternOccurrenceSpec or a mapping")
        if "ready_at" in value:
            raise ValueError("pattern occurrence payload must not contain ready_at")
        return cls(
            hypothesis_id=value.get("hypothesis_id"),
            hypothesis_content_sha256=value.get("hypothesis_content_sha256"),
            cohort_id=value.get("cohort_id"),
            instrument=value.get("instrument") or {},
            cutoff_at=value.get("cutoff_at"),
            exact_path=value.get("exact_path") or (),
            forecast=value.get("forecast") or {},
            expected_horizon_ids=value.get("expected_horizon_ids") or (),
            feature_contract_id=value.get("feature_contract_id"),
            feature_contract_fingerprint=value.get("feature_contract_fingerprint"),
            feature_mask_id=value.get("feature_mask_id"),
            feature_mask_fingerprint=value.get("feature_mask_fingerprint"),
            evaluation_dataset_fingerprint=value.get("evaluation_dataset_fingerprint"),
            formation_dataset_fingerprint=value.get("formation_dataset_fingerprint"),
            evaluation_start_not_before=value.get("evaluation_start_not_before"),
            artifact_refs=value.get("artifact_refs") or (),
            fact_refs=value.get("fact_refs") or (),
            schema_version=value.get("schema_version", PATTERN_OCCURRENCE_SCHEMA),
            occurrence_id=value.get("occurrence_id"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class PatternOutcomeLink:
    """Append-only pointer to a canonical WorldOutcome leaf. Never a second label authority."""

    occurrence_id: str
    horizon_id: str
    world_outcome_event_id: str
    world_outcome_content_sha256: str
    supersedes_link_id: str | None = None
    schema_version: str = PATTERN_OUTCOME_LINK_SCHEMA
    link_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != PATTERN_OUTCOME_LINK_SCHEMA:
            raise ValueError(f"schema_version must be {PATTERN_OUTCOME_LINK_SCHEMA}")
        occurrence_id = PatternOccurrenceId(self.occurrence_id).value
        horizon_id = _required_text(self.horizon_id, "horizon_id")
        world_outcome_event_id = _validate_prefixed_id(
            self.world_outcome_event_id, _OUTCOME_EVENT_PREFIX, "world_outcome_event_id"
        )
        world_outcome_content_sha256 = _sha256_hex(self.world_outcome_content_sha256, "world_outcome_content_sha256")
        supersedes_link_id = (
            None if self.supersedes_link_id is None else PatternOutcomeLinkId(self.supersedes_link_id).value
        )
        identity = {
            "schema_version": schema_version,
            "occurrence_id": occurrence_id,
            "horizon_id": horizon_id,
            "world_outcome_event_id": world_outcome_event_id,
            "world_outcome_content_sha256": world_outcome_content_sha256,
            "supersedes_link_id": supersedes_link_id,
        }
        link_id = _prefixed_id(_LINK_ID_PREFIX, identity)
        if self.link_id is not None and _required_text(self.link_id, "link_id") != link_id:
            raise ValueError("link_id does not match the canonical pattern outcome link")
        digest = canonical_sha256({**identity, "link_id": link_id})
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical pattern outcome link")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(self, "horizon_id", horizon_id)
        object.__setattr__(self, "world_outcome_event_id", world_outcome_event_id)
        object.__setattr__(self, "world_outcome_content_sha256", world_outcome_content_sha256)
        object.__setattr__(self, "supersedes_link_id", supersedes_link_id)
        object.__setattr__(self, "link_id", link_id)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "link_id": self.link_id,
            "occurrence_id": self.occurrence_id,
            "horizon_id": self.horizon_id,
            "world_outcome_event_id": self.world_outcome_event_id,
            "world_outcome_content_sha256": self.world_outcome_content_sha256,
            "supersedes_link_id": self.supersedes_link_id,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOutcomeLink) -> PatternOutcomeLink:
        if isinstance(value, PatternOutcomeLink):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pattern outcome link must be PatternOutcomeLink or a mapping")
        forbidden = sorted(key for key in value if key in _FORBIDDEN_OUTCOME_LABEL_KEYS)
        if forbidden:
            raise ValueError(f"PatternOutcomeLink forbids duplicated label fields: {', '.join(forbidden)}")
        return cls(
            occurrence_id=value.get("occurrence_id"),
            horizon_id=value.get("horizon_id"),
            world_outcome_event_id=value.get("world_outcome_event_id"),
            world_outcome_content_sha256=value.get("world_outcome_content_sha256"),
            supersedes_link_id=value.get("supersedes_link_id"),
            schema_version=value.get("schema_version", PATTERN_OUTCOME_LINK_SCHEMA),
            link_id=value.get("link_id"),
            content_sha256=value.get("content_sha256"),
        )


def _link_from_outcome(
    occurrence_id: str, outcome: WorldOutcome, *, supersedes_link_id: str | None
) -> PatternOutcomeLink:
    if not isinstance(outcome, WorldOutcome):
        raise TypeError("outcome link requires a canonical WorldOutcome")
    return PatternOutcomeLink(
        occurrence_id=occurrence_id,
        horizon_id=outcome.horizon.horizon_id,
        world_outcome_event_id=outcome.event_id,
        world_outcome_content_sha256=outcome.payload_hash,
        supersedes_link_id=supersedes_link_id,
    )


@dataclass(frozen=True)
class PatternOccurrenceRecorded:
    occurrence: PatternOccurrenceSpec | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = PATTERN_OCCURRENCE_EVENT_SCHEMA
    event_type: str = "pattern_occurrence_recorded"

    def __post_init__(self) -> None:
        occurrence = (
            self.occurrence
            if isinstance(self.occurrence, PatternOccurrenceSpec)
            else PatternOccurrenceSpec.from_mapping(self.occurrence)
        )
        object.__setattr__(self, "occurrence", occurrence)
        object.__setattr__(self, "event_type", "pattern_occurrence_recorded")
        _set_event_id(
            self,
            _OCCURRENCE_EVENT_PREFIX,
            {
                "event_type": "pattern_occurrence_recorded",
                "schema_version": PATTERN_OCCURRENCE_EVENT_SCHEMA,
                "occurrence_id": occurrence.occurrence_id,
                "content_sha256": occurrence.content_sha256,
            },
            schema_version=PATTERN_OCCURRENCE_EVENT_SCHEMA,
        )

    @property
    def occurrence_id(self) -> str:
        return self.occurrence.occurrence_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "occurrence": self.occurrence.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOccurrenceRecorded) -> PatternOccurrenceRecorded:
        if isinstance(value, PatternOccurrenceRecorded):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("occurrence recorded event must be a mapping")
        return cls(
            occurrence=value.get("occurrence") or value,
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_OCCURRENCE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternOutcomeLinked:
    link: PatternOutcomeLink | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = PATTERN_OCCURRENCE_EVENT_SCHEMA
    event_type: str = "pattern_outcome_linked"

    def __post_init__(self) -> None:
        link = self.link if isinstance(self.link, PatternOutcomeLink) else PatternOutcomeLink.from_mapping(self.link)
        if link.supersedes_link_id is not None:
            raise ValueError("PatternOutcomeLinked cannot carry an implicit supersession")
        object.__setattr__(self, "link", link)
        object.__setattr__(self, "event_type", "pattern_outcome_linked")
        _set_event_id(
            self,
            _OCCURRENCE_EVENT_PREFIX,
            {
                "event_type": "pattern_outcome_linked",
                "schema_version": PATTERN_OCCURRENCE_EVENT_SCHEMA,
                "link_id": link.link_id,
                "content_sha256": link.content_sha256,
            },
            schema_version=PATTERN_OCCURRENCE_EVENT_SCHEMA,
        )

    @property
    def occurrence_id(self) -> str:
        return self.link.occurrence_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "link": self.link.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOutcomeLinked) -> PatternOutcomeLinked:
        if isinstance(value, PatternOutcomeLinked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("outcome linked event must be a mapping")
        return cls(
            link=value.get("link") or value,
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_OCCURRENCE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternOutcomeLinkSuperseded:
    predecessor_link_id: str
    successor: PatternOutcomeLink | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = PATTERN_OCCURRENCE_EVENT_SCHEMA
    event_type: str = "pattern_outcome_link_superseded"

    def __post_init__(self) -> None:
        predecessor_link_id = PatternOutcomeLinkId(self.predecessor_link_id).value
        successor = (
            self.successor
            if isinstance(self.successor, PatternOutcomeLink)
            else PatternOutcomeLink.from_mapping(self.successor)
        )
        if successor.supersedes_link_id != predecessor_link_id:
            raise ValueError("successor outcome link must explicitly supersede the predecessor")
        object.__setattr__(self, "predecessor_link_id", predecessor_link_id)
        object.__setattr__(self, "successor", successor)
        object.__setattr__(self, "event_type", "pattern_outcome_link_superseded")
        _set_event_id(
            self,
            _OCCURRENCE_EVENT_PREFIX,
            {
                "event_type": "pattern_outcome_link_superseded",
                "schema_version": PATTERN_OCCURRENCE_EVENT_SCHEMA,
                "predecessor_link_id": predecessor_link_id,
                "successor_link_id": successor.link_id,
                "content_sha256": successor.content_sha256,
            },
            schema_version=PATTERN_OCCURRENCE_EVENT_SCHEMA,
        )

    @property
    def occurrence_id(self) -> str:
        return self.successor.occurrence_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "predecessor_link_id": self.predecessor_link_id,
            "successor": self.successor.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOutcomeLinkSuperseded) -> PatternOutcomeLinkSuperseded:
        if isinstance(value, PatternOutcomeLinkSuperseded):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("outcome link superseded event must be a mapping")
        return cls(
            predecessor_link_id=value.get("predecessor_link_id"),
            successor=value.get("successor") or {},
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_OCCURRENCE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class PatternOccurrenceInvalidated:
    occurrence_id: str
    invalidated_at: datetime | str
    reason: str
    event_id: str | None = None
    schema_version: str = PATTERN_OCCURRENCE_EVENT_SCHEMA
    event_type: str = "pattern_occurrence_invalidated"

    def __post_init__(self) -> None:
        occurrence_id = PatternOccurrenceId(self.occurrence_id).value
        invalidated_at = parse_utc_timestamp(self.invalidated_at, "invalidated_at")
        reason = _required_text(self.reason, "reason")
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(self, "invalidated_at", invalidated_at)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "event_type", "pattern_occurrence_invalidated")
        _set_event_id(
            self,
            _OCCURRENCE_EVENT_PREFIX,
            {
                "event_type": "pattern_occurrence_invalidated",
                "schema_version": PATTERN_OCCURRENCE_EVENT_SCHEMA,
                "occurrence_id": occurrence_id,
                "invalidated_at": _iso(invalidated_at),
                "reason": reason,
            },
            schema_version=PATTERN_OCCURRENCE_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "invalidated_at": _iso(self.invalidated_at),
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternOccurrenceInvalidated) -> PatternOccurrenceInvalidated:
        if isinstance(value, PatternOccurrenceInvalidated):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("occurrence invalidated event must be a mapping")
        return cls(
            occurrence_id=value.get("occurrence_id"),
            invalidated_at=value.get("invalidated_at"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_OCCURRENCE_EVENT_SCHEMA),
        )


PatternOccurrenceEvent = (
    PatternOccurrenceRecorded | PatternOutcomeLinked | PatternOutcomeLinkSuperseded | PatternOccurrenceInvalidated
)
_OCCURRENCE_EVENT_CLASSES = (
    PatternOccurrenceRecorded,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
    PatternOccurrenceInvalidated,
)
_OCCURRENCE_EVENT_PARSERS = {
    "pattern_occurrence_recorded": PatternOccurrenceRecorded.from_mapping,
    "pattern_outcome_linked": PatternOutcomeLinked.from_mapping,
    "pattern_outcome_link_superseded": PatternOutcomeLinkSuperseded.from_mapping,
    "pattern_occurrence_invalidated": PatternOccurrenceInvalidated.from_mapping,
}


def parse_pattern_occurrence_event(value: Mapping[str, Any] | PatternOccurrenceEvent) -> PatternOccurrenceEvent:
    if isinstance(value, _OCCURRENCE_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("pattern occurrence event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _OCCURRENCE_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown pattern occurrence event_type: {event_type}") from exc
    return parser(value)


def _fold_occurrence_events(
    events: Sequence[PatternOccurrenceEvent],
) -> tuple[PatternOccurrenceSpec, dict[str, PatternOutcomeLink], tuple[PatternOutcomeLink, ...], str]:
    if not events:
        raise ValueError("PatternOccurrence requires a recorded event")
    if not isinstance(events[0], PatternOccurrenceRecorded):
        raise ValueError("first event must be a recorded occurrence event")
    spec = events[0].occurrence
    active: dict[str, PatternOutcomeLink] = {}
    history: list[PatternOutcomeLink] = []
    status = "recorded"
    for event in events[1:]:
        if event.occurrence_id != spec.occurrence_id:
            raise ValueError("event occurrence_id mismatch")
        if status == "invalidated":
            raise ValueError("no events are allowed after an invalidated occurrence")
        if isinstance(event, PatternOccurrenceRecorded):
            raise ValueError("duplicate recorded occurrence event")
        if isinstance(event, PatternOutcomeLinked):
            link = event.link
            if link.horizon_id not in spec.expected_horizon_ids:
                raise ValueError("horizon is not an expected occurrence horizon")
            if link.horizon_id in active:
                raise ValueError("new WorldOutcome leaf requires explicit supersede")
            active[link.horizon_id] = link
            history.append(link)
            continue
        if isinstance(event, PatternOutcomeLinkSuperseded):
            successor = event.successor
            current = active.get(successor.horizon_id)
            if current is None:
                raise ValueError("no current outcome link to supersede")
            if event.predecessor_link_id != current.link_id or successor.supersedes_link_id != current.link_id:
                raise ValueError("outcome correction must explicitly supersede the current leaf")
            active[successor.horizon_id] = successor
            history.append(successor)
            continue
        if isinstance(event, PatternOccurrenceInvalidated):
            status = "invalidated"
            continue
        raise TypeError(f"unsupported occurrence event: {type(event).__name__}")
    return spec, active, tuple(history), status


def _assert_path_matches_steps(path: Sequence[PatternMatchedHop], steps: Sequence[PatternStep]) -> None:
    if len(path) != len(steps):
        raise ValueError("exact_path must apply every hypothesis step")
    for hop, step in zip(path, steps, strict=True):
        if hop.identity_tuple() != step.identity_tuple():
            raise ValueError("exact_path must match the formulated hypothesis steps")


def _assert_copied_fingerprint(provided: str | None, expected: str, field_name: str) -> str:
    if provided is None:
        return expected
    value = _sha256_hex(provided, field_name)
    if value != expected:
        raise ValueError(f"{field_name} must match the hypothesis contract/mask fingerprints")
    return expected


def _assert_copied_text(provided: str | None, expected: str, field_name: str) -> str:
    if provided is None:
        return expected
    value = _required_text(provided, field_name)
    if value != expected:
        raise ValueError(f"{field_name} must match the hypothesis contract/mask fingerprints")
    return expected


def _forecast_from_input(
    forecast: PatternForecast | WorldPrediction | Mapping[str, Any],
    *,
    model_identity: str,
) -> PatternForecast:
    if isinstance(forecast, PatternForecast):
        resolved = forecast
    elif isinstance(forecast, WorldPrediction):
        if forecast.model_id != model_identity:
            raise ValueError("prediction model lineage does not match the hypothesis")
        resolved = PatternForecast.from_world_prediction(forecast, model_identity=model_identity)
    elif isinstance(forecast, Mapping):
        resolved = PatternForecast.from_mapping(forecast)
    else:
        raise TypeError("forecast must be PatternForecast or WorldPrediction")
    if resolved.model_identity != model_identity:
        raise ValueError("prediction model lineage does not match the hypothesis")
    return resolved


@dataclass(frozen=True)
class PatternOccurrence:
    """Event-sourced application of a hypothesis. Journal is creation plus outcome leaves."""

    events: Sequence[PatternOccurrenceEvent | Mapping[str, Any]]

    def __post_init__(self) -> None:
        events = tuple(parse_pattern_occurrence_event(item) for item in self.events)
        _fold_occurrence_events(events)
        object.__setattr__(self, "events", events)

    @classmethod
    def from_events(cls, events: Sequence[PatternOccurrenceEvent | Mapping[str, Any]]) -> PatternOccurrence:
        return cls(events=events)

    @classmethod
    def record(
        cls,
        hypothesis: PatternHypothesis,
        *,
        cohort_id: str,
        instrument: WorldEntityRef | Mapping[str, Any],
        cutoff_at: datetime | str,
        exact_path: Sequence[PatternMatchedHop | Mapping[str, Any]],
        forecast: PatternForecast | WorldPrediction | Mapping[str, Any],
        artifact_refs: Sequence[str] = (),
        fact_refs: Sequence[str] = (),
        expected_horizon_ids: Sequence[str] | None = None,
        feature_contract_id: str | None = None,
        feature_contract_fingerprint: str | None = None,
        feature_mask_id: str | None = None,
        feature_mask_fingerprint: str | None = None,
        evaluation_dataset_fingerprint: str | None = None,
    ) -> PatternOccurrence:
        if not isinstance(hypothesis, PatternHypothesis):
            raise TypeError("occurrence record requires a PatternHypothesis")
        if hypothesis.status == "registered":
            raise ValueError("occurrence cannot be recorded before evaluation start")
        if hypothesis.status != "evaluating":
            raise ValueError(f"occurrence cannot be recorded while hypothesis is {hypothesis.status}")
        started_cohort = hypothesis.evaluation_cohort_id
        if started_cohort is None:
            raise ValueError("occurrence cannot be recorded before evaluation start")
        if _required_text(cohort_id, "cohort_id") != started_cohort:
            raise ValueError("occurrence cohort_id must equal the started evaluation_cohort_id")
        spec = hypothesis.spec
        if evaluation_dataset_fingerprint is None:
            if hypothesis.evaluation_dataset_fingerprint is None:
                raise ValueError("occurrence cannot be recorded before evaluation start")
            evaluation_fp = hypothesis.evaluation_dataset_fingerprint
        else:
            evaluation_fp = _sha256_hex(evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint")
        if evaluation_fp == spec.formation_dataset_fingerprint:
            raise ValueError("in-sample confirmation is forbidden: evaluation dataset must differ from formation")
        if (
            hypothesis.evaluation_dataset_fingerprint is not None
            and evaluation_fp != hypothesis.evaluation_dataset_fingerprint
        ):
            raise ValueError("evaluation dataset must match the started prospective confirmation dataset")
        resolved_forecast = _forecast_from_input(forecast, model_identity=spec.model_identity)
        resolved_path = _path_tuple(exact_path)
        _assert_path_matches_steps(resolved_path, spec.steps)
        expected = spec.target.horizon_id if expected_horizon_ids is None else expected_horizon_ids
        if isinstance(expected, str):
            expected_ids = (expected,)
        else:
            expected_ids = _unique_text_tuple(expected, "expected_horizon_ids")
        if spec.target.horizon_id not in expected_ids:
            raise ValueError("expected_horizon_ids must include the hypothesis target horizon")
        occurrence_spec = PatternOccurrenceSpec(
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_content_sha256=spec.content_sha256,
            cohort_id=cohort_id,
            instrument=instrument,
            cutoff_at=cutoff_at,
            exact_path=resolved_path,
            forecast=resolved_forecast,
            expected_horizon_ids=expected_ids,
            feature_contract_id=_assert_copied_text(
                feature_contract_id, spec.feature_contract_id, "feature_contract_id"
            ),
            feature_contract_fingerprint=_assert_copied_fingerprint(
                feature_contract_fingerprint, spec.feature_contract_fingerprint, "feature_contract_fingerprint"
            ),
            feature_mask_id=_assert_copied_text(feature_mask_id, spec.feature_mask_id, "feature_mask_id"),
            feature_mask_fingerprint=_assert_copied_fingerprint(
                feature_mask_fingerprint, spec.feature_mask_fingerprint, "feature_mask_fingerprint"
            ),
            evaluation_dataset_fingerprint=evaluation_fp,
            formation_dataset_fingerprint=spec.formation_dataset_fingerprint,
            evaluation_start_not_before=spec.evaluation_start_not_before,
            artifact_refs=artifact_refs,
            fact_refs=fact_refs,
        )
        if occurrence_spec.cutoff_at <= spec.formation_cutoff:
            raise ValueError("occurrence cutoff must be after formation cutoff")
        if occurrence_spec.instrument.kind != spec.target.entity_kind:
            raise ValueError("occurrence instrument kind must match the hypothesis target")
        return cls(events=(PatternOccurrenceRecorded(occurrence=occurrence_spec),))

    @property
    def recorded(self) -> PatternOccurrenceRecorded:
        event = self.events[0]
        assert isinstance(event, PatternOccurrenceRecorded)
        return event

    @property
    def spec(self) -> PatternOccurrenceSpec:
        return self.recorded.occurrence

    @property
    def occurrence_id(self) -> str:
        return self.spec.occurrence_id

    @property
    def hypothesis_id(self) -> str:
        return self.spec.hypothesis_id

    @property
    def feature_contract_id(self) -> str:
        return self.spec.feature_contract_id

    @property
    def feature_contract_fingerprint(self) -> str:
        return self.spec.feature_contract_fingerprint

    @property
    def feature_mask_id(self) -> str:
        return self.spec.feature_mask_id

    @property
    def feature_mask_fingerprint(self) -> str:
        return self.spec.feature_mask_fingerprint

    @property
    def evaluation_dataset_fingerprint(self) -> str:
        return self.spec.evaluation_dataset_fingerprint

    @property
    def cutoff_at(self) -> datetime:
        return self.spec.cutoff_at

    @property
    def instrument(self) -> WorldEntityRef:
        return self.spec.instrument

    @property
    def exact_path(self) -> tuple[PatternMatchedHop, ...]:
        return self.spec.exact_path

    @property
    def status(self) -> str:
        _, _, _, status = _fold_occurrence_events(self.events)
        return status

    @property
    def outcome_links(self) -> tuple[PatternOutcomeLink, ...]:
        return _fold_occurrence_events(self.events)[2]

    def to_dict(self) -> dict[str, Any]:
        return self.spec.to_dict()

    def active_outcome_link(self, horizon_id: str) -> PatternOutcomeLink | None:
        horizon = _required_text(horizon_id, "horizon_id")
        return _fold_occurrence_events(self.events)[1].get(horizon)

    def _require_outcome(self, outcome: WorldOutcome) -> WorldOutcome:
        if not isinstance(outcome, WorldOutcome):
            raise TypeError("outcome link requires a canonical WorldOutcome")
        if self.status == "invalidated":
            raise ValueError("invalidated occurrence cannot accept an outcome link")
        if outcome.horizon.horizon_id not in self.spec.expected_horizon_ids:
            raise ValueError("horizon is not an expected occurrence horizon")
        if outcome.episode_id != self.spec.forecast.episode_id:
            raise ValueError("WorldOutcome episode_id does not match the forecast")
        if outcome.event_id is None or outcome.payload_hash is None:
            raise ValueError("WorldOutcome leaf is missing canonical identity")
        return outcome

    def link_outcome(self, outcome: WorldOutcome) -> PatternOccurrence:
        resolved = self._require_outcome(outcome)
        current = self.active_outcome_link(resolved.horizon.horizon_id)
        link = _link_from_outcome(self.occurrence_id, resolved, supersedes_link_id=None)
        if current is None:
            return PatternOccurrence(events=(*self.events, PatternOutcomeLinked(link=link)))
        if (
            current.world_outcome_event_id == link.world_outcome_event_id
            and current.world_outcome_content_sha256 == link.world_outcome_content_sha256
        ):
            return self
        raise ValueError("new WorldOutcome leaf requires explicit supersede")

    def supersede_outcome_link(self, outcome: WorldOutcome) -> PatternOccurrence:
        resolved = self._require_outcome(outcome)
        current = self.active_outcome_link(resolved.horizon.horizon_id)
        if current is None:
            raise ValueError("no current outcome link to supersede")
        if resolved.supersedes_event_id != current.world_outcome_event_id:
            raise ValueError("WorldOutcome must explicitly supersede the currently linked event")
        successor = _link_from_outcome(self.occurrence_id, resolved, supersedes_link_id=current.link_id)
        event = PatternOutcomeLinkSuperseded(predecessor_link_id=current.link_id, successor=successor)
        return PatternOccurrence(events=(*self.events, event))

    def invalidate(self, *, invalidated_at: datetime | str, reason: str) -> PatternOccurrence:
        if self.status == "invalidated":
            return self
        event = PatternOccurrenceInvalidated(
            occurrence_id=self.occurrence_id,
            invalidated_at=invalidated_at,
            reason=reason,
        )
        return PatternOccurrence(events=(*self.events, event))


def reconcile_pattern_occurrence(existing: PatternOccurrence, incoming: PatternOccurrence) -> PatternOccurrence:
    if existing.occurrence_id != incoming.occurrence_id:
        raise ValueError("conflict: occurrence identity mismatch")
    if existing.events != incoming.events:
        raise ValueError("conflict: same occurrence id with different content")
    return existing


@dataclass(frozen=True)
class PatternDiscoveryCompleted:
    evaluation_cohort_id: str
    manifest_sha256: str
    formation_dataset_fingerprint: str
    evaluation_dataset_fingerprint: str
    started_event_id: str
    formation_cutoff: datetime | str
    evaluation_start_not_before: datetime | str
    selected_hypothesis_ids: Sequence[str | PatternHypothesisId] = ()
    selected_count: int = 0
    shadow_only: bool = True
    decision_effect: str = "none"
    learning_authority: str = "shadow_only"
    event_id: str | None = None
    schema_version: str = PATTERN_DISCOVERY_COMPLETED_SCHEMA
    event_type: str = PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE

    def __post_init__(self) -> None:
        evaluation_cohort_id = _validate_prefixed_id(
            self.evaluation_cohort_id, _WORLD_COHORT_ID_PREFIX, "evaluation_cohort_id"
        )
        started_event_id = _validate_prefixed_id(self.started_event_id, _WORLD_COHORT_EVENT_PREFIX, "started_event_id")
        manifest_sha256 = _sha256_hex(self.manifest_sha256, "manifest_sha256")
        formation_fp = _sha256_hex(self.formation_dataset_fingerprint, "formation_dataset_fingerprint")
        evaluation_fp = _sha256_hex(self.evaluation_dataset_fingerprint, "evaluation_dataset_fingerprint")
        formation_cutoff = parse_utc_timestamp(self.formation_cutoff, "formation_cutoff")
        evaluation_start_not_before = parse_utc_timestamp(
            self.evaluation_start_not_before, "evaluation_start_not_before"
        )
        if not (formation_cutoff < evaluation_start_not_before):
            raise ValueError("evaluation_start_not_before must be strictly later than formation_cutoff")
        selected_hypothesis_ids = _selected_hypothesis_ids(self.selected_hypothesis_ids)
        selected_count = _non_negative_int(self.selected_count, "selected_count")
        if selected_count != len(selected_hypothesis_ids):
            raise ValueError("selected_count must equal the number of selected hypothesis ids")
        if self.shadow_only is not True:
            raise ValueError("shadow_only must remain true")
        if _required_text(self.decision_effect, "decision_effect") != "none":
            raise ValueError("decision_effect must be none")
        if _required_text(self.learning_authority, "learning_authority") != "shadow_only":
            raise ValueError("learning_authority must be shadow_only")
        object.__setattr__(self, "evaluation_cohort_id", evaluation_cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "formation_dataset_fingerprint", formation_fp)
        object.__setattr__(self, "evaluation_dataset_fingerprint", evaluation_fp)
        object.__setattr__(self, "started_event_id", started_event_id)
        object.__setattr__(self, "formation_cutoff", formation_cutoff)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start_not_before)
        object.__setattr__(self, "selected_hypothesis_ids", selected_hypothesis_ids)
        object.__setattr__(self, "selected_count", selected_count)
        object.__setattr__(self, "shadow_only", True)
        object.__setattr__(self, "decision_effect", "none")
        object.__setattr__(self, "learning_authority", "shadow_only")
        object.__setattr__(self, "event_type", PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE)
        _set_event_id(
            self,
            _DISCOVERY_COMPLETED_PREFIX,
            {
                "event_type": PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE,
                "schema_version": PATTERN_DISCOVERY_COMPLETED_SCHEMA,
                "evaluation_cohort_id": evaluation_cohort_id,
                "manifest_sha256": manifest_sha256,
                "formation_dataset_fingerprint": formation_fp,
                "evaluation_dataset_fingerprint": evaluation_fp,
                "started_event_id": started_event_id,
                "formation_cutoff": _iso(formation_cutoff),
                "evaluation_start_not_before": _iso(evaluation_start_not_before),
                "selected_hypothesis_ids": list(selected_hypothesis_ids),
                "selected_count": selected_count,
                "shadow_only": True,
                "decision_effect": "none",
                "learning_authority": "shadow_only",
            },
            schema_version=PATTERN_DISCOVERY_COMPLETED_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "evaluation_cohort_id": self.evaluation_cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "formation_dataset_fingerprint": self.formation_dataset_fingerprint,
            "evaluation_dataset_fingerprint": self.evaluation_dataset_fingerprint,
            "started_event_id": self.started_event_id,
            "formation_cutoff": _iso(self.formation_cutoff),
            "evaluation_start_not_before": _iso(self.evaluation_start_not_before),
            "selected_hypothesis_ids": list(self.selected_hypothesis_ids),
            "selected_count": self.selected_count,
            "shadow_only": self.shadow_only,
            "decision_effect": self.decision_effect,
            "learning_authority": self.learning_authority,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternDiscoveryCompleted) -> PatternDiscoveryCompleted:
        if isinstance(value, PatternDiscoveryCompleted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("discovery completed event must be a mapping")
        return cls(
            evaluation_cohort_id=value.get("evaluation_cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            formation_dataset_fingerprint=value.get("formation_dataset_fingerprint"),
            evaluation_dataset_fingerprint=value.get("evaluation_dataset_fingerprint"),
            started_event_id=value.get("started_event_id"),
            formation_cutoff=value.get("formation_cutoff"),
            evaluation_start_not_before=value.get("evaluation_start_not_before"),
            selected_hypothesis_ids=value.get("selected_hypothesis_ids") or (),
            selected_count=0 if value.get("selected_count") is None else value.get("selected_count"),
            shadow_only=True if value.get("shadow_only") is None else value.get("shadow_only"),
            decision_effect="none" if value.get("decision_effect") is None else value.get("decision_effect"),
            learning_authority="shadow_only"
            if value.get("learning_authority") is None
            else value.get("learning_authority"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", PATTERN_DISCOVERY_COMPLETED_SCHEMA),
        )


__all__ = [
    "PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE",
    "PATTERN_DISCOVERY_COMPLETED_SCHEMA",
    "PATTERN_HYPOTHESIS_EVENT_SCHEMA",
    "PATTERN_HYPOTHESIS_EVENT_TYPES",
    "PATTERN_HYPOTHESIS_SCHEMA",
    "PATTERN_HYPOTHESIS_STATUSES",
    "PATTERN_OCCURRENCE_EVENT_SCHEMA",
    "PATTERN_OCCURRENCE_EVENT_TYPES",
    "PATTERN_OCCURRENCE_SCHEMA",
    "PATTERN_OCCURRENCE_STATUSES",
    "PATTERN_OUTCOME_LINK_SCHEMA",
    "GRAPH_TRAVERSAL_V1_DIRECTIONS",
    "PATTERN_ASSOCIATION_METRIC",
    "PATTERN_FRESHNESS_BUCKETS",
    "PATTERN_GRAPH_NODE_KINDS",
    "PATTERN_PATH_DIRECTIONS",
    "EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY",
    "PATTERN_EVALUATION_HORIZON_IDS",
    "PatternDiscoveryCompleted",
    "PatternEvaluationClosed",
    "PatternEvaluationStarted",
    "PatternForecast",
    "PatternFormationStats",
    "PatternHypothesis",
    "PatternHypothesisEvent",
    "PatternHypothesisEventId",
    "PatternHypothesisId",
    "PatternHypothesisInvalidated",
    "PatternHypothesisRegistered",
    "PatternHypothesisSpec",
    "PatternMatchedHop",
    "PatternOccurrence",
    "PatternOccurrenceEvent",
    "PatternOccurrenceEventId",
    "PatternOccurrenceId",
    "PatternOccurrenceInvalidated",
    "PatternOccurrenceRecorded",
    "PatternOccurrenceSpec",
    "PatternOutcomeLink",
    "PatternOutcomeLinkId",
    "PatternOutcomeLinkSuperseded",
    "PatternOutcomeLinked",
    "PatternStep",
    "PatternTarget",
    "laplace_smoothed_distribution",
    "parse_pattern_hypothesis_event",
    "total_variation_distance",
    "parse_pattern_occurrence_event",
    "reconcile_pattern_hypothesis",
    "reconcile_pattern_occurrence",
]
