"""ID-free driver projection for overlay hops. Stdlib/domain only.

``DriverState`` is frozen onto ``PatternStep`` / ``PatternMatchedHop``. It copies
closed MacroWorldObservation regimes, never observation ids, series codes, or
report text. Producer-reported ``unknown`` stays a regime value; unjoined
payloads are ``missing``; overlays without a closed vocab are ``unspecified``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.world_macro import (
    MACRO_COVERAGE_STATUSES,
    MACRO_FEATURE_KEYS,
    MACRO_FEATURE_VALUES,
    MACRO_PRODUCER_VERSION,
    MACRO_TRANSFORM_VERSION,
    MacroWorldObservation,
    require_admitted_macro_producer,
)


DRIVER_REGIME_BUNDLE_SCHEMA = "driver_regime_bundle.v1"
DRIVER_STATE_SCHEMA = "driver_state.v1"

DRIVER_SOURCE_FAMILIES = frozenset({"macro_observation", "knowledge_artifact", "unproven"})
DRIVER_SIGNAL_CLASSES = frozenset({"regime_bundle", "unspecified", "missing"})
DRIVER_ARTIFACT_KINDS = frozenset({"news_macro", "company_intelligence", "macro_world_observation"})
DRIVER_UNTIL_BOUNDS = frozenset({"bounded", "unbounded", "unknown"})
DRIVER_PRODUCER_VERSIONS = frozenset({MACRO_PRODUCER_VERSION, "unspecified"})
DRIVER_TRANSFORM_VERSIONS = frozenset({MACRO_TRANSFORM_VERSION, "unspecified"})
DRIVER_MISSINGNESS = frozenset(
    {
        "none",
        "observation_unjoined",
        "observation_hash_mismatch",
        "artifact_unjoined",
        "producer_not_admitted",
        "not_applicable",
    }
)
DRIVER_OVERLAY_RELATION_KINDS = frozenset({"OBSERVES", "ABOUT"})
_OBSERVATION_MISSINGNESS = frozenset(
    {"observation_unjoined", "observation_hash_mismatch", "producer_not_admitted"}
)
_FORBIDDEN_DRIVER_KEYS = frozenset(
    {
        "observation_id",
        "content_sha256",
        "fact_version_id",
        "fact_refs",
        "provider_entity_id",
        "source_record_id",
        "series_code",
        "brief_id",
        "symbol",
        "lei",
        "entity_id",
        "metric_key",
        "effective_from",
        "effective_until",
        "valid_until",
        "selection_view",
        "driver_key",
        "driverKey",
        "text",
        "report_text",
        "raw_text",
    }
)
_BUNDLE_KEYS = frozenset(
    {"schema_version", "macro_regime", "rates_regime", "usd_regime", "coverage_status"}
)
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "source_family",
        "signal_class",
        "regimes",
        "artifact_kind",
        "until_bound",
        "producer_version",
        "transform_version",
        "missingness",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _closed_member(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field_name)
    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    return text


def _optional_closed(value: Any, field_name: str, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    return _closed_member(value, field_name, allowed)


def _reject_raw_driver_keys(value: Mapping[str, Any], field_name: str, allowed: frozenset[str]) -> None:
    leaked = sorted(str(key) for key in value if key in _FORBIDDEN_DRIVER_KEYS)
    if leaked:
        raise ValueError(f"{field_name} forbids raw identity fields: {', '.join(leaked)}")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"{field_name} forbids unknown fields: {', '.join(unknown)}")


def _regime_value(dimension: str, value: Any) -> str:
    text = _required_text(value, dimension)
    allowed = MACRO_FEATURE_VALUES[dimension]
    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{dimension} must be one of: {allowed_text}")
    return text


def driver_state_required_for_relation(relation_kind: Any) -> bool:
    if not isinstance(relation_kind, str):
        return False
    kind = relation_kind.strip().upper().replace("-", "_")
    return kind in DRIVER_OVERLAY_RELATION_KINDS


@dataclass(frozen=True)
class DriverRegimeBundle:
    """Closed three-regime payload. ``unknown`` is a producer-reported value."""

    macro_regime: str
    rates_regime: str
    usd_regime: str
    coverage_status: str
    schema_version: str = DRIVER_REGIME_BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != DRIVER_REGIME_BUNDLE_SCHEMA:
            raise ValueError(f"schema_version must be {DRIVER_REGIME_BUNDLE_SCHEMA}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "macro_regime", _regime_value("macro_regime", self.macro_regime))
        object.__setattr__(self, "rates_regime", _regime_value("rates_regime", self.rates_regime))
        object.__setattr__(self, "usd_regime", _regime_value("usd_regime", self.usd_regime))
        object.__setattr__(
            self,
            "coverage_status",
            _closed_member(self.coverage_status, "coverage_status", MACRO_COVERAGE_STATUSES),
        )

    def identity_tuple(self) -> tuple[str, str, str, str]:
        return (self.macro_regime, self.rates_regime, self.usd_regime, self.coverage_status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "macro_regime": self.macro_regime,
            "rates_regime": self.rates_regime,
            "usd_regime": self.usd_regime,
            "coverage_status": self.coverage_status,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | DriverRegimeBundle) -> DriverRegimeBundle:
        if isinstance(value, DriverRegimeBundle):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("driver regime bundle must be DriverRegimeBundle or a mapping")
        _reject_raw_driver_keys(value, "driver regime bundle", _BUNDLE_KEYS)
        return cls(
            schema_version=value.get("schema_version", DRIVER_REGIME_BUNDLE_SCHEMA),
            macro_regime=value.get("macro_regime"),
            rates_regime=value.get("rates_regime"),
            usd_regime=value.get("usd_regime"),
            coverage_status=value.get("coverage_status"),
        )


def _as_regime_bundle(value: Any) -> DriverRegimeBundle | None:
    if value is None:
        return None
    if isinstance(value, DriverRegimeBundle):
        return value
    if isinstance(value, Mapping):
        return DriverRegimeBundle.from_mapping(value)
    raise TypeError("regimes must be DriverRegimeBundle or a mapping")


@dataclass(frozen=True)
class DriverState:
    """Minimal overlay identity. Evidence ids stay off this object."""

    source_family: str
    signal_class: str
    regimes: DriverRegimeBundle | Mapping[str, Any] | None
    artifact_kind: str | None
    until_bound: str
    producer_version: str
    transform_version: str
    missingness: str
    schema_version: str = DRIVER_STATE_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != DRIVER_STATE_SCHEMA:
            raise ValueError(f"schema_version must be {DRIVER_STATE_SCHEMA}")
        source_family = _closed_member(self.source_family, "source_family", DRIVER_SOURCE_FAMILIES)
        signal_class = _closed_member(self.signal_class, "signal_class", DRIVER_SIGNAL_CLASSES)
        regimes = _as_regime_bundle(self.regimes)
        artifact_kind = _optional_closed(self.artifact_kind, "artifact_kind", DRIVER_ARTIFACT_KINDS)
        until_bound = _closed_member(self.until_bound, "until_bound", DRIVER_UNTIL_BOUNDS)
        producer_version = _closed_member(self.producer_version, "producer_version", DRIVER_PRODUCER_VERSIONS)
        transform_version = _closed_member(self.transform_version, "transform_version", DRIVER_TRANSFORM_VERSIONS)
        missingness = _closed_member(self.missingness, "missingness", DRIVER_MISSINGNESS)
        if signal_class == "regime_bundle":
            if regimes is None:
                raise ValueError("signal_class=regime_bundle requires a DriverRegimeBundle")
            if missingness != "none":
                raise ValueError("signal_class=regime_bundle requires missingness=none")
            if source_family != "macro_observation":
                raise ValueError("regime_bundle requires source_family=macro_observation")
            if artifact_kind != "macro_world_observation":
                raise ValueError("regime_bundle requires artifact_kind=macro_world_observation")
            if producer_version == "unspecified" or transform_version == "unspecified":
                raise ValueError("regime_bundle requires admitted producer and transform versions")
        elif signal_class == "missing":
            if regimes is not None:
                raise ValueError("signal_class=missing cannot carry a regime bundle")
            if missingness in {"none", "not_applicable"}:
                raise ValueError("signal_class=missing requires an explicit missingness reason")
            if missingness in _OBSERVATION_MISSINGNESS:
                if source_family != "macro_observation":
                    raise ValueError("observation missingness requires source_family=macro_observation")
                if artifact_kind not in {None, "macro_world_observation"}:
                    raise ValueError("observation missingness cannot carry a knowledge artifact_kind")
            if missingness == "artifact_unjoined":
                if source_family not in {"knowledge_artifact", "unproven"}:
                    raise ValueError("artifact_unjoined requires a knowledge or unproven source_family")
                if artifact_kind == "macro_world_observation":
                    raise ValueError("artifact_unjoined cannot use macro_world_observation")
            if missingness == "producer_not_admitted" and producer_version != "unspecified":
                raise ValueError("producer_not_admitted must not carry a raw producer_version")
        else:
            if regimes is not None:
                raise ValueError("signal_class=unspecified cannot carry a regime bundle")
            if missingness != "not_applicable":
                raise ValueError("signal_class=unspecified requires missingness=not_applicable")
            if source_family not in {"knowledge_artifact", "unproven"}:
                raise ValueError("unspecified requires a knowledge or unproven source_family")
            if artifact_kind == "macro_world_observation":
                raise ValueError("unspecified cannot use macro_world_observation")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_family", source_family)
        object.__setattr__(self, "signal_class", signal_class)
        object.__setattr__(self, "regimes", regimes)
        object.__setattr__(self, "artifact_kind", artifact_kind)
        object.__setattr__(self, "until_bound", until_bound)
        object.__setattr__(self, "producer_version", producer_version)
        object.__setattr__(self, "transform_version", transform_version)
        object.__setattr__(self, "missingness", missingness)

    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.source_family,
            self.signal_class,
            None if self.regimes is None else self.regimes.identity_tuple(),
            self.artifact_kind,
            self.until_bound,
            self.producer_version,
            self.transform_version,
            self.missingness,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_family": self.source_family,
            "signal_class": self.signal_class,
            "regimes": None if self.regimes is None else self.regimes.to_dict(),
            "artifact_kind": self.artifact_kind,
            "until_bound": self.until_bound,
            "producer_version": self.producer_version,
            "transform_version": self.transform_version,
            "missingness": self.missingness,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | DriverState) -> DriverState:
        if isinstance(value, DriverState):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("driver state must be DriverState or a mapping")
        _reject_raw_driver_keys(value, "driver state", _STATE_KEYS)
        return cls(
            schema_version=value.get("schema_version", DRIVER_STATE_SCHEMA),
            source_family=value.get("source_family"),
            signal_class=value.get("signal_class"),
            regimes=value.get("regimes"),
            artifact_kind=value.get("artifact_kind"),
            until_bound=value.get("until_bound"),
            producer_version=value.get("producer_version"),
            transform_version=value.get("transform_version"),
            missingness=value.get("missingness"),
        )

    @classmethod
    def from_observation(cls, observation: MacroWorldObservation) -> DriverState:
        if not isinstance(observation, MacroWorldObservation):
            raise TypeError("DriverState.from_observation requires a proven MacroWorldObservation")
        producer_version = require_admitted_macro_producer(observation.producer_version)
        transform_version = _closed_member(
            observation.transform_version, "transform_version", DRIVER_TRANSFORM_VERSIONS
        )
        if transform_version == "unspecified":
            raise ValueError(f"transform_version must be {MACRO_TRANSFORM_VERSION}")
        features = observation.features
        return cls(
            source_family="macro_observation",
            signal_class="regime_bundle",
            regimes=DriverRegimeBundle(
                macro_regime=features[MACRO_FEATURE_KEYS[0]],
                rates_regime=features[MACRO_FEATURE_KEYS[1]],
                usd_regime=features[MACRO_FEATURE_KEYS[2]],
                coverage_status=observation.coverage.status,
            ),
            artifact_kind="macro_world_observation",
            until_bound="bounded" if observation.valid_until is not None else "unbounded",
            producer_version=producer_version,
            transform_version=transform_version,
            missingness="none",
        )

    @classmethod
    def missing(
        cls,
        *,
        missingness: str,
        source_family: str = "macro_observation",
        artifact_kind: str | None = None,
        until_bound: str = "unknown",
        producer_version: str = "unspecified",
        transform_version: str = "unspecified",
    ) -> DriverState:
        return cls(
            source_family=source_family,
            signal_class="missing",
            regimes=None,
            artifact_kind=artifact_kind,
            until_bound=until_bound,
            producer_version=producer_version,
            transform_version=transform_version,
            missingness=missingness,
        )

    @classmethod
    def unspecified(
        cls,
        *,
        source_family: str = "knowledge_artifact",
        artifact_kind: str | None = None,
        until_bound: str = "unknown",
        producer_version: str = "unspecified",
        transform_version: str = "unspecified",
    ) -> DriverState:
        return cls(
            source_family=source_family,
            signal_class="unspecified",
            regimes=None,
            artifact_kind=artifact_kind,
            until_bound=until_bound,
            producer_version=producer_version,
            transform_version=transform_version,
            missingness="not_applicable",
        )


__all__ = [
    "DRIVER_ARTIFACT_KINDS",
    "DRIVER_MISSINGNESS",
    "DRIVER_OVERLAY_RELATION_KINDS",
    "DRIVER_PRODUCER_VERSIONS",
    "DRIVER_REGIME_BUNDLE_SCHEMA",
    "DRIVER_SIGNAL_CLASSES",
    "DRIVER_SOURCE_FAMILIES",
    "DRIVER_STATE_SCHEMA",
    "DRIVER_TRANSFORM_VERSIONS",
    "DRIVER_UNTIL_BOUNDS",
    "DriverRegimeBundle",
    "DriverState",
    "driver_state_required_for_relation",
]
