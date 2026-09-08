"""Immutable prospective World cohort aggregate, manifest, events, and commands.

Stdlib-only. Status is derived from typed events; admission happens only through
the aggregate. Authority is always ``shadow_only`` with ``decision_effect=none``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_episode import (
    OUTCOME_STATUSES,
    SUPPORTED_WORLD_HORIZONS,
    canonical_sha256,
    parse_utc_timestamp,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureMask
from trader.domain.world_scope import WorldScopeResolution


WORLD_COHORT_MANIFEST_SCHEMA = "world_cohort_manifest.v1"
WORLD_COHORT_EVENT_SCHEMA = "world_cohort_event.v1"
WORLD_COHORT_EVENT_ENVELOPE_SCHEMA = "world_cohort_event_envelope.v1"
WORLD_COHORT_SLOT_SCHEMA = "world_cohort_slot.v1"
WORLD_COHORT_REPORT_SCHEMA = "world_cohort_report.v1"
WORLD_STATISTICAL_PROTOCOL_SCHEMA = "world_statistical_protocol.v1"
WORLD_SUPPORT_GATES_SCHEMA = "world_support_gates.v1"
WORLD_RUNTIME_IDENTITY_SCHEMA = "world_runtime_identity.v1"
WORLD_RUNTIME_IDENTITY_INTENT_SCHEMA = "world_runtime_identity_intent.v1"

COHORT_AUTHORITY = "shadow_only"
COHORT_DECISION_EFFECT = "none"
COHORT_RECOMMENDATION = "NO_GO"
COHORT_TRADER_CONTRIBUTION = "not_attributable"

_COHORT_ID_PREFIX = "world_cohort:v1"
_EVENT_ID_PREFIX = "world_cohort_event:v1"
_SLOT_ID_PREFIX = "world_cohort_slot:v1"
_EVENT_SUBJECT_KIND = "world_cohort_event"

_KNOWN_HORIZONS = frozenset(item.horizon_id for item in SUPPORTED_WORLD_HORIZONS)
_TERMINAL_OUTCOME_STATUSES = frozenset(status for status in OUTCOME_STATUSES if status != "pending")
_PRIMARY_LANE_ROLES: frozenset[str] = frozenset({"primary_control", "primary_treatment", "pilot_treatment"})
_SCOPE_LANE_NAMES = frozenset({"macro", "joint", "graph"})
_FORMAL_LOGICAL_LANES = ("market", "status_only", "company", "macro", "joint")
_FORMAL_FAMILIES = ("markov", "gru")
_PRIMARY_METRICS = frozenset({"paired_multiclass_log_loss"})
_PAIR_UNITS = frozenset({"unique_market_anchor"})
_BLOCK_KEYS = frozenset({"venue_session"})
_CI_METHODS = frozenset({"deterministic_block_bootstrap.v1"})
_HEX = frozenset("0123456789abcdef")


class StudyKind(StrEnum):
    PIPELINE_PILOT = "pipeline_pilot"
    PROSPECTIVE_EVALUATION = "prospective_evaluation"


class CohortPhase(StrEnum):
    REGISTERED = "registered"
    ARMED = "armed"
    COLLECTING = "collecting"
    COLLECTION_CLOSED = "collection_closed"
    COMPLETE = "complete"
    INVALIDATED = "invalidated"


class LaneRole(StrEnum):
    PRIMARY_CONTROL = "primary_control"
    PROCESS_CONTROL = "process_control"
    PILOT_TREATMENT = "pilot_treatment"
    PRIMARY_TREATMENT = "primary_treatment"
    SECONDARY_CHALLENGER = "secondary_challenger"


class SensorMask(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class InvalidationReason(StrEnum):
    MANIFEST_ID_HASH_CONFLICT = "manifest_id_hash_conflict"
    ACCEPTED_DRIFT = "accepted_drift"
    MAPPING_GENERATION_DRIFT = "mapping_generation_drift"
    PRE_START_EPISODE = "pre_start_episode"
    CONTAMINATED_MACRO = "contaminated_macro"
    FUTURE_LEAK = "future_leak"
    UNPAIRED_TRAINING = "unpaired_training"
    MISSING_LANE_METADATA = "missing_lane_metadata"
    DESTRUCTIVE_CORRECTION = "destructive_correction"


class LaneBlockReason(StrEnum):
    CONFIG_DRIFT = "config_drift"


class LaneOperationalStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class SupportGateMode(StrEnum):
    DESCRIPTIVE_ONLY = "descriptive_only"
    FIXED_MINIMUM = "fixed_minimum"


class ContrastRole(StrEnum):
    PIPELINE_CONTROL = "pipeline_control"
    PILOT_TREATMENT = "pilot_treatment"
    PRIMARY = "primary"
    CONTROL = "control"
    SECONDARY = "secondary"


class ModelFamily(StrEnum):
    MARKOV = "markov"
    GRU = "gru"


class CollectionStopKind(StrEnum):
    FIXED_END = "fixed_end"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


def _prefixed_id(prefix: str, payload: Any) -> str:
    return f"{prefix}:{canonical_sha256(payload)}"


def _validate_prefixed_id(value: Any, prefix: str, field_name: str) -> str:
    text = _required_text(value, field_name)
    expected = f"{prefix}:"
    if not text.startswith(expected):
        raise ValueError(f"{field_name} must start with '{expected}'")
    _sha256_hex(text[len(expected) :], field_name)
    return text


def _required_int(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")
    return value


def _closed_text(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field_name)
    if text not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {options}")
    return text


def _enum(enum_cls: type[StrEnum], value: Any, field_name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError as exc:
            options = ", ".join(item.value for item in enum_cls)
            raise ValueError(f"{field_name} must be one of: {options}") from exc
    raise TypeError(f"{field_name} must be {enum_cls.__name__}")


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    items = tuple(_required_text(item, f"{field_name}[]") for item in value)
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


def _non_empty_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    items = _immutable_text_tuple(value, field_name)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    return items


def _reject_latest(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if "latest" in text.lower():
        raise ValueError(f"{field_name} must be an exact version, never latest")
    return text


def _git_commit(value: Any) -> str:
    text = _required_text(value, "git_commit").lower()
    if len(text) != 40 or any(char not in _HEX for char in text):
        raise ValueError("git_commit must be a 40-hex digest")
    return text


def _lane_logical_name(lane_id: str) -> str:
    return lane_id.rsplit(".", 1)[-1]


def _lane_requires_scope(lane_id: str) -> bool:
    return any(part in _SCOPE_LANE_NAMES for part in lane_id.split("."))


def _text_map(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    items: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, f"{field_name} key")
        items[key] = _required_text(raw_value, f"{field_name}.{key}")
    return MappingProxyType(items)


@dataclass(frozen=True)
class WorldCohortId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _COHORT_ID_PREFIX, "cohort_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class WorldCollectionStopRule:
    kind: CollectionStopKind | str
    at: datetime | str

    def __post_init__(self) -> None:
        kind = _enum(CollectionStopKind, self.kind, "kind")
        at = parse_utc_timestamp(self.at, "at")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "at", at)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "at": _iso(self.at)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCollectionStopRule) -> WorldCollectionStopRule:
        if isinstance(value, WorldCollectionStopRule):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("collection_stop_rule must be WorldCollectionStopRule or a mapping")
        return cls(kind=value.get("kind"), at=value.get("at"))


@dataclass(frozen=True)
class WorldRuntimeIdentity:
    git_commit: str
    python_version: str
    numpy_version: str
    application_build_id: str
    schema_version: str = WORLD_RUNTIME_IDENTITY_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_RUNTIME_IDENTITY_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_RUNTIME_IDENTITY_SCHEMA}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "git_commit", _git_commit(self.git_commit))
        object.__setattr__(self, "python_version", _reject_latest(self.python_version, "python_version"))
        object.__setattr__(self, "numpy_version", _reject_latest(self.numpy_version, "numpy_version"))
        object.__setattr__(
            self,
            "application_build_id",
            _reject_latest(self.application_build_id, "application_build_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "git_commit": self.git_commit,
            "python_version": self.python_version,
            "numpy_version": self.numpy_version,
            "application_build_id": self.application_build_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldRuntimeIdentity) -> WorldRuntimeIdentity:
        if isinstance(value, WorldRuntimeIdentity):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("runtime_identity must be WorldRuntimeIdentity or a mapping")
        return cls(
            git_commit=value.get("git_commit"),
            python_version=value.get("python_version"),
            numpy_version=value.get("numpy_version"),
            application_build_id=value.get("application_build_id"),
            schema_version=value.get("schema_version", WORLD_RUNTIME_IDENTITY_SCHEMA),
        )


@dataclass(frozen=True)
class WorldRuntimeIdentityIntent:
    """Operator-declared runtime constraints. Never a measured git commit."""

    application_build_id: str
    python_version: str | None = None
    numpy_version: str | None = None
    schema_version: str = WORLD_RUNTIME_IDENTITY_INTENT_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_RUNTIME_IDENTITY_INTENT_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_RUNTIME_IDENTITY_INTENT_SCHEMA}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(
            self,
            "application_build_id",
            _reject_latest(self.application_build_id, "application_build_id"),
        )
        python_version = None if self.python_version is None else _reject_latest(self.python_version, "python_version")
        numpy_version = None if self.numpy_version is None else _reject_latest(self.numpy_version, "numpy_version")
        object.__setattr__(self, "python_version", python_version)
        object.__setattr__(self, "numpy_version", numpy_version)

    def accepts(self, measured: WorldRuntimeIdentity) -> bool:
        if not isinstance(measured, WorldRuntimeIdentity):
            raise TypeError("measured runtime identity must be WorldRuntimeIdentity")
        if measured.application_build_id != self.application_build_id:
            return False
        if self.python_version is not None and measured.python_version != self.python_version:
            return False
        if self.numpy_version is not None and measured.numpy_version != self.numpy_version:
            return False
        return True

    def to_dict(self) -> dict[str, str]:
        payload = {
            "schema_version": self.schema_version,
            "application_build_id": self.application_build_id,
        }
        if self.python_version is not None:
            payload["python_version"] = self.python_version
        if self.numpy_version is not None:
            payload["numpy_version"] = self.numpy_version
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldRuntimeIdentityIntent) -> WorldRuntimeIdentityIntent:
        if isinstance(value, WorldRuntimeIdentityIntent):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("runtime_identity_intent must be WorldRuntimeIdentityIntent or a mapping")
        return cls(
            application_build_id=value.get("application_build_id"),
            python_version=value.get("python_version"),
            numpy_version=value.get("numpy_version"),
            schema_version=value.get("schema_version", WORLD_RUNTIME_IDENTITY_INTENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldStatisticalProtocol:
    pair_unit: str
    block_key: str
    ci_method: str
    ci_level: float
    bootstrap_resamples: int
    seed: int
    schema_version: str = WORLD_STATISTICAL_PROTOCOL_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_STATISTICAL_PROTOCOL_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_STATISTICAL_PROTOCOL_SCHEMA}")
        if isinstance(self.ci_level, bool) or not isinstance(self.ci_level, (int, float)):
            raise TypeError("ci_level must be a finite number")
        ci_level = float(self.ci_level)
        if ci_level <= 0.0 or ci_level >= 1.0:
            raise ValueError("ci_level must be in (0, 1)")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "pair_unit", _closed_text(self.pair_unit, "pair_unit", _PAIR_UNITS))
        object.__setattr__(self, "block_key", _closed_text(self.block_key, "block_key", _BLOCK_KEYS))
        object.__setattr__(self, "ci_method", _closed_text(self.ci_method, "ci_method", _CI_METHODS))
        object.__setattr__(self, "ci_level", ci_level)
        object.__setattr__(
            self,
            "bootstrap_resamples",
            _required_int(self.bootstrap_resamples, "bootstrap_resamples", minimum=1),
        )
        object.__setattr__(self, "seed", _required_int(self.seed, "seed"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pair_unit": self.pair_unit,
            "block_key": self.block_key,
            "ci_method": self.ci_method,
            "ci_level": self.ci_level,
            "bootstrap_resamples": self.bootstrap_resamples,
            "seed": self.seed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldStatisticalProtocol) -> WorldStatisticalProtocol:
        if isinstance(value, WorldStatisticalProtocol):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("statistical_protocol must be WorldStatisticalProtocol or a mapping")
        return cls(
            pair_unit=value.get("pair_unit"),
            block_key=value.get("block_key"),
            ci_method=value.get("ci_method"),
            ci_level=value.get("ci_level"),
            bootstrap_resamples=value.get("bootstrap_resamples"),
            seed=value.get("seed"),
            schema_version=value.get("schema_version", WORLD_STATISTICAL_PROTOCOL_SCHEMA),
        )


@dataclass(frozen=True)
class WorldSupportGates:
    mode: SupportGateMode | str
    descriptive_minimum_unique_anchors: int
    formal_minimum_unique_anchors: int | None = None
    schema_version: str = WORLD_SUPPORT_GATES_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_SUPPORT_GATES_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_SUPPORT_GATES_SCHEMA}")
        mode = _enum(SupportGateMode, self.mode, "mode")
        descriptive = _required_int(
            self.descriptive_minimum_unique_anchors,
            "descriptive_minimum_unique_anchors",
            minimum=1,
        )
        if mode is SupportGateMode.DESCRIPTIVE_ONLY:
            if self.formal_minimum_unique_anchors is not None:
                raise ValueError("descriptive_only support gates must keep formal_minimum_unique_anchors null")
            formal = None
        else:
            if self.formal_minimum_unique_anchors is None:
                raise ValueError("fixed_minimum support gates require a non-null formal_minimum_unique_anchors")
            formal = _required_int(
                self.formal_minimum_unique_anchors,
                "formal_minimum_unique_anchors",
                minimum=1,
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "descriptive_minimum_unique_anchors", descriptive)
        object.__setattr__(self, "formal_minimum_unique_anchors", formal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "descriptive_minimum_unique_anchors": self.descriptive_minimum_unique_anchors,
            "formal_minimum_unique_anchors": self.formal_minimum_unique_anchors,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldSupportGates) -> WorldSupportGates:
        if isinstance(value, WorldSupportGates):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("support_gates must be WorldSupportGates or a mapping")
        return cls(
            mode=value.get("mode"),
            descriptive_minimum_unique_anchors=value.get("descriptive_minimum_unique_anchors"),
            formal_minimum_unique_anchors=value.get("formal_minimum_unique_anchors"),
            schema_version=value.get("schema_version", WORLD_SUPPORT_GATES_SCHEMA),
        )


@dataclass(frozen=True)
class WorldScopeMappingRef:
    mapping_id: str
    mapping_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mapping_id", _required_text(self.mapping_id, "mapping_id"))
        object.__setattr__(self, "mapping_sha256", _sha256_hex(self.mapping_sha256, "mapping_sha256"))

    def to_dict(self) -> dict[str, str]:
        return {"mapping_id": self.mapping_id, "mapping_sha256": self.mapping_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldScopeMappingRef | None) -> WorldScopeMappingRef | None:
        if value is None:
            return None
        if isinstance(value, WorldScopeMappingRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope_mapping must be WorldScopeMappingRef, a mapping, or null")
        return cls(mapping_id=value.get("mapping_id"), mapping_sha256=value.get("mapping_sha256"))


@dataclass(frozen=True)
class WorldLaneDefinition:
    lane_id: str
    model_family: ModelFamily | str
    model_id: str
    model_version: str
    feature_contract_id: str
    feature_contract_fingerprint: str
    feature_mask_id: str
    feature_mask_fingerprint: str
    seed: int
    sequence_length: int | None
    hyperparameters_sha256: str
    role: LaneRole | str

    def __post_init__(self) -> None:
        family = _enum(ModelFamily, self.model_family, "model_family")
        if family is ModelFamily.GRU:
            if self.sequence_length is None:
                raise ValueError("sequence_length is required for gru lanes")
            sequence_length = _required_int(self.sequence_length, "sequence_length", minimum=1)
        else:
            if self.sequence_length is not None:
                raise ValueError("sequence_length is forbidden for markov lanes")
            sequence_length = None
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))
        object.__setattr__(self, "model_family", family)
        object.__setattr__(self, "model_id", _required_text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _required_text(self.model_version, "model_version"))
        object.__setattr__(self, "feature_contract_id", _required_text(self.feature_contract_id, "feature_contract_id"))
        object.__setattr__(
            self,
            "feature_contract_fingerprint",
            _sha256_hex(self.feature_contract_fingerprint, "feature_contract_fingerprint"),
        )
        object.__setattr__(self, "feature_mask_id", _required_text(self.feature_mask_id, "feature_mask_id"))
        object.__setattr__(
            self,
            "feature_mask_fingerprint",
            _sha256_hex(self.feature_mask_fingerprint, "feature_mask_fingerprint"),
        )
        object.__setattr__(self, "seed", _required_int(self.seed, "seed"))
        object.__setattr__(self, "sequence_length", sequence_length)
        object.__setattr__(
            self,
            "hyperparameters_sha256",
            _sha256_hex(self.hyperparameters_sha256, "hyperparameters_sha256"),
        )
        object.__setattr__(self, "role", _enum(LaneRole, self.role, "role"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "model_family": self.model_family.value,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_contract_id": self.feature_contract_id,
            "feature_contract_fingerprint": self.feature_contract_fingerprint,
            "feature_mask_id": self.feature_mask_id,
            "feature_mask_fingerprint": self.feature_mask_fingerprint,
            "seed": self.seed,
            "sequence_length": self.sequence_length,
            "hyperparameters_sha256": self.hyperparameters_sha256,
            "role": self.role.value,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldLaneDefinition) -> WorldLaneDefinition:
        if isinstance(value, WorldLaneDefinition):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("lane must be WorldLaneDefinition or a mapping")
        return cls(
            lane_id=value.get("lane_id"),
            model_family=value.get("model_family"),
            model_id=value.get("model_id"),
            model_version=value.get("model_version"),
            feature_contract_id=value.get("feature_contract_id"),
            feature_contract_fingerprint=value.get("feature_contract_fingerprint"),
            feature_mask_id=value.get("feature_mask_id"),
            feature_mask_fingerprint=value.get("feature_mask_fingerprint"),
            seed=value.get("seed"),
            sequence_length=value.get("sequence_length"),
            hyperparameters_sha256=value.get("hyperparameters_sha256"),
            role=value.get("role"),
        )


@dataclass(frozen=True)
class WorldSensorRequirement:
    sensor_id: str
    source_contract_id: str
    projection_contract_id: str
    mode: SensorMask | str
    lane_ids: Sequence[str]

    def __post_init__(self) -> None:
        lane_ids = _non_empty_text_tuple(self.lane_ids, "lane_ids")
        object.__setattr__(self, "sensor_id", _required_text(self.sensor_id, "sensor_id"))
        object.__setattr__(self, "source_contract_id", _required_text(self.source_contract_id, "source_contract_id"))
        object.__setattr__(
            self,
            "projection_contract_id",
            _required_text(self.projection_contract_id, "projection_contract_id"),
        )
        object.__setattr__(self, "mode", _enum(SensorMask, self.mode, "mode"))
        object.__setattr__(self, "lane_ids", lane_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "source_contract_id": self.source_contract_id,
            "projection_contract_id": self.projection_contract_id,
            "mode": self.mode.value,
            "lane_ids": list(self.lane_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldSensorRequirement) -> WorldSensorRequirement:
        if isinstance(value, WorldSensorRequirement):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("sensor requirement must be WorldSensorRequirement or a mapping")
        return cls(
            sensor_id=value.get("sensor_id"),
            source_contract_id=value.get("source_contract_id"),
            projection_contract_id=value.get("projection_contract_id"),
            mode=value.get("mode"),
            lane_ids=value.get("lane_ids") or (),
        )


@dataclass(frozen=True)
class WorldContrastTerm:
    lane_id: str
    coefficient: int

    def __post_init__(self) -> None:
        coefficient = _required_int(self.coefficient, "coefficient")
        if coefficient == 0:
            raise ValueError("coefficient must be a non-zero signed integer")
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))
        object.__setattr__(self, "coefficient", coefficient)

    def to_dict(self) -> dict[str, Any]:
        return {"lane_id": self.lane_id, "coefficient": self.coefficient}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldContrastTerm) -> WorldContrastTerm:
        if isinstance(value, WorldContrastTerm):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("contrast term must be WorldContrastTerm or a mapping")
        return cls(lane_id=value.get("lane_id"), coefficient=value.get("coefficient"))


@dataclass(frozen=True)
class WorldContrastDefinition:
    contrast_id: str
    terms: Sequence[WorldContrastTerm | Mapping[str, Any]]
    primary_metric: str
    role: ContrastRole | str

    def __post_init__(self) -> None:
        if isinstance(self.terms, (str, bytes, bytearray)) or not isinstance(self.terms, Sequence):
            raise TypeError("terms must be a sequence of WorldContrastTerm")
        terms = tuple(
            item if isinstance(item, WorldContrastTerm) else WorldContrastTerm.from_mapping(item) for item in self.terms
        )
        if len(terms) < 2:
            raise ValueError("contrast requires at least two terms")
        lane_ids = [term.lane_id for term in terms]
        if len(lane_ids) != len(set(lane_ids)):
            raise ValueError("duplicate contrast lane: each lane may appear at most once")
        positives = [term for term in terms if term.coefficient > 0]
        negatives = [term for term in terms if term.coefficient < 0]
        if not positives or not negatives:
            raise ValueError("contrast requires a positive and a negative coefficient")
        if sum(term.coefficient for term in terms) != 0:
            raise ValueError("contrast coefficients must sum to zero")
        object.__setattr__(self, "contrast_id", _required_text(self.contrast_id, "contrast_id"))
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "primary_metric", _closed_text(self.primary_metric, "primary_metric", _PRIMARY_METRICS))
        object.__setattr__(self, "role", _enum(ContrastRole, self.role, "role"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contrast_id": self.contrast_id,
            "terms": [term.to_dict() for term in self.terms],
            "primary_metric": self.primary_metric,
            "role": self.role.value,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldContrastDefinition) -> WorldContrastDefinition:
        if isinstance(value, WorldContrastDefinition):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("contrast must be WorldContrastDefinition or a mapping")
        return cls(
            contrast_id=value.get("contrast_id"),
            terms=value.get("terms") or (),
            primary_metric=value.get("primary_metric"),
            role=value.get("role"),
        )


def _lane_tuple(value: Sequence[Any] | None) -> tuple[WorldLaneDefinition, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("lanes must be a sequence of WorldLaneDefinition")
    return tuple(item if isinstance(item, WorldLaneDefinition) else WorldLaneDefinition.from_mapping(item) for item in value)


def _sensor_tuple(value: Sequence[Any] | None) -> tuple[WorldSensorRequirement, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("sensor_requirements must be a sequence of WorldSensorRequirement")
    return tuple(
        item if isinstance(item, WorldSensorRequirement) else WorldSensorRequirement.from_mapping(item) for item in value
    )


def _contrast_tuple(value: Sequence[Any] | None) -> tuple[WorldContrastDefinition, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("contrasts must be a sequence of WorldContrastDefinition")
    return tuple(
        item if isinstance(item, WorldContrastDefinition) else WorldContrastDefinition.from_mapping(item) for item in value
    )


def _require_formal_lanes(lanes: Sequence[WorldLaneDefinition]) -> None:
    by_id = {lane.lane_id for lane in lanes}
    missing: list[str] = []
    for family in _FORMAL_FAMILIES:
        for logical in _FORMAL_LOGICAL_LANES:
            lane_id = f"{family}.{logical}"
            if lane_id not in by_id:
                missing.append(lane_id)
    if missing:
        raise ValueError("prospective_evaluation requires markov/gru market/status_only/company/macro/joint lanes")


@dataclass(frozen=True)
class WorldCohortManifest:
    cohort_id: str
    study_kind: StudyKind | str
    created_at: datetime | str
    question: str
    planned_start_not_before: datetime | str
    collection_stop_rule: WorldCollectionStopRule | Mapping[str, Any]
    venues: Sequence[str]
    bar_interval: str
    horizons: Sequence[str]
    primary_horizon: str
    label_contract: str
    sampling_policy_version: str
    market_feature_contract: str
    context_feature_contract: str
    ontology_revision: str
    scope_mapping: WorldScopeMappingRef | Mapping[str, Any] | None
    sensor_requirements: Sequence[WorldSensorRequirement | Mapping[str, Any]]
    lanes: Sequence[WorldLaneDefinition | Mapping[str, Any]]
    contrasts: Sequence[WorldContrastDefinition | Mapping[str, Any]]
    statistical_protocol: WorldStatisticalProtocol | Mapping[str, Any]
    support_gates: WorldSupportGates | Mapping[str, Any]
    runtime_identity: WorldRuntimeIdentity | Mapping[str, Any]
    authority: str = COHORT_AUTHORITY
    decision_effect: str = COHORT_DECISION_EFFECT
    causal_claim: bool = False
    pnl_claim: bool = False
    schema_version: str = WORLD_COHORT_MANIFEST_SCHEMA
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_COHORT_MANIFEST_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_COHORT_MANIFEST_SCHEMA}")
        cohort_id = _validate_prefixed_id(self.cohort_id, _COHORT_ID_PREFIX, "cohort_id")
        study_kind = _enum(StudyKind, self.study_kind, "study_kind")
        created_at = parse_utc_timestamp(self.created_at, "created_at")
        planned_start = parse_utc_timestamp(self.planned_start_not_before, "planned_start_not_before")
        stop_rule = WorldCollectionStopRule.from_mapping(self.collection_stop_rule)
        if planned_start >= stop_rule.at:
            raise ValueError("planned_start_not_before must precede collection_stop_rule.at")
        venues = _non_empty_text_tuple(self.venues, "venues")
        horizons = _non_empty_text_tuple(self.horizons, "horizons")
        unknown_horizons = [item for item in horizons if item not in _KNOWN_HORIZONS]
        if unknown_horizons:
            raise ValueError("horizons must use supported elapsed-time identities")
        primary_horizon = _required_text(self.primary_horizon, "primary_horizon")
        if primary_horizon not in set(horizons):
            raise ValueError("primary_horizon must be one of the declared horizons")
        lanes = _lane_tuple(self.lanes)
        if not lanes:
            raise ValueError("lanes must not be empty")
        lane_ids = [lane.lane_id for lane in lanes]
        if len(lane_ids) != len(set(lane_ids)):
            raise ValueError("lane_ids must be unique")
        sensors = _sensor_tuple(self.sensor_requirements)
        sensor_ids = [item.sensor_id for item in sensors]
        if len(sensor_ids) != len(set(sensor_ids)):
            raise ValueError("sensor_id must be unique")
        known_lanes = set(lane_ids)
        for sensor in sensors:
            unknown = [lane_id for lane_id in sensor.lane_ids if lane_id not in known_lanes]
            if unknown:
                raise ValueError("sensor requirement references an unknown lane")
        contrasts = _contrast_tuple(self.contrasts)
        if not contrasts:
            raise ValueError("contrasts must not be empty")
        contrast_ids = [item.contrast_id for item in contrasts]
        if len(contrast_ids) != len(set(contrast_ids)):
            raise ValueError("contrast_id must be unique")
        covered: set[str] = set()
        for contrast in contrasts:
            for term in contrast.terms:
                if term.lane_id not in known_lanes:
                    raise ValueError(f"unknown lane in contrast: {term.lane_id}")
                covered.add(term.lane_id)
        uncovered_primary = [
            lane.lane_id for lane in lanes if lane.role.value in _PRIMARY_LANE_ROLES and lane.lane_id not in covered
        ]
        if uncovered_primary:
            raise ValueError("every primary lane must be covered by at least one contrast")
        market_contract = _required_text(self.market_feature_contract, "market_feature_contract")
        context_contract = _required_text(self.context_feature_contract, "context_feature_contract")
        allowed_contracts = {market_contract, context_contract}
        for lane in lanes:
            if lane.feature_contract_id not in allowed_contracts:
                raise ValueError("lane feature_contract_id must match a declared market/context contract")
        scope_mapping = WorldScopeMappingRef.from_mapping(self.scope_mapping)
        needs_scope = any(_lane_requires_scope(lane.lane_id) for lane in lanes)
        if needs_scope and scope_mapping is None:
            raise ValueError("scope_mapping is required when a macro/graph lane is present")
        if not needs_scope and scope_mapping is not None:
            raise ValueError("scope_mapping must be null when no macro/graph lane uses it")
        support_gates = (
            self.support_gates
            if isinstance(self.support_gates, WorldSupportGates)
            else WorldSupportGates.from_mapping(self.support_gates)
        )
        if study_kind is StudyKind.PROSPECTIVE_EVALUATION:
            if support_gates.mode is not SupportGateMode.FIXED_MINIMUM:
                raise ValueError("prospective_evaluation requires fixed_minimum support gates")
            _require_formal_lanes(lanes)
        authority = _required_text(self.authority, "authority")
        if authority != COHORT_AUTHORITY:
            raise ValueError("authority must be shadow_only")
        decision_effect = _required_text(self.decision_effect, "decision_effect")
        if decision_effect != COHORT_DECISION_EFFECT:
            raise ValueError("decision_effect must be none")
        causal_claim = _required_bool(self.causal_claim, "causal_claim")
        pnl_claim = _required_bool(self.pnl_claim, "pnl_claim")
        if causal_claim:
            raise ValueError("causal_claim must be false")
        if pnl_claim:
            raise ValueError("pnl_claim must be false")
        protocol = (
            self.statistical_protocol
            if isinstance(self.statistical_protocol, WorldStatisticalProtocol)
            else WorldStatisticalProtocol.from_mapping(self.statistical_protocol)
        )
        runtime = (
            self.runtime_identity
            if isinstance(self.runtime_identity, WorldRuntimeIdentity)
            else WorldRuntimeIdentity.from_mapping(self.runtime_identity)
        )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "study_kind", study_kind)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "question", _required_text(self.question, "question"))
        object.__setattr__(self, "planned_start_not_before", planned_start)
        object.__setattr__(self, "collection_stop_rule", stop_rule)
        object.__setattr__(self, "venues", venues)
        object.__setattr__(self, "bar_interval", _required_text(self.bar_interval, "bar_interval"))
        object.__setattr__(self, "horizons", horizons)
        object.__setattr__(self, "primary_horizon", primary_horizon)
        object.__setattr__(self, "label_contract", _required_text(self.label_contract, "label_contract"))
        object.__setattr__(
            self,
            "sampling_policy_version",
            _required_text(self.sampling_policy_version, "sampling_policy_version"),
        )
        object.__setattr__(self, "market_feature_contract", market_contract)
        object.__setattr__(self, "context_feature_contract", context_contract)
        object.__setattr__(self, "ontology_revision", _required_text(self.ontology_revision, "ontology_revision"))
        object.__setattr__(self, "scope_mapping", scope_mapping)
        object.__setattr__(self, "sensor_requirements", sensors)
        object.__setattr__(self, "lanes", lanes)
        object.__setattr__(self, "contrasts", contrasts)
        object.__setattr__(self, "statistical_protocol", protocol)
        object.__setattr__(self, "support_gates", support_gates)
        object.__setattr__(self, "runtime_identity", runtime)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "decision_effect", decision_effect)
        object.__setattr__(self, "causal_claim", causal_claim)
        object.__setattr__(self, "pnl_claim", pnl_claim)
        digest = canonical_sha256(self.content_payload())
        if self.manifest_sha256 is not None and _sha256_hex(self.manifest_sha256, "manifest_sha256") != digest:
            raise ValueError("manifest_sha256 does not match the canonical WorldCohortManifest")
        object.__setattr__(self, "manifest_sha256", digest)

    @property
    def lane_by_id(self) -> Mapping[str, WorldLaneDefinition]:
        return MappingProxyType({lane.lane_id: lane for lane in self.lanes})

    def has_graph_lanes(self) -> bool:
        return any(_lane_logical_name(lane.lane_id) == "graph" for lane in self.lanes)

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cohort_id": self.cohort_id,
            "study_kind": self.study_kind.value,
            "created_at": _iso(self.created_at),
            "question": self.question,
            "planned_start_not_before": _iso(self.planned_start_not_before),
            "collection_stop_rule": self.collection_stop_rule.to_dict(),
            "venues": sorted(self.venues),
            "bar_interval": self.bar_interval,
            "horizons": list(self.horizons),
            "primary_horizon": self.primary_horizon,
            "label_contract": self.label_contract,
            "sampling_policy_version": self.sampling_policy_version,
            "market_feature_contract": self.market_feature_contract,
            "context_feature_contract": self.context_feature_contract,
            "ontology_revision": self.ontology_revision,
            "scope_mapping": None if self.scope_mapping is None else self.scope_mapping.to_dict(),
            "sensor_requirements": [
                item.to_dict() for item in sorted(self.sensor_requirements, key=lambda item: item.sensor_id)
            ],
            "lanes": [item.to_dict() for item in sorted(self.lanes, key=lambda item: item.lane_id)],
            "contrasts": [item.to_dict() for item in sorted(self.contrasts, key=lambda item: item.contrast_id)],
            "statistical_protocol": self.statistical_protocol.to_dict(),
            "support_gates": self.support_gates.to_dict(),
            "runtime_identity": self.runtime_identity.to_dict(),
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "causal_claim": self.causal_claim,
            "pnl_claim": self.pnl_claim,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = {
            **self.content_payload(),
            "venues": list(self.venues),
            "sensor_requirements": [item.to_dict() for item in self.sensor_requirements],
            "lanes": [item.to_dict() for item in self.lanes],
            "contrasts": [item.to_dict() for item in self.contrasts],
            "manifest_sha256": self.manifest_sha256,
        }
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortManifest) -> WorldCohortManifest:
        if isinstance(value, WorldCohortManifest):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("manifest must be WorldCohortManifest or a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            study_kind=value.get("study_kind"),
            created_at=value.get("created_at"),
            question=value.get("question"),
            planned_start_not_before=value.get("planned_start_not_before"),
            collection_stop_rule=value.get("collection_stop_rule"),
            venues=value.get("venues") or (),
            bar_interval=value.get("bar_interval"),
            horizons=value.get("horizons") or (),
            primary_horizon=value.get("primary_horizon"),
            label_contract=value.get("label_contract"),
            sampling_policy_version=value.get("sampling_policy_version"),
            market_feature_contract=value.get("market_feature_contract"),
            context_feature_contract=value.get("context_feature_contract"),
            ontology_revision=value.get("ontology_revision"),
            scope_mapping=value.get("scope_mapping"),
            sensor_requirements=value.get("sensor_requirements") or (),
            lanes=value.get("lanes") or (),
            contrasts=value.get("contrasts") or (),
            statistical_protocol=value.get("statistical_protocol"),
            support_gates=value.get("support_gates"),
            runtime_identity=value.get("runtime_identity"),
            authority=value.get("authority", COHORT_AUTHORITY),
            decision_effect=value.get("decision_effect", COHORT_DECISION_EFFECT),
            causal_claim=value.get("causal_claim", False),
            pnl_claim=value.get("pnl_claim", False),
            schema_version=value.get("schema_version", WORLD_COHORT_MANIFEST_SCHEMA),
            manifest_sha256=value.get("manifest_sha256"),
        )


def _slot_identity_payload(
    *,
    cohort_id: str,
    venue: str,
    symbol: str,
    bar_interval: str,
    as_of_bar_ts: datetime,
) -> dict[str, str]:
    return {
        "cohort_id": cohort_id,
        "venue": venue,
        "symbol": symbol,
        "bar_interval": bar_interval,
        "as_of_bar_ts": _iso(as_of_bar_ts),
    }


@dataclass(frozen=True)
class WorldCohortSlot:
    cohort_id: str
    manifest_sha256: str
    venue: str
    symbol: str
    bar_interval: str
    as_of_bar_ts: datetime | str
    anchor_end_at: datetime | str
    comparison_batch_id: str
    episode_refs_by_contract: Mapping[str, str]
    expected_lane_ids: Sequence[str]
    feature_contract_fingerprints: Mapping[str, str]
    feature_mask_fingerprints: Mapping[str, str]
    started_event_id: str
    scope_resolution: WorldScopeResolution | Mapping[str, Any] | None = None
    schema_version: str = WORLD_COHORT_SLOT_SCHEMA
    slot_id: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_COHORT_SLOT_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_COHORT_SLOT_SCHEMA}")
        cohort_id = _validate_prefixed_id(self.cohort_id, _COHORT_ID_PREFIX, "cohort_id")
        as_of = parse_utc_timestamp(self.as_of_bar_ts, "as_of_bar_ts")
        anchor_end_at = parse_utc_timestamp(self.anchor_end_at, "anchor_end_at")
        if as_of > anchor_end_at:
            raise ValueError("as_of_bar_ts must not follow anchor_end_at")
        expected_lane_ids = _non_empty_text_tuple(self.expected_lane_ids, "expected_lane_ids")
        episode_refs = _text_map(self.episode_refs_by_contract, "episode_refs_by_contract")
        if not episode_refs:
            raise ValueError("episode_refs_by_contract must not be empty")
        contract_fps = _text_map(self.feature_contract_fingerprints, "feature_contract_fingerprints")
        mask_fps = _text_map(self.feature_mask_fingerprints, "feature_mask_fingerprints")
        for lane_id in expected_lane_ids:
            if lane_id not in contract_fps or lane_id not in mask_fps:
                raise ValueError("slot fingerprints must cover every expected lane")
        resolution = None
        if self.scope_resolution is not None:
            resolution = (
                self.scope_resolution
                if isinstance(self.scope_resolution, WorldScopeResolution)
                else WorldScopeResolution.from_mapping(self.scope_resolution)
            )
            if resolution.anchor.market_venue != _required_text(self.venue, "venue") or resolution.anchor.instrument != _required_text(
                self.symbol, "symbol"
            ):
                raise ValueError("scope resolution anchor must match the slot market identity")
        slot_id = _prefixed_id(
            _SLOT_ID_PREFIX,
            _slot_identity_payload(
                cohort_id=cohort_id,
                venue=_required_text(self.venue, "venue"),
                symbol=_required_text(self.symbol, "symbol"),
                bar_interval=_required_text(self.bar_interval, "bar_interval"),
                as_of_bar_ts=as_of,
            ),
        )
        if self.slot_id is not None and _required_text(self.slot_id, "slot_id") != slot_id:
            raise ValueError("slot_id does not match the canonical cohort market anchor")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", _sha256_hex(self.manifest_sha256, "manifest_sha256"))
        object.__setattr__(self, "venue", _required_text(self.venue, "venue"))
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "bar_interval", _required_text(self.bar_interval, "bar_interval"))
        object.__setattr__(self, "as_of_bar_ts", as_of)
        object.__setattr__(self, "anchor_end_at", anchor_end_at)
        object.__setattr__(self, "comparison_batch_id", _required_text(self.comparison_batch_id, "comparison_batch_id"))
        object.__setattr__(self, "episode_refs_by_contract", episode_refs)
        object.__setattr__(self, "expected_lane_ids", expected_lane_ids)
        object.__setattr__(self, "feature_contract_fingerprints", contract_fps)
        object.__setattr__(self, "feature_mask_fingerprints", mask_fps)
        object.__setattr__(self, "scope_resolution", resolution)
        object.__setattr__(
            self,
            "started_event_id",
            _validate_prefixed_id(self.started_event_id, _EVENT_ID_PREFIX, "started_event_id"),
        )
        object.__setattr__(self, "slot_id", slot_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "slot_id": self.slot_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "venue": self.venue,
            "symbol": self.symbol,
            "bar_interval": self.bar_interval,
            "as_of_bar_ts": _iso(self.as_of_bar_ts),
            "anchor_end_at": _iso(self.anchor_end_at),
            "comparison_batch_id": self.comparison_batch_id,
            "episode_refs_by_contract": dict(self.episode_refs_by_contract),
            "expected_lane_ids": list(self.expected_lane_ids),
            "feature_contract_fingerprints": dict(self.feature_contract_fingerprints),
            "feature_mask_fingerprints": dict(self.feature_mask_fingerprints),
            "scope_resolution": None if self.scope_resolution is None else self.scope_resolution.to_dict(),
            "started_event_id": self.started_event_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortSlot) -> WorldCohortSlot:
        if isinstance(value, WorldCohortSlot):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("slot must be WorldCohortSlot or a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            venue=value.get("venue"),
            symbol=value.get("symbol"),
            bar_interval=value.get("bar_interval"),
            as_of_bar_ts=value.get("as_of_bar_ts"),
            anchor_end_at=value.get("anchor_end_at"),
            comparison_batch_id=value.get("comparison_batch_id"),
            episode_refs_by_contract=value.get("episode_refs_by_contract") or {},
            expected_lane_ids=value.get("expected_lane_ids") or (),
            feature_contract_fingerprints=value.get("feature_contract_fingerprints") or {},
            feature_mask_fingerprints=value.get("feature_mask_fingerprints") or {},
            scope_resolution=value.get("scope_resolution"),
            started_event_id=value.get("started_event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_SLOT_SCHEMA),
            slot_id=value.get("slot_id"),
        )


@dataclass(frozen=True)
class WorldCohortCompletionHorizonLeaf:
    horizon_id: str
    status: str
    digest: str

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in OUTCOME_STATUSES:
            allowed = ", ".join(sorted(OUTCOME_STATUSES))
            raise ValueError(f"horizon leaf status must be one of: {allowed}")
        if status not in _TERMINAL_OUTCOME_STATUSES:
            raise ValueError("completion evidence requires terminal horizon leaves")
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "digest", _sha256_hex(self.digest, "digest"))

    def to_dict(self) -> dict[str, str]:
        return {"horizon_id": self.horizon_id, "status": self.status, "digest": self.digest}

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | WorldCohortCompletionHorizonLeaf
    ) -> WorldCohortCompletionHorizonLeaf:
        if isinstance(value, WorldCohortCompletionHorizonLeaf):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("horizon leaf must be WorldCohortCompletionHorizonLeaf or a mapping")
        return cls(horizon_id=value.get("horizon_id"), status=value.get("status"), digest=value.get("digest"))


@dataclass(frozen=True)
class WorldCohortCompletionSlotEvidence:
    slot_id: str
    leaves: Sequence[WorldCohortCompletionHorizonLeaf | Mapping[str, Any]]

    def __post_init__(self) -> None:
        if isinstance(self.leaves, (str, bytes, bytearray)) or not isinstance(self.leaves, Sequence):
            raise TypeError("leaves must be a sequence of WorldCohortCompletionHorizonLeaf")
        leaves = tuple(
            item if isinstance(item, WorldCohortCompletionHorizonLeaf) else WorldCohortCompletionHorizonLeaf.from_mapping(item)
            for item in self.leaves
        )
        if not leaves:
            raise ValueError("completion slot evidence requires horizon leaves")
        horizon_ids = [item.horizon_id for item in leaves]
        if len(horizon_ids) != len(set(horizon_ids)):
            raise ValueError("completion leaves must not repeat a horizon")
        object.__setattr__(
            self,
            "slot_id",
            _validate_prefixed_id(self.slot_id, _SLOT_ID_PREFIX, "slot_id"),
        )
        object.__setattr__(self, "leaves", leaves)

    def to_dict(self) -> dict[str, Any]:
        return {"slot_id": self.slot_id, "leaves": [item.to_dict() for item in self.leaves]}

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | WorldCohortCompletionSlotEvidence
    ) -> WorldCohortCompletionSlotEvidence:
        if isinstance(value, WorldCohortCompletionSlotEvidence):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("slot evidence must be WorldCohortCompletionSlotEvidence or a mapping")
        return cls(slot_id=value.get("slot_id"), leaves=value.get("leaves") or ())


@dataclass(frozen=True)
class WorldCohortCompletionEvidence:
    slots: Sequence[WorldCohortCompletionSlotEvidence | Mapping[str, Any]]

    def __post_init__(self) -> None:
        if isinstance(self.slots, (str, bytes, bytearray)) or not isinstance(self.slots, Sequence):
            raise TypeError("slots must be a sequence of WorldCohortCompletionSlotEvidence")
        slots = tuple(
            item if isinstance(item, WorldCohortCompletionSlotEvidence) else WorldCohortCompletionSlotEvidence.from_mapping(item)
            for item in self.slots
        )
        slot_ids = [item.slot_id for item in slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("completion evidence must not repeat a slot_id")
        object.__setattr__(self, "slots", slots)

    def to_dict(self) -> dict[str, Any]:
        return {"slots": [item.to_dict() for item in self.slots]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortCompletionEvidence) -> WorldCohortCompletionEvidence:
        if isinstance(value, WorldCohortCompletionEvidence):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("completion evidence must be WorldCohortCompletionEvidence or a mapping")
        return cls(slots=value.get("slots") or ())


@dataclass(frozen=True)
class LaneOperationalState:
    lane_id: str
    status: LaneOperationalStatus | str
    reason: LaneBlockReason | None = None
    affected_from: datetime | str | None = None
    affected_until: datetime | str | None = None

    def __post_init__(self) -> None:
        status = _enum(LaneOperationalStatus, self.status, "status")
        reason: LaneBlockReason | None
        if self.reason is None:
            reason = None
        elif isinstance(self.reason, LaneBlockReason):
            reason = self.reason
        else:
            raise TypeError("reason must be LaneBlockReason")
        if status is LaneOperationalStatus.BLOCKED and reason is None:
            raise ValueError("blocked lane state requires a typed reason")
        if status is LaneOperationalStatus.ACTIVE and reason is not None:
            raise ValueError("active lane state must not carry a block reason")
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(
            self,
            "affected_from",
            None if self.affected_from is None else parse_utc_timestamp(self.affected_from, "affected_from"),
        )
        object.__setattr__(
            self,
            "affected_until",
            None if self.affected_until is None else parse_utc_timestamp(self.affected_until, "affected_until"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "status": self.status.value,
            "reason": None if self.reason is None else self.reason.value,
            "affected_from": None if self.affected_from is None else _iso(self.affected_from),
            "affected_until": None if self.affected_until is None else _iso(self.affected_until),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | LaneOperationalState) -> LaneOperationalState:
        if isinstance(value, LaneOperationalState):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("lane operational state must be LaneOperationalState or a mapping")
        reason_raw = value.get("reason")
        reason: LaneBlockReason | None
        if reason_raw is None:
            reason = None
        elif isinstance(reason_raw, LaneBlockReason):
            reason = reason_raw
        elif isinstance(reason_raw, str):
            reason = LaneBlockReason(reason_raw)
        else:
            raise TypeError("reason must be LaneBlockReason")
        return cls(
            lane_id=value.get("lane_id"),
            status=value.get("status"),
            reason=reason,
            affected_from=value.get("affected_from"),
            affected_until=value.get("affected_until"),
        )


def _event_id_for(payload: Mapping[str, Any]) -> str:
    return _prefixed_id(_EVENT_ID_PREFIX, payload)


def _set_event_identity(event: Any, payload: Mapping[str, Any]) -> None:
    computed = _event_id_for(payload)
    provided = getattr(event, "event_id")
    if provided is not None and _required_text(provided, "event_id") != computed:
        raise ValueError("event_id does not match the canonical cohort event")
    schema_version = _required_text(getattr(event, "schema_version"), "schema_version")
    if schema_version != WORLD_COHORT_EVENT_SCHEMA:
        raise ValueError(f"schema_version must be {WORLD_COHORT_EVENT_SCHEMA}")
    object.__setattr__(event, "schema_version", schema_version)
    object.__setattr__(event, "event_id", computed)


def _require_event_ids(event: Any, *, cohort_id: Any, manifest_sha256: Any) -> tuple[str, str]:
    return (
        _validate_prefixed_id(cohort_id, _COHORT_ID_PREFIX, "cohort_id"),
        _sha256_hex(manifest_sha256, "manifest_sha256"),
    )


@dataclass(frozen=True)
class WorldCohortRegistered:
    cohort_id: str
    manifest_sha256: str
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_registered"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "event_type", "world_cohort_registered")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_registered",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
            },
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortRegistered) -> WorldCohortRegistered:
        if isinstance(value, WorldCohortRegistered):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("registered event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortArmed:
    cohort_id: str
    manifest_sha256: str
    runtime_identity: WorldRuntimeIdentity | Mapping[str, Any]
    satisfied_sensor_ids: Sequence[str] = ()
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_armed"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        runtime = (
            self.runtime_identity
            if isinstance(self.runtime_identity, WorldRuntimeIdentity)
            else WorldRuntimeIdentity.from_mapping(self.runtime_identity)
        )
        satisfied = _immutable_text_tuple(self.satisfied_sensor_ids, "satisfied_sensor_ids")
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "runtime_identity", runtime)
        object.__setattr__(self, "satisfied_sensor_ids", satisfied)
        object.__setattr__(self, "event_type", "world_cohort_armed")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_armed",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "runtime_identity": runtime.to_dict(),
                "satisfied_sensor_ids": list(satisfied),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "runtime_identity": self.runtime_identity.to_dict(),
            "satisfied_sensor_ids": list(self.satisfied_sensor_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortArmed) -> WorldCohortArmed:
        if isinstance(value, WorldCohortArmed):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("armed event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            runtime_identity=value.get("runtime_identity"),
            satisfied_sensor_ids=value.get("satisfied_sensor_ids") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortStarted:
    cohort_id: str
    manifest_sha256: str
    runtime_identity: WorldRuntimeIdentity | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_started"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        runtime = (
            self.runtime_identity
            if isinstance(self.runtime_identity, WorldRuntimeIdentity)
            else WorldRuntimeIdentity.from_mapping(self.runtime_identity)
        )
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "runtime_identity", runtime)
        object.__setattr__(self, "event_type", "world_cohort_started")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_started",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "runtime_identity": runtime.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "runtime_identity": self.runtime_identity.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortStarted) -> WorldCohortStarted:
        if isinstance(value, WorldCohortStarted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("started event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            runtime_identity=value.get("runtime_identity"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortSlotAdmitted:
    slot: WorldCohortSlot | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_slot_admitted"

    def __post_init__(self) -> None:
        slot = self.slot if isinstance(self.slot, WorldCohortSlot) else WorldCohortSlot.from_mapping(self.slot)
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "event_type", "world_cohort_slot_admitted")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_slot_admitted",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "slot": slot.to_dict(),
            },
        )

    @property
    def cohort_id(self) -> str:
        return self.slot.cohort_id

    @property
    def manifest_sha256(self) -> str:
        return self.slot.manifest_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "slot": self.slot.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortSlotAdmitted) -> WorldCohortSlotAdmitted:
        if isinstance(value, WorldCohortSlotAdmitted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("slot admitted event must be a mapping")
        return cls(
            slot=value.get("slot"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortLaneBlocked:
    cohort_id: str
    manifest_sha256: str
    state: LaneOperationalState | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_lane_blocked"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        state = self.state if isinstance(self.state, LaneOperationalState) else LaneOperationalState.from_mapping(self.state)
        if state.status is not LaneOperationalStatus.BLOCKED:
            raise ValueError("blocked event requires a blocked LaneOperationalState")
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "event_type", "world_cohort_lane_blocked")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_lane_blocked",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "state": state.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortLaneBlocked) -> WorldCohortLaneBlocked:
        if isinstance(value, WorldCohortLaneBlocked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("lane blocked event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            state=value.get("state"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortLaneRestored:
    cohort_id: str
    manifest_sha256: str
    state: LaneOperationalState | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_lane_restored"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        state = self.state if isinstance(self.state, LaneOperationalState) else LaneOperationalState.from_mapping(self.state)
        if state.status is not LaneOperationalStatus.ACTIVE:
            raise ValueError("restored event requires an active LaneOperationalState")
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "event_type", "world_cohort_lane_restored")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_lane_restored",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "state": state.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortLaneRestored) -> WorldCohortLaneRestored:
        if isinstance(value, WorldCohortLaneRestored):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("lane restored event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            state=value.get("state"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortCollectionClosed:
    cohort_id: str
    manifest_sha256: str
    reason: str
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_collection_closed"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        object.__setattr__(self, "event_type", "world_cohort_collection_closed")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_collection_closed",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "reason": self.reason,
            },
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortCollectionClosed) -> WorldCohortCollectionClosed:
        if isinstance(value, WorldCohortCollectionClosed):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("collection closed event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortCompleted:
    cohort_id: str
    manifest_sha256: str
    evidence: WorldCohortCompletionEvidence | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_completed"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        evidence = (
            self.evidence
            if isinstance(self.evidence, WorldCohortCompletionEvidence)
            else WorldCohortCompletionEvidence.from_mapping(self.evidence)
        )
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "event_type", "world_cohort_completed")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_completed",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "evidence": evidence.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortCompleted) -> WorldCohortCompleted:
        if isinstance(value, WorldCohortCompleted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("completed event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            evidence=value.get("evidence"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldCohortInvalidated:
    cohort_id: str
    manifest_sha256: str
    reason: InvalidationReason | str
    scope: str
    proofs: Sequence[str]
    occurred_at: datetime | str
    event_id: str | None = None
    schema_version: str = WORLD_COHORT_EVENT_SCHEMA
    event_type: str = "world_cohort_invalidated"

    def __post_init__(self) -> None:
        cohort_id, manifest_sha256 = _require_event_ids(
            self, cohort_id=self.cohort_id, manifest_sha256=self.manifest_sha256
        )
        proofs = _non_empty_text_tuple(self.proofs, "proofs")
        object.__setattr__(self, "cohort_id", cohort_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "reason", _enum(InvalidationReason, self.reason, "reason"))
        object.__setattr__(self, "scope", _required_text(self.scope, "scope"))
        object.__setattr__(self, "proofs", proofs)
        object.__setattr__(self, "occurred_at", parse_utc_timestamp(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "event_type", "world_cohort_invalidated")
        _set_event_identity(
            self,
            {
                "event_type": "world_cohort_invalidated",
                "schema_version": WORLD_COHORT_EVENT_SCHEMA,
                "cohort_id": cohort_id,
                "manifest_sha256": manifest_sha256,
                "reason": self.reason.value,
                "scope": self.scope,
                "proofs": list(proofs),
                "occurred_at": _iso(self.occurred_at),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "reason": self.reason.value,
            "scope": self.scope,
            "proofs": list(self.proofs),
            "occurred_at": _iso(self.occurred_at),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortInvalidated) -> WorldCohortInvalidated:
        if isinstance(value, WorldCohortInvalidated):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("invalidated event must be a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            reason=value.get("reason"),
            scope=value.get("scope"),
            proofs=value.get("proofs") or (),
            occurred_at=value.get("occurred_at"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_COHORT_EVENT_SCHEMA),
        )


WorldCohortEvent = (
    WorldCohortRegistered
    | WorldCohortArmed
    | WorldCohortStarted
    | WorldCohortSlotAdmitted
    | WorldCohortLaneBlocked
    | WorldCohortLaneRestored
    | WorldCohortCollectionClosed
    | WorldCohortCompleted
    | WorldCohortInvalidated
)

WORLD_COHORT_EVENTS = (
    WorldCohortRegistered,
    WorldCohortArmed,
    WorldCohortStarted,
    WorldCohortSlotAdmitted,
    WorldCohortLaneBlocked,
    WorldCohortLaneRestored,
    WorldCohortCollectionClosed,
    WorldCohortCompleted,
    WorldCohortInvalidated,
)
_EVENT_PARSERS = {
    "world_cohort_registered": WorldCohortRegistered.from_mapping,
    "world_cohort_armed": WorldCohortArmed.from_mapping,
    "world_cohort_started": WorldCohortStarted.from_mapping,
    "world_cohort_slot_admitted": WorldCohortSlotAdmitted.from_mapping,
    "world_cohort_lane_blocked": WorldCohortLaneBlocked.from_mapping,
    "world_cohort_lane_restored": WorldCohortLaneRestored.from_mapping,
    "world_cohort_collection_closed": WorldCohortCollectionClosed.from_mapping,
    "world_cohort_completed": WorldCohortCompleted.from_mapping,
    "world_cohort_invalidated": WorldCohortInvalidated.from_mapping,
}


def parse_world_cohort_event(value: Mapping[str, Any] | WorldCohortEvent) -> WorldCohortEvent:
    if isinstance(value, WORLD_COHORT_EVENTS):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("cohort event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown world cohort event_type: {event_type}") from exc
    return parser(value)


def world_cohort_event_payload_hash(event: WorldCohortEvent | Mapping[str, Any]) -> str:
    parsed = parse_world_cohort_event(event)
    return canonical_sha256(parsed.to_dict())


@dataclass(frozen=True)
class WorldCohortEventEnvelope:
    event: WorldCohortEvent | Mapping[str, Any]
    sequence: int
    evidence: AvailabilityEvidence | None = None
    schema_version: str = WORLD_COHORT_EVENT_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_COHORT_EVENT_ENVELOPE_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_COHORT_EVENT_ENVELOPE_SCHEMA}")
        sequence = _required_int(self.sequence, "sequence", minimum=1)
        event = parse_world_cohort_event(self.event)
        payload_hash = world_cohort_event_payload_hash(event)
        if self.evidence is not None:
            if not isinstance(self.evidence, AvailabilityEvidence):
                raise TypeError("envelope evidence must be AvailabilityEvidence")
            subject = self.evidence.receipt.subject
            if subject.kind != _EVENT_SUBJECT_KIND:
                raise ValueError("availability evidence subject kind must be world_cohort_event")
            if subject.subject_id != event.event_id:
                raise ValueError("availability evidence subject_id must match the event_id")
            if subject.content_sha256 != payload_hash:
                raise ValueError("availability evidence content_sha256 must match the event payload hash")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "evidence", self.evidence)
        object.__setattr__(self, "payload_hash", payload_hash)

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def cohort_id(self) -> str:
        return self.event.cohort_id

    @property
    def manifest_sha256(self) -> str:
        return self.event.manifest_sha256

    @property
    def availability_status(self) -> str:
        if self.evidence is None:
            return "availability_unproven"
        return "eligible"

    def require_proven(self) -> AvailabilityEvidence:
        if self.evidence is None:
            raise ValueError("event envelope is availability_unproven")
        return self.evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "sequence": self.sequence,
            "payload_hash": self.payload_hash,
            "availability_status": self.availability_status,
            "event": self.event.to_dict(),
        }

    @classmethod
    def bind(
        cls,
        event: WorldCohortEvent | Mapping[str, Any],
        *,
        sequence: int,
        evidence: AvailabilityEvidence | None = None,
    ) -> WorldCohortEventEnvelope:
        return cls(event=event, sequence=sequence, evidence=evidence)


@dataclass(frozen=True)
class RegisterWorldCohort:
    manifest: WorldCohortManifest | Mapping[str, Any]

    def __post_init__(self) -> None:
        manifest = (
            self.manifest if isinstance(self.manifest, WorldCohortManifest) else WorldCohortManifest.from_mapping(self.manifest)
        )
        object.__setattr__(self, "manifest", manifest)


@dataclass(frozen=True)
class ArmWorldCohort:
    cohort_id: str
    manifest_sha256: str
    runtime_identity: WorldRuntimeIdentity | Mapping[str, Any]
    satisfied_sensor_ids: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cohort_id", _validate_prefixed_id(self.cohort_id, _COHORT_ID_PREFIX, "cohort_id"))
        object.__setattr__(self, "manifest_sha256", _sha256_hex(self.manifest_sha256, "manifest_sha256"))
        runtime = (
            self.runtime_identity
            if isinstance(self.runtime_identity, WorldRuntimeIdentity)
            else WorldRuntimeIdentity.from_mapping(self.runtime_identity)
        )
        object.__setattr__(self, "runtime_identity", runtime)
        object.__setattr__(
            self,
            "satisfied_sensor_ids",
            _immutable_text_tuple(self.satisfied_sensor_ids, "satisfied_sensor_ids"),
        )


@dataclass(frozen=True)
class StartWorldCohort:
    cohort_id: str
    manifest_sha256: str
    runtime_identity: WorldRuntimeIdentity | Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cohort_id", _validate_prefixed_id(self.cohort_id, _COHORT_ID_PREFIX, "cohort_id"))
        object.__setattr__(self, "manifest_sha256", _sha256_hex(self.manifest_sha256, "manifest_sha256"))
        runtime = (
            self.runtime_identity
            if isinstance(self.runtime_identity, WorldRuntimeIdentity)
            else WorldRuntimeIdentity.from_mapping(self.runtime_identity)
        )
        object.__setattr__(self, "runtime_identity", runtime)


@dataclass(frozen=True)
class AdmitWorldCohortSlot:
    slot: WorldCohortSlot | Mapping[str, Any]
    started_evidence: AvailabilityEvidence | None

    def __post_init__(self) -> None:
        if self.started_evidence is None:
            raise ValueError("started_evidence is required; unproven start is not admissible")
        if not isinstance(self.started_evidence, AvailabilityEvidence):
            raise TypeError("started_evidence must be AvailabilityEvidence")
        slot = self.slot if isinstance(self.slot, WorldCohortSlot) else WorldCohortSlot.from_mapping(self.slot)
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "started_evidence", self.started_evidence)


@dataclass(frozen=True)
class BlockWorldCohortLane:
    lane_id: str
    reason: LaneBlockReason
    affected_from: datetime | str | None = None
    affected_until: datetime | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, LaneBlockReason):
            raise TypeError("reason must be LaneBlockReason")
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))
        object.__setattr__(
            self,
            "affected_from",
            None if self.affected_from is None else parse_utc_timestamp(self.affected_from, "affected_from"),
        )
        object.__setattr__(
            self,
            "affected_until",
            None if self.affected_until is None else parse_utc_timestamp(self.affected_until, "affected_until"),
        )


@dataclass(frozen=True)
class RestoreWorldCohortLane:
    lane_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))


@dataclass(frozen=True)
class CloseWorldCohort:
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))


@dataclass(frozen=True)
class CompleteWorldCohort:
    evidence: WorldCohortCompletionEvidence | Mapping[str, Any]

    def __post_init__(self) -> None:
        evidence = (
            self.evidence
            if isinstance(self.evidence, WorldCohortCompletionEvidence)
            else WorldCohortCompletionEvidence.from_mapping(self.evidence)
        )
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True)
class InvalidateWorldCohort:
    reason: InvalidationReason | str
    scope: str
    proofs: Sequence[str]
    occurred_at: datetime | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _enum(InvalidationReason, self.reason, "reason"))
        object.__setattr__(self, "scope", _required_text(self.scope, "scope"))
        object.__setattr__(self, "proofs", _non_empty_text_tuple(self.proofs, "proofs"))
        object.__setattr__(self, "occurred_at", parse_utc_timestamp(self.occurred_at, "occurred_at"))


WorldCohortCommand = (
    RegisterWorldCohort
    | ArmWorldCohort
    | StartWorldCohort
    | AdmitWorldCohortSlot
    | BlockWorldCohortLane
    | RestoreWorldCohortLane
    | CloseWorldCohort
    | CompleteWorldCohort
    | InvalidateWorldCohort
)

WORLD_COHORT_COMMANDS = (
    RegisterWorldCohort,
    ArmWorldCohort,
    StartWorldCohort,
    AdmitWorldCohortSlot,
    BlockWorldCohortLane,
    RestoreWorldCohortLane,
    CloseWorldCohort,
    CompleteWorldCohort,
    InvalidateWorldCohort,
)


_MATCH_FIELDS = (
    "study_cohort_id",
    "manifest_sha256",
    "venue",
    "symbol",
    "bar_interval",
    "as_of_bar_ts",
    "horizon_id",
    "comparison_batch_id",
    "predicted_at",
    "training_cutoff",
    "training_lineage_fingerprint",
    "label_move_class",
    "label_target_at",
    "label_evidence_digest",
)


@dataclass(frozen=True)
class WorldCohortMatchedMember:
    lane_id: str
    study_cohort_id: str
    manifest_sha256: str
    venue: str
    symbol: str
    bar_interval: str
    as_of_bar_ts: datetime | str
    horizon_id: str
    comparison_batch_id: str
    predicted_at: datetime | str
    training_cutoff: datetime | str
    training_lineage_fingerprint: str
    label_move_class: str
    label_target_at: datetime | str
    label_evidence_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane_id", _required_text(self.lane_id, "lane_id"))
        object.__setattr__(
            self,
            "study_cohort_id",
            _validate_prefixed_id(self.study_cohort_id, _COHORT_ID_PREFIX, "study_cohort_id"),
        )
        object.__setattr__(self, "manifest_sha256", _sha256_hex(self.manifest_sha256, "manifest_sha256"))
        object.__setattr__(self, "venue", _required_text(self.venue, "venue"))
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "bar_interval", _required_text(self.bar_interval, "bar_interval"))
        object.__setattr__(self, "as_of_bar_ts", parse_utc_timestamp(self.as_of_bar_ts, "as_of_bar_ts"))
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))
        object.__setattr__(self, "comparison_batch_id", _required_text(self.comparison_batch_id, "comparison_batch_id"))
        object.__setattr__(self, "predicted_at", parse_utc_timestamp(self.predicted_at, "predicted_at"))
        object.__setattr__(self, "training_cutoff", parse_utc_timestamp(self.training_cutoff, "training_cutoff"))
        object.__setattr__(
            self,
            "training_lineage_fingerprint",
            _sha256_hex(self.training_lineage_fingerprint, "training_lineage_fingerprint"),
        )
        object.__setattr__(self, "label_move_class", _required_text(self.label_move_class, "label_move_class"))
        object.__setattr__(self, "label_target_at", parse_utc_timestamp(self.label_target_at, "label_target_at"))
        object.__setattr__(self, "label_evidence_digest", _sha256_hex(self.label_evidence_digest, "label_evidence_digest"))

    def pairing_key(self) -> dict[str, Any]:
        return {
            "study_cohort_id": self.study_cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "venue": self.venue,
            "symbol": self.symbol,
            "bar_interval": self.bar_interval,
            "as_of_bar_ts": self.as_of_bar_ts,
            "horizon_id": self.horizon_id,
            "comparison_batch_id": self.comparison_batch_id,
            "predicted_at": self.predicted_at,
            "training_cutoff": self.training_cutoff,
            "training_lineage_fingerprint": self.training_lineage_fingerprint,
            "label_move_class": self.label_move_class,
            "label_target_at": self.label_target_at,
            "label_evidence_digest": self.label_evidence_digest,
        }


@dataclass(frozen=True)
class WorldCohortMatchedSet:
    contrast_id: str
    expected_lane_ids: Sequence[str]
    members: Sequence[WorldCohortMatchedMember]
    horizon_id: str | None = None

    def __post_init__(self) -> None:
        expected = _non_empty_text_tuple(self.expected_lane_ids, "expected_lane_ids")
        if isinstance(self.members, (str, bytes, bytearray)) or not isinstance(self.members, Sequence):
            raise TypeError("members must be a sequence of WorldCohortMatchedMember")
        members = tuple(self.members)
        if not members:
            raise ValueError("matched set cannot invent absent members")
        present = [member.lane_id for member in members]
        if len(present) != len(set(present)):
            raise ValueError("matched set must not contain duplicate lanes")
        missing = [lane_id for lane_id in expected if lane_id not in set(present)]
        if missing:
            raise ValueError("matched set has an absent member")
        extra = [lane_id for lane_id in present if lane_id not in set(expected)]
        if extra:
            raise ValueError("matched set must not invent extra members")
        reference = members[0].pairing_key()
        for member in members[1:]:
            current = member.pairing_key()
            for field_name in _MATCH_FIELDS:
                if current[field_name] != reference[field_name]:
                    raise ValueError(f"matched set {field_name} mismatch")
        horizon_id = members[0].horizon_id if self.horizon_id is None else _required_text(self.horizon_id, "horizon_id")
        if horizon_id != members[0].horizon_id:
            raise ValueError("matched set horizon_id mismatch")
        ordered = tuple(sorted(members, key=lambda item: item.lane_id))
        object.__setattr__(self, "contrast_id", _required_text(self.contrast_id, "contrast_id"))
        object.__setattr__(self, "expected_lane_ids", expected)
        object.__setattr__(self, "members", ordered)
        object.__setattr__(self, "horizon_id", horizon_id)


@dataclass(frozen=True)
class WorldCohortReport:
    cohort_id: str
    manifest_sha256: str
    phase: CohortPhase | str
    authority: str = COHORT_AUTHORITY
    decision_effect: str = COHORT_DECISION_EFFECT
    recommendation: str = COHORT_RECOMMENDATION
    causal_claim: bool = False
    pnl_claim: bool = False
    actual_trader_contribution: str = COHORT_TRADER_CONTRIBUTION
    schema_version: str = WORLD_COHORT_REPORT_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_COHORT_REPORT_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_COHORT_REPORT_SCHEMA}")
        authority = _required_text(self.authority, "authority")
        decision_effect = _required_text(self.decision_effect, "decision_effect")
        recommendation = _required_text(self.recommendation, "recommendation")
        if authority != COHORT_AUTHORITY or decision_effect != COHORT_DECISION_EFFECT or recommendation != COHORT_RECOMMENDATION:
            raise ValueError("report must stay shadow_only with decision_effect=none and recommendation=NO_GO")
        causal_claim = _required_bool(self.causal_claim, "causal_claim")
        pnl_claim = _required_bool(self.pnl_claim, "pnl_claim")
        if causal_claim or pnl_claim:
            raise ValueError("report causal_claim and pnl_claim must be false")
        contribution = _required_text(self.actual_trader_contribution, "actual_trader_contribution")
        if contribution != COHORT_TRADER_CONTRIBUTION:
            raise ValueError("actual_trader_contribution must be not_attributable")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "cohort_id", _validate_prefixed_id(self.cohort_id, _COHORT_ID_PREFIX, "cohort_id"))
        object.__setattr__(self, "manifest_sha256", _sha256_hex(self.manifest_sha256, "manifest_sha256"))
        object.__setattr__(self, "phase", _enum(CohortPhase, self.phase, "phase"))
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "decision_effect", decision_effect)
        object.__setattr__(self, "recommendation", recommendation)
        object.__setattr__(self, "causal_claim", causal_claim)
        object.__setattr__(self, "pnl_claim", pnl_claim)
        object.__setattr__(self, "actual_trader_contribution", contribution)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "phase": self.phase.value,
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "recommendation": self.recommendation,
            "causal_claim": self.causal_claim,
            "pnl_claim": self.pnl_claim,
            "actual_trader_contribution": self.actual_trader_contribution,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCohortReport) -> WorldCohortReport:
        if isinstance(value, WorldCohortReport):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("report must be WorldCohortReport or a mapping")
        return cls(
            cohort_id=value.get("cohort_id"),
            manifest_sha256=value.get("manifest_sha256"),
            phase=value.get("phase"),
            authority=value.get("authority", COHORT_AUTHORITY),
            decision_effect=value.get("decision_effect", COHORT_DECISION_EFFECT),
            recommendation=value.get("recommendation", COHORT_RECOMMENDATION),
            causal_claim=value.get("causal_claim", False),
            pnl_claim=value.get("pnl_claim", False),
            actual_trader_contribution=value.get("actual_trader_contribution", COHORT_TRADER_CONTRIBUTION),
            schema_version=value.get("schema_version", WORLD_COHORT_REPORT_SCHEMA),
        )

    @classmethod
    def from_cohort(cls, cohort: WorldCohort) -> WorldCohortReport:
        return cls(
            cohort_id=cohort.cohort_id,
            manifest_sha256=cohort.manifest.manifest_sha256,
            phase=cohort.phase,
        )


def _default_lane_states(manifest: WorldCohortManifest) -> dict[str, LaneOperationalState]:
    return {
        lane.lane_id: LaneOperationalState(lane_id=lane.lane_id, status=LaneOperationalStatus.ACTIVE)
        for lane in manifest.lanes
    }


def _assert_event_identity(event: WorldCohortEvent, manifest: WorldCohortManifest) -> None:
    if event.cohort_id != manifest.cohort_id or event.manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("event cohort identity does not match the manifest")


def _assert_required_sensors_satisfied(
    satisfied_sensor_ids: Sequence[str],
    manifest: WorldCohortManifest,
) -> None:
    required = {
        item.sensor_id for item in manifest.sensor_requirements if item.mode is SensorMask.REQUIRED
    }
    if not required.issubset(set(satisfied_sensor_ids)):
        raise ValueError("required sensors are not satisfied")


def _validate_slot_against_manifest(slot: WorldCohortSlot, manifest: WorldCohortManifest, started: WorldCohortStarted) -> None:
    if slot.cohort_id != manifest.cohort_id or slot.manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("slot cohort identity does not match the manifest")
    if slot.venue not in set(manifest.venues):
        raise ValueError("slot venue is not in the manifest")
    if slot.bar_interval != manifest.bar_interval:
        raise ValueError("slot bar_interval must match the manifest")
    expected = tuple(lane.lane_id for lane in manifest.lanes)
    if set(slot.expected_lane_ids) != set(expected):
        raise ValueError("slot expected lanes must match the manifest")
    declared_contracts = {lane.feature_contract_id for lane in manifest.lanes}
    unknown_refs = [contract_id for contract_id in slot.episode_refs_by_contract if contract_id not in declared_contracts]
    if unknown_refs:
        raise ValueError("episode_refs_by_contract keys must be declared by a lane")
    for lane in manifest.lanes:
        if slot.feature_contract_fingerprints.get(lane.lane_id) != lane.feature_contract_fingerprint:
            raise ValueError("slot feature contract fingerprint does not match the lane")
        if slot.feature_mask_fingerprints.get(lane.lane_id) != lane.feature_mask_fingerprint:
            raise ValueError("slot feature mask fingerprint does not match the lane")
    if slot.started_event_id != started.event_id:
        raise ValueError("slot started_event_id must match WorldCohortStarted")
    if manifest.scope_mapping is None:
        if slot.scope_resolution is not None:
            raise ValueError("scope resolution is not allowed without a manifest scope_mapping")
        return
    if slot.scope_resolution is None:
        raise ValueError("scope resolution is required when the manifest declares scope_mapping")
    if (
        slot.scope_resolution.mapping_id != manifest.scope_mapping.mapping_id
        or slot.scope_resolution.mapping_sha256 != manifest.scope_mapping.mapping_sha256
    ):
        raise ValueError("scope resolution mapping identity must match the manifest")


def _validate_completion_evidence(
    evidence: WorldCohortCompletionEvidence,
    *,
    slots: Sequence[WorldCohortSlot],
    horizons: Sequence[str],
) -> None:
    expected_slots = {slot.slot_id for slot in slots}
    actual_slots = {item.slot_id for item in evidence.slots}
    if actual_slots != expected_slots:
        raise ValueError("completion evidence must reference every admitted slot")
    expected_horizons = set(horizons)
    for item in evidence.slots:
        actual_horizons = {leaf.horizon_id for leaf in item.leaves}
        if actual_horizons != expected_horizons:
            missing = expected_horizons - actual_horizons
            raise ValueError(f"completion evidence is missing horizon {sorted(missing)[0]}")


def _assert_start_evidence(event: WorldCohortStarted, evidence: AvailabilityEvidence) -> None:
    payload_hash = world_cohort_event_payload_hash(event)
    subject = evidence.receipt.subject
    if subject.kind != _EVENT_SUBJECT_KIND or subject.subject_id != event.event_id or subject.content_sha256 != payload_hash:
        raise ValueError("started_evidence does not prove the WorldCohortStarted event")


@dataclass(frozen=True)
class WorldCohort:
    """Event-sourced study aggregate. Phase is derived; callers never assign it."""

    manifest: WorldCohortManifest
    events: Sequence[WorldCohortEvent | Mapping[str, Any]]

    def __post_init__(self) -> None:
        manifest = (
            self.manifest if isinstance(self.manifest, WorldCohortManifest) else WorldCohortManifest.from_mapping(self.manifest)
        )
        events = tuple(parse_world_cohort_event(item) for item in self.events)
        if not events:
            raise ValueError("WorldCohort requires a registered event")
        if not isinstance(events[0], WorldCohortRegistered):
            raise ValueError("first event must be WorldCohortRegistered")
        _assert_event_identity(events[0], manifest)
        phase = CohortPhase.REGISTERED
        started: WorldCohortStarted | None = None
        slots: list[WorldCohortSlot] = []
        lane_states = _default_lane_states(manifest)
        completion: WorldCohortCompletionEvidence | None = None
        for event in events[1:]:
            _assert_event_identity(event, manifest)
            if phase in {CohortPhase.COMPLETE, CohortPhase.INVALIDATED}:
                raise ValueError("no events are allowed after a terminal phase")
            if isinstance(event, WorldCohortRegistered):
                raise ValueError("duplicate registered event")
            if isinstance(event, WorldCohortArmed):
                if phase is not CohortPhase.REGISTERED:
                    raise ValueError("cohort can only arm from registered")
                if event.runtime_identity != manifest.runtime_identity:
                    raise ValueError("armed runtime identity must match the manifest")
                _assert_required_sensors_satisfied(event.satisfied_sensor_ids, manifest)
                phase = CohortPhase.ARMED
                continue
            if isinstance(event, WorldCohortStarted):
                if phase is not CohortPhase.ARMED:
                    raise ValueError("cohort can only start from armed")
                if event.runtime_identity != manifest.runtime_identity:
                    raise ValueError("started runtime identity must match the manifest")
                started = event
                phase = CohortPhase.COLLECTING
                continue
            if isinstance(event, WorldCohortSlotAdmitted):
                if phase is not CohortPhase.COLLECTING:
                    raise ValueError("slots can only be admitted while collecting")
                if started is None:
                    raise ValueError("cannot admit a slot before start")
                _validate_slot_against_manifest(event.slot, manifest, started)
                existing = next((item for item in slots if item.slot_id == event.slot.slot_id), None)
                if existing is not None:
                    raise ValueError("duplicate admitted slot")
                slots.append(event.slot)
                continue
            if isinstance(event, WorldCohortLaneBlocked):
                if phase is not CohortPhase.COLLECTING:
                    raise ValueError("lanes can only be blocked while collecting")
                if event.state.lane_id not in lane_states:
                    raise ValueError("unknown lane")
                lane_states[event.state.lane_id] = event.state
                continue
            if isinstance(event, WorldCohortLaneRestored):
                if phase is not CohortPhase.COLLECTING:
                    raise ValueError("lanes can only be restored while collecting")
                current = lane_states.get(event.state.lane_id)
                if current is None:
                    raise ValueError("unknown lane")
                if current.status is not LaneOperationalStatus.BLOCKED:
                    raise ValueError("lane is not blocked")
                lane_states[event.state.lane_id] = event.state
                continue
            if isinstance(event, WorldCohortCollectionClosed):
                if phase is not CohortPhase.COLLECTING:
                    raise ValueError("collection can only close while collecting")
                phase = CohortPhase.COLLECTION_CLOSED
                continue
            if isinstance(event, WorldCohortCompleted):
                if phase is not CohortPhase.COLLECTION_CLOSED:
                    raise ValueError("complete requires collection_closed")
                _validate_completion_evidence(event.evidence, slots=slots, horizons=manifest.horizons)
                completion = event.evidence
                phase = CohortPhase.COMPLETE
                continue
            if isinstance(event, WorldCohortInvalidated):
                if phase is CohortPhase.COMPLETE:
                    raise ValueError("complete is terminal")
                phase = CohortPhase.INVALIDATED
                continue
            raise TypeError(f"unsupported cohort event: {type(event).__name__}")
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "_phase", phase)
        object.__setattr__(self, "_started", started)
        object.__setattr__(self, "_slots", tuple(slots))
        object.__setattr__(self, "_lane_states", MappingProxyType(lane_states))
        object.__setattr__(self, "_completion", completion)

    @property
    def cohort_id(self) -> str:
        return self.manifest.cohort_id

    @property
    def phase(self) -> CohortPhase:
        return self._phase

    @property
    def started_event(self) -> WorldCohortStarted | None:
        return self._started

    @property
    def admitted_slots(self) -> tuple[WorldCohortSlot, ...]:
        return self._slots

    @property
    def lane_states(self) -> Mapping[str, LaneOperationalState]:
        return self._lane_states

    @property
    def completion_evidence(self) -> WorldCohortCompletionEvidence | None:
        return self._completion

    def _append(self, event: WorldCohortEvent) -> WorldCohort:
        return WorldCohort(manifest=self.manifest, events=(*self.events, event))

    def _require_identity(self, *, cohort_id: str, manifest_sha256: str) -> None:
        if cohort_id != self.cohort_id or manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("command cohort identity does not match the aggregate")

    def _require_runtime(self, runtime: WorldRuntimeIdentity) -> None:
        if runtime != self.manifest.runtime_identity:
            raise ValueError("runtime identity must match the manifest")

    @classmethod
    def reconstruct(
        cls,
        manifest: WorldCohortManifest | Mapping[str, Any],
        events: Sequence[WorldCohortEvent | Mapping[str, Any]],
    ) -> WorldCohort:
        return cls(manifest=manifest, events=events)

    @classmethod
    def register(cls, command: RegisterWorldCohort, existing: WorldCohort | None = None) -> WorldCohort:
        if not isinstance(command, RegisterWorldCohort):
            raise TypeError("register requires RegisterWorldCohort")
        manifest = command.manifest
        if existing is not None:
            if existing.cohort_id != manifest.cohort_id:
                raise ValueError("existing cohort_id mismatch")
            if existing.manifest.manifest_sha256 != manifest.manifest_sha256:
                raise ValueError("cohort_id reused with a different hash")
            return existing
        event = WorldCohortRegistered(cohort_id=manifest.cohort_id, manifest_sha256=manifest.manifest_sha256)
        return cls(manifest=manifest, events=(event,))

    def arm(self, command: ArmWorldCohort) -> WorldCohort:
        if not isinstance(command, ArmWorldCohort):
            raise TypeError("arm requires ArmWorldCohort")
        self._require_identity(cohort_id=command.cohort_id, manifest_sha256=command.manifest_sha256)
        self._require_runtime(command.runtime_identity)
        if self.phase is CohortPhase.ARMED:
            return self
        if self.phase is not CohortPhase.REGISTERED:
            raise ValueError("cohort can only arm from registered")
        _assert_required_sensors_satisfied(command.satisfied_sensor_ids, self.manifest)
        return self._append(
            WorldCohortArmed(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                runtime_identity=command.runtime_identity,
                satisfied_sensor_ids=command.satisfied_sensor_ids,
            )
        )

    def start(self, command: StartWorldCohort) -> WorldCohort:
        if not isinstance(command, StartWorldCohort):
            raise TypeError("start requires StartWorldCohort")
        self._require_identity(cohort_id=command.cohort_id, manifest_sha256=command.manifest_sha256)
        self._require_runtime(command.runtime_identity)
        if self.phase is CohortPhase.COLLECTING:
            return self
        if self.phase is not CohortPhase.ARMED:
            raise ValueError("cohort can only start from armed")
        return self._append(
            WorldCohortStarted(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                runtime_identity=command.runtime_identity,
            )
        )

    def admit_slot(self, command: AdmitWorldCohortSlot) -> WorldCohort:
        if not isinstance(command, AdmitWorldCohortSlot):
            raise TypeError("admit_slot requires AdmitWorldCohortSlot")
        if self.phase is CohortPhase.COLLECTION_CLOSED:
            raise ValueError("cannot admit after collection_closed")
        if self.phase is CohortPhase.INVALIDATED:
            raise ValueError("cannot admit into an invalidated cohort")
        if self.phase is not CohortPhase.COLLECTING or self.started_event is None:
            raise ValueError("slots can only be admitted while collecting")
        slot = command.slot
        evidence = command.started_evidence
        _assert_start_evidence(self.started_event, evidence)
        if slot.anchor_end_at <= evidence.effective_ready_at:
            raise ValueError("anchor_end_at must be strictly after the proven start")
        stop_at = self.manifest.collection_stop_rule.at
        if slot.as_of_bar_ts > stop_at or slot.anchor_end_at > stop_at:
            raise ValueError("cannot admit after collection_stop_rule.at")
        _validate_slot_against_manifest(slot, self.manifest, self.started_event)
        existing = next((item for item in self.admitted_slots if item.slot_id == slot.slot_id), None)
        if existing is not None:
            if existing == slot:
                return self
            raise ValueError("slot_id reused with a conflicting slot")
        return self._append(WorldCohortSlotAdmitted(slot=slot))

    def block_lane(self, command: BlockWorldCohortLane) -> WorldCohort:
        if not isinstance(command, BlockWorldCohortLane):
            raise TypeError("block_lane requires BlockWorldCohortLane")
        if self.phase is not CohortPhase.COLLECTING:
            raise ValueError("lanes can only be blocked while collecting")
        if command.lane_id not in self.lane_states:
            raise ValueError("unknown lane")
        current = self.lane_states[command.lane_id]
        state = LaneOperationalState(
            lane_id=command.lane_id,
            status=LaneOperationalStatus.BLOCKED,
            reason=command.reason,
            affected_from=command.affected_from,
            affected_until=command.affected_until,
        )
        if current.status is LaneOperationalStatus.BLOCKED:
            if current.reason is command.reason:
                return self
            raise ValueError("lane is already blocked with a different reason")
        return self._append(
            WorldCohortLaneBlocked(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                state=state,
            )
        )

    def restore_lane(self, command: RestoreWorldCohortLane) -> WorldCohort:
        if not isinstance(command, RestoreWorldCohortLane):
            raise TypeError("restore_lane requires RestoreWorldCohortLane")
        if self.phase is not CohortPhase.COLLECTING:
            raise ValueError("lanes can only be restored while collecting")
        current = self.lane_states.get(command.lane_id)
        if current is None:
            raise ValueError("unknown lane")
        if current.status is LaneOperationalStatus.ACTIVE:
            return self
        state = LaneOperationalState(lane_id=command.lane_id, status=LaneOperationalStatus.ACTIVE)
        return self._append(
            WorldCohortLaneRestored(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                state=state,
            )
        )

    def close(self, command: CloseWorldCohort) -> WorldCohort:
        if not isinstance(command, CloseWorldCohort):
            raise TypeError("close requires CloseWorldCohort")
        if self.phase in {CohortPhase.COMPLETE, CohortPhase.INVALIDATED}:
            raise ValueError("cohort is already terminal")
        if self.phase is CohortPhase.COLLECTION_CLOSED:
            return self
        if self.phase is not CohortPhase.COLLECTING:
            raise ValueError("collection can only close while collecting")
        return self._append(
            WorldCohortCollectionClosed(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                reason=command.reason,
            )
        )

    def complete(self, command: CompleteWorldCohort) -> WorldCohort:
        if not isinstance(command, CompleteWorldCohort):
            raise TypeError("complete requires CompleteWorldCohort")
        if self.phase is CohortPhase.COMPLETE:
            if self.completion_evidence == command.evidence:
                return self
            raise ValueError("complete already recorded with different evidence")
        if self.phase is not CohortPhase.COLLECTION_CLOSED:
            raise ValueError("complete requires collection_closed")
        _validate_completion_evidence(command.evidence, slots=self.admitted_slots, horizons=self.manifest.horizons)
        return self._append(
            WorldCohortCompleted(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                evidence=command.evidence,
            )
        )

    def invalidate(self, command: InvalidateWorldCohort) -> WorldCohort:
        if not isinstance(command, InvalidateWorldCohort):
            raise TypeError("invalidate requires InvalidateWorldCohort")
        if self.phase is CohortPhase.COMPLETE:
            raise ValueError("complete is terminal")
        if self.phase is CohortPhase.INVALIDATED:
            return self
        return self._append(
            WorldCohortInvalidated(
                cohort_id=self.cohort_id,
                manifest_sha256=self.manifest.manifest_sha256,
                reason=command.reason,
                scope=command.scope,
                proofs=command.proofs,
                occurred_at=command.occurred_at,
            )
        )

    def handle(self, command: WorldCohortCommand) -> WorldCohort:
        if isinstance(command, RegisterWorldCohort):
            return type(self).register(command, existing=self)
        if isinstance(command, ArmWorldCohort):
            return self.arm(command)
        if isinstance(command, StartWorldCohort):
            return self.start(command)
        if isinstance(command, AdmitWorldCohortSlot):
            return self.admit_slot(command)
        if isinstance(command, BlockWorldCohortLane):
            return self.block_lane(command)
        if isinstance(command, RestoreWorldCohortLane):
            return self.restore_lane(command)
        if isinstance(command, CloseWorldCohort):
            return self.close(command)
        if isinstance(command, CompleteWorldCohort):
            return self.complete(command)
        if isinstance(command, InvalidateWorldCohort):
            return self.invalidate(command)
        raise TypeError(f"unsupported command: {type(command).__name__}")


def world_cohort_lane_signature(lanes: Sequence[WorldLaneDefinition | Mapping[str, Any]]) -> tuple[str, ...]:
    """Stable lane-id signature. Same families × logicals = same pilot shape."""

    resolved = _lane_tuple(lanes)
    return tuple(sorted(lane.lane_id for lane in resolved))


_LIVE_COHORT_PHASES = frozenset({CohortPhase.REGISTERED, CohortPhase.ARMED, CohortPhase.COLLECTING})


def is_same_shape_mapping_cohort(
    cohort: WorldCohort,
    *,
    mapping_id: str,
    lane_signature: Sequence[str],
    study_kind: StudyKind | str,
    question: str,
) -> bool:
    """True when the cohort is the same mapping family and pilot shape. Hash may differ."""

    if not isinstance(cohort, WorldCohort):
        raise TypeError("cohort must be WorldCohort")
    pin = cohort.manifest.scope_mapping
    if pin is None:
        return False
    if pin.mapping_id != _required_text(mapping_id, "mapping_id"):
        return False
    expected_kind = _enum(StudyKind, study_kind, "study_kind")
    if cohort.manifest.study_kind is not expected_kind:
        return False
    if cohort.manifest.question != _required_text(question, "question"):
        return False
    expected_lanes = tuple(_required_text(item, "lane_signature[]") for item in lane_signature)
    return world_cohort_lane_signature(cohort.manifest.lanes) == tuple(sorted(expected_lanes))


def is_live_same_shape_mapping_cohort(
    cohort: WorldCohort,
    *,
    mapping_id: str,
    lane_signature: Sequence[str],
    study_kind: StudyKind | str,
    question: str,
) -> bool:
    """True when a non-terminal same-shape cohort must stay pinned to its mapping generation."""

    if not isinstance(cohort, WorldCohort):
        raise TypeError("cohort must be WorldCohort")
    if cohort.phase not in _LIVE_COHORT_PHASES:
        return False
    return is_same_shape_mapping_cohort(
        cohort,
        mapping_id=mapping_id,
        lane_signature=lane_signature,
        study_kind=study_kind,
        question=question,
    )


def is_prior_mapping_generation_cohort(
    cohort: WorldCohort,
    *,
    mapping_id: str,
    mapping_sha256: str,
    lane_signature: Sequence[str],
    study_kind: StudyKind | str,
    question: str,
) -> bool:
    """True when a collecting cohort is the same pilot shape on a previous mapping hash."""

    if not isinstance(cohort, WorldCohort):
        raise TypeError("cohort must be WorldCohort")
    if cohort.phase is not CohortPhase.COLLECTING:
        return False
    pin = cohort.manifest.scope_mapping
    if pin is None:
        return False
    expected_hash = _sha256_hex(mapping_sha256, "mapping_sha256")
    if pin.mapping_sha256 == expected_hash:
        return False
    return is_same_shape_mapping_cohort(
        cohort,
        mapping_id=mapping_id,
        lane_signature=lane_signature,
        study_kind=study_kind,
        question=question,
    )


__all__ = [
    "COHORT_AUTHORITY",
    "COHORT_DECISION_EFFECT",
    "COHORT_RECOMMENDATION",
    "AdmitWorldCohortSlot",
    "ArmWorldCohort",
    "BlockWorldCohortLane",
    "CloseWorldCohort",
    "CohortPhase",
    "CollectionStopKind",
    "CompleteWorldCohort",
    "ContrastRole",
    "InvalidateWorldCohort",
    "InvalidationReason",
    "LaneBlockReason",
    "LaneOperationalState",
    "LaneOperationalStatus",
    "LaneRole",
    "ModelFamily",
    "RegisterWorldCohort",
    "RestoreWorldCohortLane",
    "SensorMask",
    "StartWorldCohort",
    "StudyKind",
    "SupportGateMode",
    "WORLD_COHORT_COMMANDS",
    "WORLD_COHORT_EVENTS",
    "WorldCohort",
    "WorldCohortArmed",
    "WorldCohortCollectionClosed",
    "WorldCohortCommand",
    "WorldCohortCompleted",
    "WorldCohortCompletionEvidence",
    "WorldCohortCompletionHorizonLeaf",
    "WorldCohortCompletionSlotEvidence",
    "WorldCohortEvent",
    "WorldCohortEventEnvelope",
    "WorldCohortId",
    "WorldCohortInvalidated",
    "WorldCohortLaneBlocked",
    "is_live_same_shape_mapping_cohort",
    "is_prior_mapping_generation_cohort",
    "is_same_shape_mapping_cohort",
    "world_cohort_lane_signature",
    "WorldCohortLaneRestored",
    "WorldCohortManifest",
    "WorldCohortMatchedMember",
    "WorldCohortMatchedSet",
    "WorldCohortRegistered",
    "WorldCohortReport",
    "WorldCohortSlot",
    "WorldCohortSlotAdmitted",
    "WorldCohortStarted",
    "WorldCollectionStopRule",
    "WorldContrastDefinition",
    "WorldContrastTerm",
    "WorldFeatureContract",
    "WorldFeatureMask",
    "WorldLaneDefinition",
    "WorldRuntimeIdentity",
    "WorldRuntimeIdentityIntent",
    "WorldScopeMappingRef",
    "WorldScopeResolution",
    "WorldSensorRequirement",
    "WorldStatisticalProtocol",
    "WorldSupportGates",
    "parse_world_cohort_event",
    "world_cohort_event_payload_hash",
]
