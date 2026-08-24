"""Immutable source-only macro contracts, hashes, deny-list, and collection lifecycle.

Stdlib-only. Facts and observations are never mutated; eligibility is a derived view.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
)
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldScopeMapping,
    WorldScopeResolution,
)


MACRO_SOURCE_FACT_SCHEMA = "macro_source_fact.v1"
MACRO_WORLD_OBSERVATION_SCHEMA = "macro_world_observation.v1"
MACRO_WORLD_OBSERVATION_SUBJECT_KIND = "macro_world_observation"
MACRO_SOURCE_REGISTRY_SCHEMA = "macro_source_registry.v1"
MACRO_COLLECTION_PLAN_SCHEMA = "macro_collection_plan.v1"
MACRO_COLLECTION_EVENT_SCHEMA = "macro_collection_event.v1"
MACRO_PRODUCER_VERSION_V1 = "macro_source_only.v1"
MACRO_PRODUCER_VERSION = "macro_source_only.v2"
MACRO_LANE_IDENTITY_V1 = "context.v2.macro_source.v1"
MACRO_LANE_IDENTITY = "context.v2.macro_source.v2"
MACRO_ADMITTED_PRODUCER_VERSIONS = frozenset({MACRO_PRODUCER_VERSION})
MACRO_TRANSFORM_VERSION = "macro_regimes.v1"
MACRO_SOURCE_REGISTRY_VERSION = "macro_sources.v1"
WORLD_MACRO_COLLECTION_PLAN_SHA256 = "74c6d12e6f41a920b6d00224de75cc1eeda47b6634720344fd48851dac7c04e5"
WORLD_MACRO_COLLECTION_PLAN_ID = "macro_collection_plan:v1:" + WORLD_MACRO_COLLECTION_PLAN_SHA256

MACRO_FACT_KINDS = frozenset({"series_point", "market_benchmark"})
MACRO_SCOPE_KINDS = frozenset({"world", "region", "country", "venue"})
_FORBIDDEN_MACRO_SCOPE_KINDS = frozenset({"company", "family", "instrument"})
MACRO_FEATURE_KEYS = ("macro_regime", "rates_regime", "usd_regime")
MACRO_REGIME_VALUES = frozenset({"unknown", "mixed", "quiet", "tightening", "easing"})
RATES_REGIME_VALUES = frozenset({"unknown", "stable", "rising", "falling"})
USD_REGIME_VALUES = frozenset({"unknown", "strong", "weak", "stable"})
MACRO_FEATURE_VALUES = MappingProxyType(
    {
        "macro_regime": MACRO_REGIME_VALUES,
        "rates_regime": RATES_REGIME_VALUES,
        "usd_regime": USD_REGIME_VALUES,
    }
)
MACRO_COVERAGE_STATUSES = frozenset({"complete", "partial", "unknown", "missing"})
MACRO_COLLECTION_STATUSES = frozenset({"registered", "collecting", "completed", "completed_partial", "failed"})
MACRO_TERMINAL_STATUSES = frozenset({"completed", "completed_partial", "failed"})
MACRO_PROVIDER_RESOLUTION_STATUSES = frozenset({"resolved", "unmapped"})

MACRO_POLICY_DENYLIST = frozenset(
    {
        "candidate",
        "candidate_scope",
        "hotlist",
        "mandate",
        "rank",
        "attractiveness",
        "brain_decision",
        "action",
        "intent",
        "order",
        "trade",
        "position",
        "portfolio",
        "risk_gate",
        "scheduler",
        "fill",
        "pnl",
        "reward",
        "feedback",
        "outcome",
        "company_brief",
        "company_report",
        "selected_symbol",
        "model_rationale",
        "prompt",
        "tool_trace",
        "memory",
        "memrl",
    }
)
_DENYLIST_SINGLE_TOKENS = frozenset(item for item in MACRO_POLICY_DENYLIST if "_" not in item)

_FACT_KEY_PREFIX = "macro_fact_key:v1"
_FACT_VERSION_PREFIX = "macro_source_fact_version:v1"
_OBSERVATION_ID_PREFIX = "macro_world_observation:v1"
_RUN_ID_PREFIX = "macro_collection_run:v1"
_EVENT_ID_PREFIX = "macro_collection_event:v1"
_PLAN_ID_PREFIX = "macro_collection_plan:v1"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _required_mapping_text(value: Mapping[str, Any], key: str) -> str:
    if key not in value or value[key] is None:
        raise ValueError(f"{key} is required")
    return _required_text(value[key], key)


def _optional_utc(value: datetime | str | None, field_name: str) -> datetime | None:
    return None if value is None else parse_utc_timestamp(value, field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _normalise_key(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", separated.strip().lower()).strip("_")


def _denied_key(key: str) -> bool:
    normalized = _normalise_key(key)
    if normalized in MACRO_POLICY_DENYLIST:
        return True
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return any(token in _DENYLIST_SINGLE_TOKENS for token in tokens)


def assert_source_only_payload(value: Any, field_name: str = "payload") -> None:
    """Reject policy/control keys at every nested level. Forbidden keys are never stripped."""

    def visit(current: Any, path: str) -> None:
        if isinstance(current, Mapping):
            for raw_key, nested in current.items():
                if not isinstance(raw_key, str):
                    raise TypeError(f"{path} keys must be strings")
                if _denied_key(raw_key):
                    raise ValueError(f"{path}.{raw_key} is forbidden in a source-only macro payload")
                visit(nested, f"{path}.{raw_key}")
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            for index, nested in enumerate(current):
                visit(nested, f"{path}[{index}]")

    visit(value, field_name)


def macro_observes_producer_ref(producer_version: str) -> str:
    """Canonical OBSERVES provenance token bound to one admitted producer contract."""

    return f"producer:{_required_text(producer_version, 'producer_version')}"


def is_admitted_macro_producer(producer_version: str | None) -> bool:
    if producer_version is None:
        return False
    return _required_text(producer_version, "producer_version") in MACRO_ADMITTED_PRODUCER_VERSIONS


_ORIGIN_SCOPE_REF_PREFIX = "origin_scope:"
_ANCESTRY_DISTANCE_REF_PREFIX = "ancestry_distance:"
_PRODUCER_REF_PREFIX = "producer:"


def macro_origin_scope_ref(scope: MacroScope | Mapping[str, Any]) -> str:
    """Canonical provenance token for the observation's actual origin scope."""

    resolved = scope if isinstance(scope, MacroScope) else MacroScope.from_mapping(scope)
    return f"{_ORIGIN_SCOPE_REF_PREFIX}{resolved.kind}:{resolved.entity_id}"


def parse_macro_origin_scope_ref(value: str) -> MacroScope:
    text = _required_text(value, "origin_scope_ref")
    if not text.startswith(_ORIGIN_SCOPE_REF_PREFIX):
        raise ValueError("origin_scope ref is malformed")
    rest = text[len(_ORIGIN_SCOPE_REF_PREFIX) :]
    kind, separator, entity_id = rest.partition(":")
    if not separator:
        raise ValueError("origin_scope ref is malformed")
    return MacroScope(kind=kind, entity_id=entity_id)


def macro_ancestry_distance_ref(distance: int) -> str:
    return f"{_ANCESTRY_DISTANCE_REF_PREFIX}{_non_negative_int(distance, 'distance')}"


def parse_macro_ancestry_distance_ref(value: str) -> int:
    text = _required_text(value, "ancestry_distance_ref")
    if not text.startswith(_ANCESTRY_DISTANCE_REF_PREFIX):
        raise ValueError("ancestry_distance ref is malformed")
    raw = text[len(_ANCESTRY_DISTANCE_REF_PREFIX) :]
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ancestry_distance ref is malformed") from exc
    return _non_negative_int(parsed, "ancestry_distance")


def _split_identity_hash(value: str) -> tuple[str, str] | None:
    if "/" not in value:
        return None
    left, _separator, digest = value.rpartition("/")
    if not left or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return left, digest


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    return tuple(_required_text(item, f"{field_name}[]") for item in value)


def _unique_sorted_texts(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    items = _immutable_text_tuple(value, field_name)
    return tuple(sorted(frozenset(items)))


def _prefixed_id(prefix: str, payload: Any) -> str:
    return f"{prefix}:{canonical_sha256(payload)}"


def _validate_prefixed_id(value: Any, prefix: str, field_name: str) -> str:
    text = _required_text(value, field_name)
    expected = f"{prefix}:"
    if not text.startswith(expected):
        raise ValueError(f"{field_name} must start with '{expected}'")
    digest = text[len(expected) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field_name} digest must be a sha256 hex digest")
    return text


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class MacroFactKey:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _FACT_KEY_PREFIX, "fact_key"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MacroSourceFactVersionId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_prefixed_id(self.value, _FACT_VERSION_PREFIX, "fact_version_id"),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MacroObservationId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_prefixed_id(self.value, _OBSERVATION_ID_PREFIX, "observation_id"),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MacroCollectionRunId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _RUN_ID_PREFIX, "run_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MacroCollectionEventId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_prefixed_id(self.value, _EVENT_ID_PREFIX, "event_id"))

    def __str__(self) -> str:
        return self.value


def _as_fact_key(value: MacroFactKey | str) -> MacroFactKey:
    return value if isinstance(value, MacroFactKey) else MacroFactKey(value)


def _as_fact_version_id(value: MacroSourceFactVersionId | str) -> MacroSourceFactVersionId:
    return value if isinstance(value, MacroSourceFactVersionId) else MacroSourceFactVersionId(value)


@dataclass(frozen=True)
class MacroScope:
    """Canonical world/region/country/venue ref. Never company, family, or instrument."""

    kind: str
    entity_id: str

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind in _FORBIDDEN_MACRO_SCOPE_KINDS:
            raise ValueError(f"MacroScope must not target {kind}")
        canonical = WorldCanonicalScopeRef(kind=kind, entity_id=self.entity_id)
        object.__setattr__(self, "kind", canonical.kind)
        object.__setattr__(self, "entity_id", canonical.entity_id)

    def canonical_ref(self) -> WorldCanonicalScopeRef:
        return WorldCanonicalScopeRef(kind=self.kind, entity_id=self.entity_id)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "entity_id": self.entity_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroScope) -> MacroScope:
        if isinstance(value, MacroScope):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope must be MacroScope or a mapping")
        assert_source_only_payload(value, "scope")
        return cls(kind=value.get("kind"), entity_id=value.get("entity_id"))


@dataclass(frozen=True)
class MacroNumericValue:
    number: float
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "number", _finite_number(self.number, "number"))
        object.__setattr__(self, "unit", _required_text(self.unit, "unit"))

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "unit": self.unit}


@dataclass(frozen=True)
class MacroCategoryValue:
    category: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", _required_text(self.category, "category"))

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category}


def parse_macro_value(
    value: MacroNumericValue | MacroCategoryValue | Mapping[str, Any],
) -> MacroNumericValue | MacroCategoryValue:
    if isinstance(value, (MacroNumericValue, MacroCategoryValue)):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("macro value must be a typed number or category")
    assert_source_only_payload(value, "value")
    keys = set(value)
    if "number" in value and "category" in value:
        raise ValueError("macro value cannot be both number and category")
    if "number" in value:
        extra = keys - {"number", "unit"}
        if extra:
            raise ValueError("numeric macro value only accepts number and unit")
        if "unit" not in value or value.get("unit") in (None, ""):
            raise ValueError("numeric macro value requires a unit")
        return MacroNumericValue(number=value.get("number"), unit=value.get("unit"))
    if "category" in value:
        extra = keys - {"category"}
        if extra:
            raise ValueError("category macro value only accepts category")
        return MacroCategoryValue(category=value.get("category"))
    raise ValueError("macro value must contain number or category")


@dataclass(frozen=True)
class MacroFactSource:
    provider_id: str
    adapter_version: str
    source_record_id: str
    source_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _required_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "adapter_version", _required_text(self.adapter_version, "adapter_version"))
        object.__setattr__(self, "source_record_id", _required_text(self.source_record_id, "source_record_id"))
        object.__setattr__(self, "source_ref", _required_text(self.source_ref, "source_ref"))

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "source_record_id": self.source_record_id,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroFactSource) -> MacroFactSource:
        if isinstance(value, MacroFactSource):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("source must be MacroFactSource or a mapping")
        assert_source_only_payload(value, "source")
        return cls(
            provider_id=value.get("provider_id"),
            adapter_version=value.get("adapter_version"),
            source_record_id=value.get("source_record_id"),
            source_ref=value.get("source_ref"),
        )


def _fact_key_payload(
    *,
    source: MacroFactSource,
    metric_key: str,
    scope: MacroScope,
    fact_kind: str,
    period: str,
) -> dict[str, Any]:
    return {
        "provider_id": source.provider_id,
        "source_record_id": source.source_record_id,
        "metric_key": metric_key,
        "scope": scope.to_dict(),
        "fact_kind": fact_kind,
        "period": period,
    }


def _fact_version_payload(
    *,
    fact_key: str,
    value: MacroNumericValue | MacroCategoryValue,
    published_at: datetime,
    schema_version: str,
    adapter_version: str,
) -> dict[str, Any]:
    return {
        "fact_key": fact_key,
        "value": value.to_dict(),
        "published_at": _iso(published_at),
        "schema_version": schema_version,
        "adapter_version": adapter_version,
    }


def _fact_content_payload(
    *,
    schema_version: str,
    fact_key: str,
    fact_kind: str,
    metric_key: str,
    scope: MacroScope,
    value: MacroNumericValue | MacroCategoryValue,
    period: str,
    occurred_at: datetime,
    published_at: datetime,
    valid_until: datetime | None,
    source: MacroFactSource,
    supersedes_fact_version_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "fact_key": fact_key,
        "fact_kind": fact_kind,
        "metric_key": metric_key,
        "scope": scope.to_dict(),
        "value": value.to_dict(),
        "period": period,
        "occurred_at": _iso(occurred_at),
        "published_at": _iso(published_at),
        "valid_until": _iso(valid_until),
        "source": source.to_dict(),
        "supersedes_fact_version_id": supersedes_fact_version_id,
    }


@dataclass(frozen=True)
class MacroSourceFact:
    fact_kind: str
    metric_key: str
    scope: MacroScope | Mapping[str, Any]
    value: MacroNumericValue | MacroCategoryValue | Mapping[str, Any]
    period: str
    occurred_at: datetime | str
    published_at: datetime | str
    ingested_at: datetime | str
    source: MacroFactSource | Mapping[str, Any]
    valid_until: datetime | str | None = None
    supersedes_fact_version_id: str | None = None
    schema_version: str = MACRO_SOURCE_FACT_SCHEMA
    fact_key: MacroFactKey | str | None = None
    fact_version_id: MacroSourceFactVersionId | str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_SOURCE_FACT_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_SOURCE_FACT_SCHEMA}")
        fact_kind = _required_text(self.fact_kind, "fact_kind")
        if fact_kind not in MACRO_FACT_KINDS:
            allowed = ", ".join(sorted(MACRO_FACT_KINDS))
            raise ValueError(f"fact_kind must be one of: {allowed}")
        metric_key = _required_text(self.metric_key, "metric_key")
        period = _required_text(self.period, "period")
        scope = MacroScope.from_mapping(self.scope)
        value = parse_macro_value(self.value)
        source = MacroFactSource.from_mapping(self.source)
        occurred_at = parse_utc_timestamp(self.occurred_at, "occurred_at")
        published_at = parse_utc_timestamp(self.published_at, "published_at")
        ingested_at = parse_utc_timestamp(self.ingested_at, "ingested_at")
        valid_until = _optional_utc(self.valid_until, "valid_until")
        supersedes = (
            None
            if self.supersedes_fact_version_id is None
            else _validate_prefixed_id(
                self.supersedes_fact_version_id, _FACT_VERSION_PREFIX, "supersedes_fact_version_id"
            )
        )
        fact_key_value = _prefixed_id(
            _FACT_KEY_PREFIX,
            _fact_key_payload(source=source, metric_key=metric_key, scope=scope, fact_kind=fact_kind, period=period),
        )
        if self.fact_key is not None:
            provided_key = _as_fact_key(self.fact_key).value
            if provided_key != fact_key_value:
                raise ValueError("fact_key does not match the canonical fact identity")
        version_value = _prefixed_id(
            _FACT_VERSION_PREFIX,
            _fact_version_payload(
                fact_key=fact_key_value,
                value=value,
                published_at=published_at,
                schema_version=schema_version,
                adapter_version=source.adapter_version,
            ),
        )
        if self.fact_version_id is not None:
            provided_version = _as_fact_version_id(self.fact_version_id).value
            if provided_version != version_value:
                raise ValueError("fact_version_id does not match the canonical fact version")
        if supersedes == version_value:
            raise ValueError("supersedes_fact_version_id must point at the previous leaf")
        content_payload = _fact_content_payload(
            schema_version=schema_version,
            fact_key=fact_key_value,
            fact_kind=fact_kind,
            metric_key=metric_key,
            scope=scope,
            value=value,
            period=period,
            occurred_at=occurred_at,
            published_at=published_at,
            valid_until=valid_until,
            source=source,
            supersedes_fact_version_id=supersedes,
        )
        digest = canonical_sha256(content_payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical MacroSourceFact")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "fact_kind", fact_kind)
        object.__setattr__(self, "metric_key", metric_key)
        object.__setattr__(self, "period", period)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "ingested_at", ingested_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "supersedes_fact_version_id", supersedes)
        object.__setattr__(self, "fact_key", MacroFactKey(fact_key_value))
        object.__setattr__(self, "fact_version_id", MacroSourceFactVersionId(version_value))
        object.__setattr__(self, "content_sha256", digest)
        assert_source_only_payload(self.to_dict(), "macro_source_fact")

    def corrected(
        self,
        *,
        value: MacroNumericValue | MacroCategoryValue | Mapping[str, Any],
        published_at: datetime | str,
        ingested_at: datetime | str,
        occurred_at: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        source: MacroFactSource | Mapping[str, Any] | None = None,
    ) -> MacroSourceFact:
        correction = MacroSourceFact(
            fact_kind=self.fact_kind,
            metric_key=self.metric_key,
            scope=self.scope,
            value=value,
            period=self.period,
            occurred_at=self.occurred_at if occurred_at is None else occurred_at,
            published_at=published_at,
            ingested_at=ingested_at,
            source=self.source if source is None else source,
            valid_until=self.valid_until if valid_until is None else valid_until,
            supersedes_fact_version_id=self.fact_version_id.value,
        )
        if correction.fact_key != self.fact_key:
            raise ValueError("correction must preserve MacroFactKey")
        return correction

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fact_key": self.fact_key.value,
            "fact_version_id": self.fact_version_id.value,
            "fact_kind": self.fact_kind,
            "metric_key": self.metric_key,
            "scope": self.scope.to_dict(),
            "value": self.value.to_dict(),
            "period": self.period,
            "occurred_at": _iso(self.occurred_at),
            "published_at": _iso(self.published_at),
            "ingested_at": _iso(self.ingested_at),
            "valid_until": _iso(self.valid_until),
            "source": self.source.to_dict(),
            "supersedes_fact_version_id": self.supersedes_fact_version_id,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceFact) -> MacroSourceFact:
        if isinstance(value, MacroSourceFact):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("macro source fact must be MacroSourceFact or a mapping")
        assert_source_only_payload(value, "macro_source_fact")
        return cls(
            schema_version=_required_mapping_text(value, "schema_version"),
            fact_kind=value.get("fact_kind"),
            metric_key=value.get("metric_key"),
            scope=value.get("scope"),
            value=value.get("value"),
            period=value.get("period"),
            occurred_at=value.get("occurred_at"),
            published_at=value.get("published_at"),
            ingested_at=value.get("ingested_at"),
            source=value.get("source"),
            valid_until=value.get("valid_until"),
            supersedes_fact_version_id=value.get("supersedes_fact_version_id"),
            fact_key=value.get("fact_key"),
            fact_version_id=value.get("fact_version_id"),
            content_sha256=value.get("content_sha256"),
        )


def reconcile_macro_source_fact(existing: MacroSourceFact, incoming: MacroSourceFact) -> MacroSourceFact:
    if existing.fact_version_id != incoming.fact_version_id:
        raise ValueError("fact_version_id mismatch")
    if existing.content_sha256 != incoming.content_sha256:
        raise ValueError("conflict: same fact version id with different content")
    return existing


@dataclass(frozen=True)
class MacroSourceRegistryEntry:
    source_id: str
    provider_id: str
    provider_entity_id: str
    canonical_scope: MacroScope | Mapping[str, Any]
    adapter_version: str
    fact_kind: str
    metric_key: str

    def __post_init__(self) -> None:
        fact_kind = _required_text(self.fact_kind, "fact_kind")
        if fact_kind not in MACRO_FACT_KINDS:
            allowed = ", ".join(sorted(MACRO_FACT_KINDS))
            raise ValueError(f"fact_kind must be one of: {allowed}")
        object.__setattr__(self, "source_id", _required_text(self.source_id, "source_id"))
        object.__setattr__(self, "provider_id", _required_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "provider_entity_id", _required_text(self.provider_entity_id, "provider_entity_id"))
        object.__setattr__(self, "canonical_scope", MacroScope.from_mapping(self.canonical_scope))
        object.__setattr__(self, "adapter_version", _required_text(self.adapter_version, "adapter_version"))
        object.__setattr__(self, "fact_kind", fact_kind)
        object.__setattr__(self, "metric_key", _required_text(self.metric_key, "metric_key"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "provider_entity_id": self.provider_entity_id,
            "canonical_scope": self.canonical_scope.to_dict(),
            "adapter_version": self.adapter_version,
            "fact_kind": self.fact_kind,
            "metric_key": self.metric_key,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceRegistryEntry) -> MacroSourceRegistryEntry:
        if isinstance(value, MacroSourceRegistryEntry):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("registry entry must be MacroSourceRegistryEntry or a mapping")
        assert_source_only_payload(value, "registry_entry")
        return cls(
            source_id=value.get("source_id"),
            provider_id=value.get("provider_id"),
            provider_entity_id=value.get("provider_entity_id"),
            canonical_scope=value.get("canonical_scope"),
            adapter_version=value.get("adapter_version"),
            fact_kind=value.get("fact_kind"),
            metric_key=value.get("metric_key"),
        )


@dataclass(frozen=True)
class MacroProviderResolution:
    status: str
    scope: MacroScope | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_PROVIDER_RESOLUTION_STATUSES:
            raise ValueError("provider resolution status must be resolved or unmapped")
        if status == "resolved":
            if self.scope is None:
                raise ValueError("resolved provider mapping requires a canonical scope")
            object.__setattr__(self, "scope", MacroScope.from_mapping(self.scope))
            return
        if self.scope is not None:
            raise ValueError("unmapped provider mapping must not select a scope")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class MacroSourceRegistry:
    """Versioned provider-id → canonical MacroScope table. No implicit geographic crosswalk."""

    registry_version: str
    entries: Sequence[MacroSourceRegistryEntry | Mapping[str, Any]] = ()
    schema_version: str = MACRO_SOURCE_REGISTRY_SCHEMA
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        registry_version = _required_text(self.registry_version, "registry_version")
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_SOURCE_REGISTRY_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_SOURCE_REGISTRY_SCHEMA}")
        entries = tuple(
            item if isinstance(item, MacroSourceRegistryEntry) else MacroSourceRegistryEntry.from_mapping(item)
            for item in self.entries
        )
        entries = tuple(sorted(entries, key=lambda item: (item.provider_id, item.provider_entity_id, item.source_id)))
        seen_provider: set[tuple[str, str]] = set()
        seen_source: set[str] = set()
        for entry in entries:
            provider_key = (entry.provider_id, entry.provider_entity_id)
            if provider_key in seen_provider:
                raise ValueError("conflicting MacroSourceRegistry entries for the same provider entity")
            if entry.source_id in seen_source:
                raise ValueError("conflicting MacroSourceRegistry entries for the same source_id")
            seen_provider.add(provider_key)
            seen_source.add(entry.source_id)
        payload = {
            "schema_version": schema_version,
            "registry_version": registry_version,
            "entries": [entry.to_dict() for entry in entries],
        }
        digest = canonical_sha256(payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical MacroSourceRegistry")
        object.__setattr__(self, "registry_version", registry_version)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "content_sha256", digest)
        assert_source_only_payload(self.to_dict(), "macro_source_registry")

    def resolve_canonical_scope(self, *, provider_id: str, provider_entity_id: str) -> MacroProviderResolution:
        query = (_required_text(provider_id, "provider_id"), _required_text(provider_entity_id, "provider_entity_id"))
        matching = [entry for entry in self.entries if (entry.provider_id, entry.provider_entity_id) == query]
        if not matching:
            return MacroProviderResolution(status="unmapped")
        return MacroProviderResolution(status="resolved", scope=matching[0].canonical_scope)

    def entry_for(self, source_id: str) -> MacroSourceRegistryEntry:
        query = _required_text(source_id, "source_id")
        matching = [entry for entry in self.entries if entry.source_id == query]
        if not matching:
            raise ValueError(f"unknown source_id: {query}")
        return matching[0]

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_version": self.registry_version,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_payload(), "content_sha256": self.content_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceRegistry) -> MacroSourceRegistry:
        if isinstance(value, MacroSourceRegistry):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("macro source registry must be MacroSourceRegistry or a mapping")
        assert_source_only_payload(value, "macro_source_registry")
        return cls(
            registry_version=value.get("registry_version"),
            entries=value.get("entries") or (),
            schema_version=_required_mapping_text(value, "schema_version"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class MacroCollectionTarget:
    """Immutable collection unit: one canonical scope and its declared source IDs."""

    scope: MacroScope | Mapping[str, Any]
    source_ids: Sequence[str]

    def __post_init__(self) -> None:
        scope = MacroScope.from_mapping(self.scope)
        source_ids = _immutable_text_tuple(self.source_ids, "source_ids")
        if not source_ids:
            raise ValueError("source_ids must not be empty")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must be unique")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "source_ids", tuple(sorted(source_ids)))
        assert_source_only_payload(self.to_dict(), "macro_collection_target")

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope.to_dict(), "source_ids": list(self.source_ids)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCollectionTarget) -> MacroCollectionTarget:
        if isinstance(value, MacroCollectionTarget):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("collection target must be MacroCollectionTarget or a mapping")
        assert_source_only_payload(value, "macro_collection_target")
        return cls(scope=value.get("scope"), source_ids=value.get("source_ids") or ())


def _targets_from_registry(registry: MacroSourceRegistry) -> tuple[MacroCollectionTarget, ...]:
    grouped: dict[tuple[str, str], list[str]] = {}
    scopes: dict[tuple[str, str], MacroScope] = {}
    for entry in registry.entries:
        scope = entry.canonical_scope
        key = (scope.kind, scope.entity_id)
        scopes[key] = scope
        grouped.setdefault(key, []).append(entry.source_id)
    return tuple(MacroCollectionTarget(scope=scopes[key], source_ids=tuple(grouped[key])) for key in sorted(grouped))


@dataclass(frozen=True)
class MacroCollectionPlan:
    """Deterministic schedule of typed targets grouped from registry canonical scopes.

    WorldScopeMapping remains the ontology/resolution authority and never schedules
    collection. Unsourced venues stay honest missingness. ``world:market`` appears
    only when declared sources bind that canonical scope.
    """

    registry_version: str
    registry_content_sha256: str
    targets: Sequence[MacroCollectionTarget | Mapping[str, Any]] = ()
    schema_version: str = MACRO_COLLECTION_PLAN_SCHEMA
    content_sha256: str | None = None
    plan_id: str | None = None

    def __post_init__(self) -> None:
        registry_version = _required_text(self.registry_version, "registry_version")
        registry_content_sha256 = _required_text(self.registry_content_sha256, "registry_content_sha256")
        if len(registry_content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in registry_content_sha256
        ):
            raise ValueError("registry_content_sha256 digest must be a sha256 hex digest")
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_COLLECTION_PLAN_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_COLLECTION_PLAN_SCHEMA}")
        targets = tuple(
            item if isinstance(item, MacroCollectionTarget) else MacroCollectionTarget.from_mapping(item)
            for item in self.targets
        )
        targets = tuple(sorted(targets, key=lambda item: (item.scope.kind, item.scope.entity_id)))
        seen_scopes: set[tuple[str, str]] = set()
        seen_sources: set[str] = set()
        for target in targets:
            scope_key = (target.scope.kind, target.scope.entity_id)
            if scope_key in seen_scopes:
                raise ValueError("collection targets must have unique scopes")
            seen_scopes.add(scope_key)
            overlap = seen_sources.intersection(target.source_ids)
            if overlap:
                raise ValueError("source_ids must appear exactly once in a collection plan")
            seen_sources.update(target.source_ids)
        payload = {
            "schema_version": schema_version,
            "registry_version": registry_version,
            "registry_content_sha256": registry_content_sha256,
            "targets": [target.to_dict() for target in targets],
        }
        digest = canonical_sha256(payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical MacroCollectionPlan")
        plan_id = _prefixed_id(_PLAN_ID_PREFIX, payload)
        if self.plan_id is not None and _required_text(self.plan_id, "plan_id") != plan_id:
            raise ValueError("plan_id does not match the canonical collection plan identity")
        object.__setattr__(self, "registry_version", registry_version)
        object.__setattr__(self, "registry_content_sha256", registry_content_sha256)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "plan_id", plan_id)
        assert_source_only_payload(self.to_dict(), "macro_collection_plan")

    def __iter__(self):
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    @property
    def scopes(self) -> tuple[MacroScope, ...]:
        return tuple(target.scope for target in self.targets)

    def target_for(self, scope: MacroScope | Mapping[str, Any]) -> MacroCollectionTarget:
        query = MacroScope.from_mapping(scope)
        key = (query.kind, query.entity_id)
        for target in self.targets:
            if (target.scope.kind, target.scope.entity_id) == key:
                return target
        raise ValueError("collection target is not scheduled by the source registry")

    def require_target(self, target: MacroCollectionTarget) -> MacroCollectionTarget:
        if not isinstance(target, MacroCollectionTarget):
            raise TypeError("target must be MacroCollectionTarget")
        scheduled = self.target_for(target.scope)
        if scheduled != target:
            raise ValueError("collection target does not match the bound registry plan")
        return scheduled

    def bind_registry(self, registry: MacroSourceRegistry) -> None:
        if not isinstance(registry, MacroSourceRegistry):
            raise TypeError("registry must be MacroSourceRegistry")
        expected = type(self).from_registry(registry)
        if (
            self.registry_version != registry.registry_version
            or self.registry_content_sha256 != registry.content_sha256
            or self != expected
        ):
            raise ValueError("collection plan does not match the exact source registry identity")

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_version": self.registry_version,
            "registry_content_sha256": self.registry_content_sha256,
            "targets": [target.to_dict() for target in self.targets],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_payload(), "content_sha256": self.content_sha256, "plan_id": self.plan_id}

    @classmethod
    def from_registry(cls, registry: MacroSourceRegistry) -> MacroCollectionPlan:
        if not isinstance(registry, MacroSourceRegistry):
            raise TypeError("registry must be MacroSourceRegistry")
        return cls(
            registry_version=registry.registry_version,
            registry_content_sha256=registry.content_sha256,
            targets=_targets_from_registry(registry),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCollectionPlan) -> MacroCollectionPlan:
        if isinstance(value, MacroCollectionPlan):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("collection plan must be MacroCollectionPlan or a mapping")
        assert_source_only_payload(value, "macro_collection_plan")
        return cls(
            registry_version=value.get("registry_version"),
            registry_content_sha256=value.get("registry_content_sha256"),
            targets=value.get("targets") or (),
            schema_version=_required_mapping_text(value, "schema_version"),
            content_sha256=value.get("content_sha256"),
            plan_id=value.get("plan_id"),
        )


def require_committed_macro_collection_plan(plan: MacroCollectionPlan) -> MacroCollectionPlan:
    """Fail closed when a derived plan is not the frozen live identity."""

    if not isinstance(plan, MacroCollectionPlan):
        raise TypeError("plan must be MacroCollectionPlan")
    if plan.plan_id != WORLD_MACRO_COLLECTION_PLAN_ID or plan.content_sha256 != WORLD_MACRO_COLLECTION_PLAN_SHA256:
        raise ValueError("derived collection plan drifted from committed identity")
    return plan


def committed_macro_collection_plan(registry: MacroSourceRegistry) -> MacroCollectionPlan:
    """Derive the live plan from the committed registry and refuse any other identity."""

    return require_committed_macro_collection_plan(MacroCollectionPlan.from_registry(registry))


@dataclass(frozen=True)
class MacroDerivationPolicy:
    """Versioned closed vocabularies. Threshold constants live in transform_version, not here."""

    transform_version: str
    producer_version: str = MACRO_PRODUCER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "transform_version", _required_text(self.transform_version, "transform_version"))
        object.__setattr__(self, "producer_version", _required_text(self.producer_version, "producer_version"))
        for values in MACRO_FEATURE_VALUES.values():
            if "unknown" not in values:
                raise ValueError("every macro feature vocabulary must include unknown")

    def allowed_values(self, dimension: str) -> frozenset[str]:
        key = _required_text(dimension, "dimension")
        try:
            return MACRO_FEATURE_VALUES[key]
        except KeyError as exc:
            raise ValueError(f"unknown macro dimension: {key}") from exc


@dataclass(frozen=True)
class MacroCoverage:
    status: str
    required_sources: int
    fresh_sources: int
    missing_source_ids: Sequence[str] = ()

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_COVERAGE_STATUSES:
            allowed = ", ".join(sorted(MACRO_COVERAGE_STATUSES))
            raise ValueError(f"coverage status must be one of: {allowed}")
        required_sources = _non_negative_int(self.required_sources, "required_sources")
        fresh_sources = _non_negative_int(self.fresh_sources, "fresh_sources")
        if fresh_sources > required_sources:
            raise ValueError("fresh_sources cannot exceed required_sources")
        missing = _unique_sorted_texts(self.missing_source_ids, "missing_source_ids")
        if status == "complete" and missing:
            raise ValueError("complete coverage cannot include missing_source_ids")
        if status == "complete" and fresh_sources != required_sources:
            raise ValueError("complete coverage requires every required source to be fresh")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "required_sources", required_sources)
        object.__setattr__(self, "fresh_sources", fresh_sources)
        object.__setattr__(self, "missing_source_ids", missing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "required_sources": self.required_sources,
            "fresh_sources": self.fresh_sources,
            "missing_source_ids": list(self.missing_source_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCoverage) -> MacroCoverage:
        if isinstance(value, MacroCoverage):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("coverage must be MacroCoverage or a mapping")
        assert_source_only_payload(value, "coverage")
        return cls(
            status=value.get("status"),
            required_sources=value.get("required_sources"),
            fresh_sources=value.get("fresh_sources"),
            missing_source_ids=value.get("missing_source_ids") or (),
        )


@dataclass(frozen=True)
class MacroDimensionState:
    dimension: str
    value: str
    coverage_status: str
    method: str
    fact_refs: Sequence[str] = ()

    def __post_init__(self) -> None:
        dimension = _required_text(self.dimension, "dimension")
        if dimension not in MACRO_FEATURE_VALUES:
            raise ValueError(f"unknown macro dimension: {dimension}")
        value = _required_text(self.value, "value")
        if value not in MACRO_FEATURE_VALUES[dimension]:
            allowed = ", ".join(sorted(MACRO_FEATURE_VALUES[dimension]))
            raise ValueError(f"{dimension} must be one of: {allowed}")
        coverage_status = _required_text(self.coverage_status, "coverage_status")
        if coverage_status not in MACRO_COVERAGE_STATUSES:
            allowed = ", ".join(sorted(MACRO_COVERAGE_STATUSES))
            raise ValueError(f"coverage_status must be one of: {allowed}")
        fact_refs = _unique_sorted_texts(self.fact_refs, "fact_refs")
        for ref in fact_refs:
            _validate_prefixed_id(ref, _FACT_VERSION_PREFIX, "fact_refs[]")
        if not fact_refs and value != "unknown":
            raise ValueError("dimension value must be unknown when fact_refs are empty")
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "coverage_status", coverage_status)
        object.__setattr__(self, "method", _required_text(self.method, "method"))
        object.__setattr__(self, "fact_refs", fact_refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "coverage_status": self.coverage_status,
            "method": self.method,
            "fact_refs": list(self.fact_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroDimensionState) -> MacroDimensionState:
        if isinstance(value, MacroDimensionState):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("dimension state must be MacroDimensionState or a mapping")
        assert_source_only_payload(value, "dimension_state")
        return cls(
            dimension=value.get("dimension"),
            value=value.get("value"),
            coverage_status=value.get("coverage_status"),
            method=value.get("method"),
            fact_refs=value.get("fact_refs") or (),
        )


def _observation_id_payload(
    *,
    scope: MacroScope,
    cutoff_at: datetime,
    producer_version: str,
    transform_version: str,
    source_registry_version: str,
    fact_refs: Sequence[str],
) -> dict[str, Any]:
    return {
        "scope": scope.to_dict(),
        "cutoff_at": _iso(cutoff_at),
        "producer_version": producer_version,
        "transform_version": transform_version,
        "source_registry_version": source_registry_version,
        "fact_refs": list(fact_refs),
    }


@dataclass(frozen=True)
class MacroWorldObservation:
    scope: MacroScope | Mapping[str, Any]
    cutoff_at: datetime | str
    fact_refs: Sequence[str]
    features: Mapping[str, str]
    dimensions: Sequence[MacroDimensionState | Mapping[str, Any]]
    coverage: MacroCoverage | Mapping[str, Any]
    producer_version: str = MACRO_PRODUCER_VERSION
    transform_version: str = MACRO_TRANSFORM_VERSION
    source_registry_version: str = MACRO_SOURCE_REGISTRY_VERSION
    valid_until: datetime | str | None = None
    schema_version: str = MACRO_WORLD_OBSERVATION_SCHEMA
    observation_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_WORLD_OBSERVATION_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_WORLD_OBSERVATION_SCHEMA}")
        scope = MacroScope.from_mapping(self.scope)
        cutoff_at = parse_utc_timestamp(self.cutoff_at, "cutoff_at")
        valid_until = _optional_utc(self.valid_until, "valid_until")
        if valid_until is not None and cutoff_at >= valid_until:
            raise ValueError("cutoff_at must be earlier than valid_until")
        producer_version = _required_text(self.producer_version, "producer_version")
        transform_version = _required_text(self.transform_version, "transform_version")
        source_registry_version = _required_text(self.source_registry_version, "source_registry_version")
        fact_refs = _unique_sorted_texts(self.fact_refs, "fact_refs")
        for ref in fact_refs:
            _validate_prefixed_id(ref, _FACT_VERSION_PREFIX, "fact_refs[]")
        if not isinstance(self.features, Mapping):
            raise TypeError("features must be a mapping")
        features: dict[str, str] = {}
        for raw_key, raw_value in self.features.items():
            key = _required_text(raw_key, "feature key")
            if key not in MACRO_FEATURE_VALUES:
                raise ValueError(f"feature is not a closed macro dimension: {key}")
            text = _required_text(raw_value, f"features.{key}")
            if text not in MACRO_FEATURE_VALUES[key]:
                allowed = ", ".join(sorted(MACRO_FEATURE_VALUES[key]))
                raise ValueError(f"features.{key} must be one of: {allowed}")
            features[key] = text
        if tuple(sorted(features)) != tuple(sorted(MACRO_FEATURE_KEYS)):
            raise ValueError("observation features must include macro_regime, rates_regime, and usd_regime")
        dimensions = tuple(
            item if isinstance(item, MacroDimensionState) else MacroDimensionState.from_mapping(item)
            for item in self.dimensions
        )
        by_name = {item.dimension: item for item in dimensions}
        if tuple(sorted(by_name)) != tuple(sorted(MACRO_FEATURE_KEYS)) or len(dimensions) != len(MACRO_FEATURE_KEYS):
            raise ValueError("observation must carry one MacroDimensionState per closed feature")
        ordered_dimensions = tuple(by_name[name] for name in MACRO_FEATURE_KEYS)
        for name in MACRO_FEATURE_KEYS:
            if by_name[name].value != features[name]:
                raise ValueError(f"dimension {name} does not match features.{name}")
            extra = set(by_name[name].fact_refs) - set(fact_refs)
            if extra:
                raise ValueError(f"dimension {name} fact_refs must be a subset of observation fact_refs")
        coverage = (
            self.coverage if isinstance(self.coverage, MacroCoverage) else MacroCoverage.from_mapping(self.coverage)
        )
        observation_id = _prefixed_id(
            _OBSERVATION_ID_PREFIX,
            _observation_id_payload(
                scope=scope,
                cutoff_at=cutoff_at,
                producer_version=producer_version,
                transform_version=transform_version,
                source_registry_version=source_registry_version,
                fact_refs=fact_refs,
            ),
        )
        if self.observation_id is not None and _required_text(self.observation_id, "observation_id") != observation_id:
            raise ValueError("observation_id does not match the canonical observation identity")
        content_payload = {
            "schema_version": schema_version,
            "observation_id": observation_id,
            "scope": scope.to_dict(),
            "cutoff_at": _iso(cutoff_at),
            "producer_version": producer_version,
            "transform_version": transform_version,
            "source_registry_version": source_registry_version,
            "fact_refs": list(fact_refs),
            "features": {key: features[key] for key in MACRO_FEATURE_KEYS},
            "dimensions": [item.to_dict() for item in ordered_dimensions],
            "coverage": coverage.to_dict(),
            "valid_until": _iso(valid_until),
        }
        digest = canonical_sha256(content_payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical MacroWorldObservation")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "producer_version", producer_version)
        object.__setattr__(self, "transform_version", transform_version)
        object.__setattr__(self, "source_registry_version", source_registry_version)
        object.__setattr__(self, "fact_refs", fact_refs)
        object.__setattr__(self, "features", MappingProxyType({key: features[key] for key in MACRO_FEATURE_KEYS}))
        object.__setattr__(self, "dimensions", ordered_dimensions)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "content_sha256", digest)
        assert_source_only_payload(self.to_dict(), "macro_world_observation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "scope": self.scope.to_dict(),
            "cutoff_at": _iso(self.cutoff_at),
            "producer_version": self.producer_version,
            "transform_version": self.transform_version,
            "source_registry_version": self.source_registry_version,
            "fact_refs": list(self.fact_refs),
            "features": dict(self.features),
            "dimensions": [item.to_dict() for item in self.dimensions],
            "coverage": self.coverage.to_dict(),
            "valid_until": _iso(self.valid_until),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroWorldObservation) -> MacroWorldObservation:
        if isinstance(value, MacroWorldObservation):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("macro world observation must be MacroWorldObservation or a mapping")
        assert_source_only_payload(value, "macro_world_observation")
        return cls(
            schema_version=_required_mapping_text(value, "schema_version"),
            scope=value.get("scope"),
            cutoff_at=value.get("cutoff_at"),
            producer_version=_required_mapping_text(value, "producer_version"),
            transform_version=_required_mapping_text(value, "transform_version"),
            source_registry_version=_required_mapping_text(value, "source_registry_version"),
            fact_refs=value.get("fact_refs") or (),
            features=value.get("features") or {},
            dimensions=value.get("dimensions") or (),
            coverage=value.get("coverage"),
            valid_until=value.get("valid_until"),
            observation_id=value.get("observation_id"),
            content_sha256=value.get("content_sha256"),
        )


def reconcile_macro_world_observation(
    existing: MacroWorldObservation,
    incoming: MacroWorldObservation,
) -> MacroWorldObservation:
    if existing.observation_id != incoming.observation_id:
        raise ValueError("observation_id mismatch")
    if existing.content_sha256 != incoming.content_sha256:
        raise ValueError("conflict: same observation id with different content")
    return existing


@dataclass(frozen=True)
class MacroObservationEnvelope:
    """Observation plus the store-attested receipt and reader evidence. Proof is never dropped."""

    observation: MacroWorldObservation | Mapping[str, Any]
    persisted: PersistedWorldRef[MacroObservationId]
    evidence: AvailabilityEvidence

    def __post_init__(self) -> None:
        observation = (
            self.observation
            if isinstance(self.observation, MacroWorldObservation)
            else MacroWorldObservation.from_mapping(self.observation)
        )
        if not isinstance(self.persisted, PersistedWorldRef):
            raise TypeError("envelope persisted must be PersistedWorldRef")
        if not isinstance(self.evidence, AvailabilityEvidence):
            raise TypeError("envelope evidence must be AvailabilityEvidence")
        identity = self.persisted.identity
        if not isinstance(identity, MacroObservationId):
            raise TypeError("envelope persisted identity must be MacroObservationId")
        if identity.value != observation.observation_id:
            raise ValueError("persisted identity must match observation_id")
        receipt = self.persisted.receipt
        if receipt.subject.kind != MACRO_WORLD_OBSERVATION_SUBJECT_KIND:
            raise ValueError("receipt subject kind must be macro_world_observation")
        if receipt.subject.subject_id != observation.observation_id:
            raise ValueError("receipt subject_id must match observation_id")
        if receipt.subject.content_sha256 != observation.content_sha256:
            raise ValueError("receipt content_sha256 must match the observation")
        expected_scope = f"{observation.scope.kind}:{observation.scope.entity_id}"
        if receipt.scope != expected_scope:
            raise ValueError("receipt scope must match the observation canonical scope")
        if self.evidence.receipt != receipt:
            raise ValueError("availability evidence receipt must equal the persisted receipt")
        object.__setattr__(self, "observation", observation)
        assert_source_only_payload(observation.to_dict(), "macro_observation_envelope")


@dataclass(frozen=True)
class MacroContextSearchPlan:
    """Nearest-to-broadest exact-scope search over a resolved market ancestry.

    Venue/country/region/world stay the mapping's scopes. The consumer never
    restamps a broader observation as venue. Unmapped/ambiguous ancestry is
    empty: fail closed, no global world fallback.
    """

    resolution: WorldScopeResolution | Mapping[str, Any]
    admitted_producer_version: str = MACRO_PRODUCER_VERSION
    ancestry: tuple[MacroScope, ...] = field(init=False)

    def __post_init__(self) -> None:
        resolution = WorldScopeResolution.from_mapping(self.resolution)
        admitted = _required_text(self.admitted_producer_version, "admitted_producer_version")
        if admitted not in MACRO_ADMITTED_PRODUCER_VERSIONS:
            raise ValueError("search plan admitted_producer_version is not the live producer contract")
        ancestry = tuple(MacroScope(kind=scope.kind, entity_id=scope.entity_id) for scope in resolution.scopes)
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "admitted_producer_version", admitted)
        object.__setattr__(self, "ancestry", ancestry)
        assert_source_only_payload(self.to_dict(), "macro_context_search_plan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted_producer_version": self.admitted_producer_version,
            "resolution": self.resolution.to_dict(),
            "ancestry": [scope.to_dict() for scope in self.ancestry],
        }

    def select(
        self,
        candidates: Sequence[MacroObservationEnvelope],
        *,
        cutoff_at: datetime | str,
    ) -> MacroContextSelection | None:
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
            raise TypeError("candidates must be a sequence of MacroObservationEnvelope")
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        policy = PointInTimeEligibilityPolicy(admitted_versions=frozenset({self.admitted_producer_version}))
        stale_choice: MacroContextSelection | None = None
        for distance, scope in enumerate(self.ancestry):
            scoped = [
                envelope
                for envelope in candidates
                if isinstance(envelope, MacroObservationEnvelope) and envelope.observation.scope == scope
            ]
            eligible: list[MacroObservationEnvelope] = []
            stale: list[MacroObservationEnvelope] = []
            for envelope in scoped:
                decision = policy.evaluate(
                    evidence=envelope.evidence,
                    cutoff_at=cutoff,
                    valid_until=envelope.observation.valid_until,
                    version=envelope.observation.producer_version,
                )
                if decision.status == "eligible":
                    eligible.append(envelope)
                elif decision.status == "stale":
                    stale.append(envelope)
            if eligible:
                chosen = max(eligible, key=lambda item: (item.observation.cutoff_at, item.observation.observation_id))
                return MacroContextSelection(
                    envelope=chosen,
                    origin_scope=scope,
                    distance=distance,
                    search_plan=self,
                    eligibility_status="eligible",
                )
            if stale and stale_choice is None:
                chosen = max(stale, key=lambda item: (item.observation.cutoff_at, item.observation.observation_id))
                stale_choice = MacroContextSelection(
                    envelope=chosen,
                    origin_scope=scope,
                    distance=distance,
                    search_plan=self,
                    eligibility_status="stale",
                )
        return stale_choice

    @classmethod
    def from_resolution(
        cls,
        resolution: WorldScopeResolution | Mapping[str, Any],
        *,
        admitted_producer_version: str = MACRO_PRODUCER_VERSION,
    ) -> MacroContextSearchPlan:
        return cls(resolution=resolution, admitted_producer_version=admitted_producer_version)


@dataclass(frozen=True)
class MacroContextSelection:
    """Exact-scope v2 observation chosen from a search plan. Origin scope is never restamped."""

    envelope: MacroObservationEnvelope
    origin_scope: MacroScope | Mapping[str, Any]
    distance: int
    search_plan: MacroContextSearchPlan
    eligibility_status: str = "eligible"

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MacroObservationEnvelope):
            raise TypeError("selection envelope must be MacroObservationEnvelope")
        if not isinstance(self.search_plan, MacroContextSearchPlan):
            raise TypeError("selection search_plan must be MacroContextSearchPlan")
        origin = MacroScope.from_mapping(self.origin_scope)
        if self.envelope.observation.scope != origin:
            raise ValueError("selection origin_scope must equal the observation scope")
        if not is_admitted_macro_producer(self.envelope.observation.producer_version):
            raise ValueError("selection cannot admit a producer outside the live contract")
        if self.envelope.observation.producer_version != self.search_plan.admitted_producer_version:
            raise ValueError("selection producer_version must match the search plan")
        distance = _non_negative_int(self.distance, "distance")
        if distance >= len(self.search_plan.ancestry) or self.search_plan.ancestry[distance] != origin:
            raise ValueError("selection distance must index the origin scope in the search ancestry")
        status = _required_text(self.eligibility_status, "eligibility_status")
        if status not in {"eligible", "stale"}:
            raise ValueError("eligibility_status must be eligible or stale")
        object.__setattr__(self, "origin_scope", origin)
        object.__setattr__(self, "distance", distance)
        object.__setattr__(self, "eligibility_status", status)
        assert_source_only_payload(self.to_dict(), "macro_context_selection")

    def provenance(self) -> MacroObservationProvenance:
        return MacroObservationProvenance.from_selection(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.envelope.observation.observation_id,
            "origin_scope": self.origin_scope.to_dict(),
            "distance": self.distance,
            "eligibility_status": self.eligibility_status,
            "admitted_producer_version": self.search_plan.admitted_producer_version,
            "producer_version": self.envelope.observation.producer_version,
        }


@dataclass(frozen=True)
class MacroObservationProvenance:
    """Reconstructible source lineage for a selected or observed macro envelope.

    Ancestry distance is selection-time: collection-time OBSERVES relations carry
    the native origin scope and producer, never a fabricated distance.
    """

    producer_version: str
    origin_scope: MacroScope | Mapping[str, Any]
    observation_id: str
    observation_sha256: str
    fact_refs: Sequence[str] = ()
    ancestry_distance: int | None = None
    mapping_id: str | None = None
    mapping_sha256: str | None = None

    def __post_init__(self) -> None:
        producer_version = _required_text(self.producer_version, "producer_version")
        origin = MacroScope.from_mapping(self.origin_scope)
        observation_id = _validate_prefixed_id(self.observation_id, _OBSERVATION_ID_PREFIX, "observation_id")
        observation_sha256 = _required_text(self.observation_sha256, "observation_sha256")
        if len(observation_sha256) != 64 or any(char not in "0123456789abcdef" for char in observation_sha256):
            raise ValueError("observation_sha256 digest must be a sha256 hex digest")
        fact_refs = _unique_sorted_texts(self.fact_refs, "fact_refs")
        for ref in fact_refs:
            _validate_prefixed_id(ref, _FACT_VERSION_PREFIX, "fact_refs[]")
        distance = (
            None if self.ancestry_distance is None else _non_negative_int(self.ancestry_distance, "ancestry_distance")
        )
        mapping_id = None if self.mapping_id is None else _required_text(self.mapping_id, "mapping_id")
        mapping_sha256 = None if self.mapping_sha256 is None else _required_text(self.mapping_sha256, "mapping_sha256")
        if (mapping_id is None) != (mapping_sha256 is None):
            raise ValueError("mapping_id and mapping_sha256 must be provided together")
        if mapping_sha256 is not None and (
            len(mapping_sha256) != 64 or any(char not in "0123456789abcdef" for char in mapping_sha256)
        ):
            raise ValueError("mapping_sha256 digest must be a sha256 hex digest")
        object.__setattr__(self, "producer_version", producer_version)
        object.__setattr__(self, "origin_scope", origin)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "observation_sha256", observation_sha256)
        object.__setattr__(self, "fact_refs", fact_refs)
        object.__setattr__(self, "ancestry_distance", distance)
        object.__setattr__(self, "mapping_id", mapping_id)
        object.__setattr__(self, "mapping_sha256", mapping_sha256)
        assert_source_only_payload(self.to_dict(), "macro_observation_provenance")

    def to_source_refs(self) -> tuple[str, ...]:
        refs = [
            f"{self.observation_id}/{self.observation_sha256}",
            macro_observes_producer_ref(self.producer_version),
            macro_origin_scope_ref(self.origin_scope),
        ]
        if self.ancestry_distance is not None:
            refs.append(macro_ancestry_distance_ref(self.ancestry_distance))
        if self.mapping_id is not None and self.mapping_sha256 is not None:
            refs.append(f"{self.mapping_id}/{self.mapping_sha256}")
        refs.extend(self.fact_refs)
        return tuple(refs)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "producer_version": self.producer_version,
            "origin_scope": self.origin_scope.to_dict(),
            "observation_id": self.observation_id,
            "observation_sha256": self.observation_sha256,
            "fact_refs": list(self.fact_refs),
        }
        if self.ancestry_distance is not None:
            payload["ancestry_distance"] = self.ancestry_distance
        if self.mapping_id is not None:
            payload["mapping_id"] = self.mapping_id
            payload["mapping_sha256"] = self.mapping_sha256
        return payload

    @classmethod
    def from_selection(cls, selection: MacroContextSelection) -> MacroObservationProvenance:
        if not isinstance(selection, MacroContextSelection):
            raise TypeError("selection must be MacroContextSelection")
        observation = selection.envelope.observation
        resolution = selection.search_plan.resolution
        return cls(
            producer_version=observation.producer_version,
            origin_scope=selection.origin_scope,
            observation_id=observation.observation_id,
            observation_sha256=observation.content_sha256,
            fact_refs=observation.fact_refs,
            ancestry_distance=selection.distance,
            mapping_id=resolution.mapping_id,
            mapping_sha256=resolution.mapping_sha256,
        )

    @classmethod
    def from_source_refs(
        cls,
        refs: Sequence[str],
        *,
        origin_scope: MacroScope | Mapping[str, Any] | None = None,
        ancestry_distance: int | None = None,
    ) -> MacroObservationProvenance:
        tokens = _immutable_text_tuple(refs, "source_refs")
        producer_version: str | None = None
        observation_id: str | None = None
        observation_sha256: str | None = None
        mapping_id: str | None = None
        mapping_sha256: str | None = None
        parsed_origin = None if origin_scope is None else MacroScope.from_mapping(origin_scope)
        parsed_distance = ancestry_distance
        facts: list[str] = []
        for token in tokens:
            if token.startswith(_PRODUCER_REF_PREFIX):
                producer_version = token[len(_PRODUCER_REF_PREFIX) :]
                continue
            if token.startswith(_ORIGIN_SCOPE_REF_PREFIX):
                token_origin = parse_macro_origin_scope_ref(token)
                if parsed_origin is not None and token_origin != parsed_origin:
                    raise ValueError("origin_scope token contradicts the provided origin")
                parsed_origin = token_origin
                continue
            if token.startswith(_ANCESTRY_DISTANCE_REF_PREFIX):
                token_distance = parse_macro_ancestry_distance_ref(token)
                if parsed_distance is not None and token_distance != parsed_distance:
                    raise ValueError("ancestry_distance token contradicts the provided distance")
                parsed_distance = token_distance
                continue
            if token.startswith(f"{_FACT_VERSION_PREFIX}:"):
                facts.append(token)
                continue
            split = _split_identity_hash(token)
            if split is None:
                continue
            left, digest = split
            if left.startswith(f"{_OBSERVATION_ID_PREFIX}:"):
                observation_id = left
                observation_sha256 = digest
            elif left.startswith("world_scope_mapping."):
                mapping_id = left
                mapping_sha256 = digest
        if producer_version is None or observation_id is None or observation_sha256 is None or parsed_origin is None:
            raise ValueError("source_refs are insufficient to reconstruct macro provenance")
        return cls(
            producer_version=producer_version,
            origin_scope=parsed_origin,
            observation_id=observation_id,
            observation_sha256=observation_sha256,
            fact_refs=tuple(facts),
            ancestry_distance=parsed_distance,
            mapping_id=mapping_id,
            mapping_sha256=mapping_sha256,
        )


def _event_id_for(payload: Mapping[str, Any]) -> str:
    return _prefixed_id(_EVENT_ID_PREFIX, payload)


def _set_event_identity(event: Any, payload: Mapping[str, Any]) -> None:
    computed = _event_id_for(payload)
    provided = getattr(event, "event_id")
    if provided is not None and _required_text(provided, "event_id") != computed:
        raise ValueError("event_id does not match the canonical collection event")
    schema_version = _required_text(getattr(event, "schema_version"), "schema_version")
    if schema_version != MACRO_COLLECTION_EVENT_SCHEMA:
        raise ValueError(f"schema_version must be {MACRO_COLLECTION_EVENT_SCHEMA}")
    object.__setattr__(event, "schema_version", schema_version)
    object.__setattr__(event, "event_id", computed)
    assert_source_only_payload(event.to_dict(), "macro_collection_event")


def _run_id_payload(
    *,
    scope: MacroScope,
    cutoff_at: datetime,
    expected_source_ids: Sequence[str],
    producer_version: str,
    transform_version: str,
    source_registry_version: str,
) -> dict[str, Any]:
    return {
        "scope": scope.to_dict(),
        "cutoff_at": _iso(cutoff_at),
        "expected_source_ids": list(sorted(expected_source_ids)),
        "producer_version": producer_version,
        "transform_version": transform_version,
        "source_registry_version": source_registry_version,
    }


@dataclass(frozen=True)
class MacroCollectionRegistered:
    scope: MacroScope | Mapping[str, Any]
    cutoff_at: datetime | str
    expected_source_ids: Sequence[str]
    producer_version: str = MACRO_PRODUCER_VERSION
    transform_version: str = MACRO_TRANSFORM_VERSION
    source_registry_version: str = MACRO_SOURCE_REGISTRY_VERSION
    run_id: str | None = None
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_collection_registered"

    def __post_init__(self) -> None:
        scope = MacroScope.from_mapping(self.scope)
        cutoff_at = parse_utc_timestamp(self.cutoff_at, "cutoff_at")
        expected = _immutable_text_tuple(self.expected_source_ids, "expected_source_ids")
        if not expected:
            raise ValueError("expected_source_ids must not be empty")
        if len(set(expected)) != len(expected):
            raise ValueError("expected_source_ids must be unique")
        expected = tuple(sorted(expected))
        producer_version = _required_text(self.producer_version, "producer_version")
        transform_version = _required_text(self.transform_version, "transform_version")
        source_registry_version = _required_text(self.source_registry_version, "source_registry_version")
        run_id = _prefixed_id(
            _RUN_ID_PREFIX,
            _run_id_payload(
                scope=scope,
                cutoff_at=cutoff_at,
                expected_source_ids=expected,
                producer_version=producer_version,
                transform_version=transform_version,
                source_registry_version=source_registry_version,
            ),
        )
        if self.run_id is not None and _required_text(self.run_id, "run_id") != run_id:
            raise ValueError("run_id does not match the canonical collection run identity")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "expected_source_ids", expected)
        object.__setattr__(self, "producer_version", producer_version)
        object.__setattr__(self, "transform_version", transform_version)
        object.__setattr__(self, "source_registry_version", source_registry_version)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "event_type", "macro_collection_registered")
        _set_event_identity(
            self,
            {
                "event_type": "macro_collection_registered",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
                "scope": scope.to_dict(),
                "cutoff_at": _iso(cutoff_at),
                "expected_source_ids": list(expected),
                "producer_version": producer_version,
                "transform_version": transform_version,
                "source_registry_version": source_registry_version,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "scope": self.scope.to_dict(),
            "cutoff_at": _iso(self.cutoff_at),
            "expected_source_ids": list(self.expected_source_ids),
            "producer_version": self.producer_version,
            "transform_version": self.transform_version,
            "source_registry_version": self.source_registry_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCollectionRegistered) -> MacroCollectionRegistered:
        if isinstance(value, MacroCollectionRegistered):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("registered event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        return cls(
            scope=value.get("scope"),
            cutoff_at=value.get("cutoff_at"),
            expected_source_ids=value.get("expected_source_ids") or (),
            producer_version=_required_mapping_text(value, "producer_version"),
            transform_version=_required_mapping_text(value, "transform_version"),
            source_registry_version=_required_mapping_text(value, "source_registry_version"),
            run_id=value.get("run_id"),
            event_id=value.get("event_id"),
            schema_version=_required_mapping_text(value, "schema_version"),
        )


@dataclass(frozen=True)
class MacroCollectionStarted:
    run_id: str
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_collection_started"

    def __post_init__(self) -> None:
        run_id = _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "event_type", "macro_collection_started")
        _set_event_identity(
            self,
            {
                "event_type": "macro_collection_started",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCollectionStarted) -> MacroCollectionStarted:
        if isinstance(value, MacroCollectionStarted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("started event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        return cls(
            run_id=value.get("run_id"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_COLLECTION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroSourceCompleted:
    run_id: str
    source_id: str
    fact_version_ids: Sequence[str] = ()
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_source_completed"

    def __post_init__(self) -> None:
        run_id = _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id")
        source_id = _required_text(self.source_id, "source_id")
        fact_version_ids = _unique_sorted_texts(self.fact_version_ids, "fact_version_ids")
        for ref in fact_version_ids:
            _validate_prefixed_id(ref, _FACT_VERSION_PREFIX, "fact_version_ids[]")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "fact_version_ids", fact_version_ids)
        object.__setattr__(self, "event_type", "macro_source_completed")
        _set_event_identity(
            self,
            {
                "event_type": "macro_source_completed",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
                "source_id": source_id,
                "fact_version_ids": list(fact_version_ids),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "source_id": self.source_id,
            "fact_version_ids": list(self.fact_version_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceCompleted) -> MacroSourceCompleted:
        if isinstance(value, MacroSourceCompleted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("source completed event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        return cls(
            run_id=value.get("run_id"),
            source_id=value.get("source_id"),
            fact_version_ids=value.get("fact_version_ids") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_COLLECTION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroSourceFailed:
    run_id: str
    source_id: str
    reason: str
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_source_failed"

    def __post_init__(self) -> None:
        run_id = _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id")
        source_id = _required_text(self.source_id, "source_id")
        reason = _required_text(self.reason, "reason")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "event_type", "macro_source_failed")
        _set_event_identity(
            self,
            {
                "event_type": "macro_source_failed",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
                "source_id": source_id,
                "reason": reason,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "source_id": self.source_id,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceFailed) -> MacroSourceFailed:
        if isinstance(value, MacroSourceFailed):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("source failed event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        return cls(
            run_id=value.get("run_id"),
            source_id=value.get("source_id"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_COLLECTION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroObservationPublished:
    run_id: str
    envelope: MacroObservationEnvelope
    observation_id: str | None = None
    content_sha256: str | None = None
    receipt_id: str | None = None
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_observation_published"

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MacroObservationEnvelope):
            raise TypeError("MacroObservationPublished requires a typed envelope")
        run_id = _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id")
        observation = self.envelope.observation
        receipt = self.envelope.persisted.receipt
        observation_id = observation.observation_id
        content_sha256 = observation.content_sha256
        receipt_id = receipt.receipt_id
        if self.observation_id is not None and _required_text(self.observation_id, "observation_id") != observation_id:
            raise ValueError("published observation_id must match the envelope")
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != content_sha256:
            raise ValueError("published content_sha256 must match the envelope")
        if self.receipt_id is not None and _required_text(self.receipt_id, "receipt_id") != receipt_id:
            raise ValueError("published receipt_id must match the store-attested envelope receipt")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "content_sha256", content_sha256)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "event_type", "macro_observation_published")
        _set_event_identity(
            self,
            {
                "event_type": "macro_observation_published",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
                "observation_id": observation_id,
                "content_sha256": content_sha256,
                "receipt_id": receipt_id,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "observation_id": self.observation_id,
            "content_sha256": self.content_sha256,
            "receipt_id": self.receipt_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroObservationPublished) -> MacroObservationPublished:
        if isinstance(value, MacroObservationPublished):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("published event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        envelope = value.get("envelope")
        if not isinstance(envelope, MacroObservationEnvelope):
            raise TypeError("MacroObservationPublished requires a typed envelope")
        return cls(
            run_id=value.get("run_id"),
            envelope=envelope,
            observation_id=value.get("observation_id"),
            content_sha256=value.get("content_sha256"),
            receipt_id=value.get("receipt_id"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_COLLECTION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroCollectionCompleted:
    run_id: str
    status: str
    observation_id: str | None = None
    reason: str | None = None
    event_id: str | None = None
    schema_version: str = MACRO_COLLECTION_EVENT_SCHEMA
    event_type: str = "macro_collection_completed"

    def __post_init__(self) -> None:
        run_id = _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id")
        status = _required_text(self.status, "status")
        if status not in MACRO_TERMINAL_STATUSES:
            allowed = ", ".join(sorted(MACRO_TERMINAL_STATUSES))
            raise ValueError(f"terminal status must be one of: {allowed}")
        observation_id = (
            None
            if self.observation_id is None
            else _validate_prefixed_id(self.observation_id, _OBSERVATION_ID_PREFIX, "observation_id")
        )
        if status == "failed" and observation_id is not None:
            raise ValueError("failed run must not publish an observation")
        if status in {"completed", "completed_partial"} and observation_id is None:
            raise ValueError("successful completion requires a published observation_id")
        reason = None if self.reason is None else _required_text(self.reason, "reason")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "event_type", "macro_collection_completed")
        _set_event_identity(
            self,
            {
                "event_type": "macro_collection_completed",
                "schema_version": MACRO_COLLECTION_EVENT_SCHEMA,
                "run_id": run_id,
                "status": status,
                "observation_id": observation_id,
                "reason": reason,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "status": self.status,
            "observation_id": self.observation_id,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroCollectionCompleted) -> MacroCollectionCompleted:
        if isinstance(value, MacroCollectionCompleted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("completed event must be a mapping")
        assert_source_only_payload(value, "macro_collection_event")
        return cls(
            run_id=value.get("run_id"),
            status=value.get("status"),
            observation_id=value.get("observation_id"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_COLLECTION_EVENT_SCHEMA),
        )


MacroCollectionEvent = (
    MacroCollectionRegistered
    | MacroCollectionStarted
    | MacroSourceCompleted
    | MacroSourceFailed
    | MacroObservationPublished
    | MacroCollectionCompleted
)

_EVENT_CLASSES = (
    MacroCollectionRegistered,
    MacroCollectionStarted,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroObservationPublished,
    MacroCollectionCompleted,
)
_EVENT_PARSERS = {
    "macro_collection_registered": MacroCollectionRegistered.from_mapping,
    "macro_collection_started": MacroCollectionStarted.from_mapping,
    "macro_source_completed": MacroSourceCompleted.from_mapping,
    "macro_source_failed": MacroSourceFailed.from_mapping,
    "macro_observation_published": MacroObservationPublished.from_mapping,
    "macro_collection_completed": MacroCollectionCompleted.from_mapping,
}


def parse_macro_collection_event(value: Mapping[str, Any] | MacroCollectionEvent) -> MacroCollectionEvent:
    if isinstance(value, _EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("collection event must be a mapping or typed event")
    assert_source_only_payload(value, "macro_collection_event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown macro collection event_type: {event_type}") from exc
    return parser(value)


@dataclass(frozen=True)
class MacroCollectionTerminalResult:
    status: str
    run_id: str
    observation_id: str | None
    failed_source_ids: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_TERMINAL_STATUSES:
            allowed = ", ".join(sorted(MACRO_TERMINAL_STATUSES))
            raise ValueError(f"terminal status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _RUN_ID_PREFIX, "run_id"))
        object.__setattr__(
            self,
            "observation_id",
            None
            if self.observation_id is None
            else _validate_prefixed_id(self.observation_id, _OBSERVATION_ID_PREFIX, "observation_id"),
        )
        object.__setattr__(self, "failed_source_ids", _unique_sorted_texts(self.failed_source_ids, "failed_source_ids"))
        object.__setattr__(self, "reason", None if self.reason is None else _required_text(self.reason, "reason"))
        if status == "failed" and self.observation_id is not None:
            raise ValueError("failed terminal result must not carry an observation_id")
        if status in {"completed", "completed_partial"} and self.observation_id is None:
            raise ValueError("successful terminal result requires an observation_id")


def _source_result_equivalent(
    existing: MacroSourceCompleted | MacroSourceFailed,
    incoming: MacroSourceCompleted | MacroSourceFailed,
) -> bool:
    if type(existing) is not type(incoming):
        return False
    if existing.source_id != incoming.source_id:
        return False
    if isinstance(existing, MacroSourceCompleted) and isinstance(incoming, MacroSourceCompleted):
        return existing.fact_version_ids == incoming.fact_version_ids
    if isinstance(existing, MacroSourceFailed) and isinstance(incoming, MacroSourceFailed):
        return existing.reason == incoming.reason
    return False


@dataclass(frozen=True)
class MacroCollectionRun:
    """Event-sourced collection aggregate. Status is derived; callers never assign it."""

    events: Sequence[MacroCollectionEvent | Mapping[str, Any]]

    def __post_init__(self) -> None:
        events = tuple(parse_macro_collection_event(item) for item in self.events)
        if not events:
            raise ValueError("MacroCollectionRun requires a registered event")
        if not isinstance(events[0], MacroCollectionRegistered):
            raise ValueError("first event must be a registered collection event")
        seen_sources: dict[str, MacroSourceCompleted | MacroSourceFailed] = {}
        started = False
        published: MacroObservationPublished | None = None
        completed: MacroCollectionCompleted | None = None
        registered = events[0]
        expected = set(registered.expected_source_ids)
        for event in events[1:]:
            if event.run_id != registered.run_id:
                raise ValueError("event run_id mismatch")
            if completed is not None:
                raise ValueError("no events are allowed after completion")
            if isinstance(event, MacroCollectionRegistered):
                raise ValueError("duplicate registered event")
            if isinstance(event, MacroCollectionStarted):
                if started:
                    raise ValueError("collection already started")
                started = True
                continue
            if isinstance(event, (MacroSourceCompleted, MacroSourceFailed)):
                if not started:
                    raise ValueError("sources cannot be recorded before start")
                if published is not None:
                    raise ValueError("sources cannot be recorded after publish")
                if event.source_id not in expected:
                    raise ValueError(f"unknown source_id: {event.source_id}")
                previous = seen_sources.get(event.source_id)
                if previous is not None:
                    raise ValueError("duplicate source result")
                seen_sources[event.source_id] = event
                continue
            if isinstance(event, MacroObservationPublished):
                if not started:
                    raise ValueError("cannot publish before start")
                if published is not None:
                    raise ValueError("observation already published")
                if set(seen_sources) != expected:
                    raise ValueError("publish requires a terminal result for every source")
                if not isinstance(event.envelope, MacroObservationEnvelope):
                    raise TypeError("published event requires a MacroObservationEnvelope")
                published = event
                continue
            if isinstance(event, MacroCollectionCompleted):
                if set(seen_sources) != expected:
                    raise ValueError("complete requires a terminal result for every source")
                failed_ids = tuple(
                    sorted(
                        source_id for source_id, result in seen_sources.items() if isinstance(result, MacroSourceFailed)
                    )
                )
                if event.status == "failed":
                    if published is not None or event.observation_id is not None:
                        raise ValueError("failed run must not publish an observation")
                elif event.status == "completed":
                    if (
                        published is None
                        or published.envelope is None
                        or event.observation_id != published.observation_id
                        or failed_ids
                    ):
                        raise ValueError("completed run requires a published envelope and no failed sources")
                elif event.status == "completed_partial":
                    if (
                        published is None
                        or published.envelope is None
                        or event.observation_id != published.observation_id
                        or not failed_ids
                    ):
                        raise ValueError(
                            "completed_partial requires a published envelope and at least one failed source"
                        )
                completed = event
                continue
            raise TypeError(f"unsupported collection event: {type(event).__name__}")
        object.__setattr__(self, "events", events)

    @property
    def registered(self) -> MacroCollectionRegistered:
        event = self.events[0]
        assert isinstance(event, MacroCollectionRegistered)
        return event

    @property
    def run_id(self) -> str:
        return self.registered.run_id

    @property
    def scope(self) -> MacroScope:
        return self.registered.scope

    @property
    def cutoff_at(self) -> datetime:
        return self.registered.cutoff_at

    @property
    def expected_source_ids(self) -> tuple[str, ...]:
        return self.registered.expected_source_ids

    @property
    def source_results(self) -> Mapping[str, MacroSourceCompleted | MacroSourceFailed]:
        results: dict[str, MacroSourceCompleted | MacroSourceFailed] = {}
        for event in self.events:
            if isinstance(event, (MacroSourceCompleted, MacroSourceFailed)):
                results[event.source_id] = event
        return MappingProxyType(results)

    def _published_event(self) -> MacroObservationPublished | None:
        for event in reversed(self.events):
            if isinstance(event, MacroObservationPublished):
                return event
        return None

    @property
    def published_envelope(self) -> MacroObservationEnvelope | None:
        published = self._published_event()
        return None if published is None else published.envelope

    def _completed_event(self) -> MacroCollectionCompleted | None:
        for event in reversed(self.events):
            if isinstance(event, MacroCollectionCompleted):
                return event
        return None

    def _failed_source_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                source_id for source_id, result in self.source_results.items() if isinstance(result, MacroSourceFailed)
            )
        )

    def _sources_terminal(self) -> bool:
        return set(self.source_results) == set(self.expected_source_ids)

    @property
    def status(self) -> str:
        completed = self._completed_event()
        if completed is not None:
            return completed.status
        if any(isinstance(event, MacroCollectionStarted) for event in self.events):
            return "collecting"
        return "registered"

    @property
    def terminal_result(self) -> MacroCollectionTerminalResult | None:
        completed = self._completed_event()
        if completed is None:
            return None
        return MacroCollectionTerminalResult(
            status=completed.status,
            run_id=self.run_id,
            observation_id=completed.observation_id,
            failed_source_ids=self._failed_source_ids(),
            reason=completed.reason,
        )

    @classmethod
    def register(
        cls,
        *,
        scope: MacroScope | Mapping[str, Any],
        cutoff_at: datetime | str,
        expected_source_ids: Sequence[str],
        producer_version: str = MACRO_PRODUCER_VERSION,
        transform_version: str = MACRO_TRANSFORM_VERSION,
        source_registry_version: str = MACRO_SOURCE_REGISTRY_VERSION,
    ) -> MacroCollectionRun:
        event = MacroCollectionRegistered(
            scope=scope,
            cutoff_at=cutoff_at,
            expected_source_ids=expected_source_ids,
            producer_version=producer_version,
            transform_version=transform_version,
            source_registry_version=source_registry_version,
        )
        return cls(events=(event,))

    @classmethod
    def from_events(cls, events: Sequence[MacroCollectionEvent | Mapping[str, Any]]) -> MacroCollectionRun:
        return cls(events=events)

    def start(self) -> MacroCollectionRun:
        if self.status == "collecting":
            return self
        if self.status != "registered":
            raise ValueError("collection can only start from registered")
        return MacroCollectionRun(events=(*self.events, MacroCollectionStarted(run_id=self.run_id)))

    def record_source_result(self, result: MacroSourceCompleted | MacroSourceFailed) -> MacroCollectionRun:
        if not isinstance(result, (MacroSourceCompleted, MacroSourceFailed)):
            raise TypeError("source result must be MacroSourceCompleted or MacroSourceFailed")
        if self.status != "collecting":
            raise ValueError("source results can only be recorded while collecting")
        if result.run_id != self.run_id:
            raise ValueError("source result run_id mismatch")
        if result.source_id not in set(self.expected_source_ids):
            raise ValueError(f"unknown source_id: {result.source_id}")
        previous = self.source_results.get(result.source_id)
        if previous is not None:
            if _source_result_equivalent(previous, result):
                return self
            raise ValueError("conflict: same source_id with a different terminal result")
        return MacroCollectionRun(events=(*self.events, result))

    def publish(self, envelope: MacroObservationEnvelope) -> MacroCollectionRun:
        if not isinstance(envelope, MacroObservationEnvelope):
            raise TypeError("publish requires a MacroObservationEnvelope")
        if self.status != "collecting":
            raise ValueError("publish is only allowed while collecting")
        if not self._sources_terminal():
            raise ValueError("publish requires a terminal result for every source")
        if not envelope.observation.fact_refs:
            raise ValueError("publish requires at least one admissible fact")
        observation = envelope.observation
        if observation.scope != self.scope:
            raise ValueError("published observation scope must match the run")
        if observation.cutoff_at != self.cutoff_at:
            raise ValueError("published observation cutoff_at must match the run")
        if observation.producer_version != self.registered.producer_version:
            raise ValueError("published observation producer_version must match the run")
        if observation.transform_version != self.registered.transform_version:
            raise ValueError("published observation transform_version must match the run")
        if observation.source_registry_version != self.registered.source_registry_version:
            raise ValueError("published observation source_registry_version must match the run")
        known_facts = {
            fact_id
            for result in self.source_results.values()
            if isinstance(result, MacroSourceCompleted)
            for fact_id in result.fact_version_ids
        }
        unknown = set(observation.fact_refs) - known_facts
        if unknown:
            raise ValueError("published observation fact_refs must come from completed sources")
        failed_ids = set(self._failed_source_ids())
        expected_ids = set(self.expected_source_ids)
        coverage = observation.coverage
        if coverage.required_sources != len(self.expected_source_ids):
            raise ValueError("published coverage required_sources must match the expected source count")
        missing = set(coverage.missing_source_ids)
        if not missing <= expected_ids:
            raise ValueError("published coverage missing_source_ids must be expected sources")
        if not failed_ids <= missing:
            raise ValueError("published coverage must include every failed source")
        if failed_ids and coverage.status == "complete":
            raise ValueError("published coverage cannot be complete while sources failed")
        existing = self._published_event()
        if existing is not None:
            if existing.envelope == envelope:
                return self
            raise ValueError("conflict: observation already published with different content")
        event = MacroObservationPublished(run_id=self.run_id, envelope=envelope)
        return MacroCollectionRun(events=(*self.events, event))

    def complete(self, envelope: MacroObservationEnvelope | None = None) -> MacroCollectionRun:
        if self.status in MACRO_TERMINAL_STATUSES:
            return self
        if self.status != "collecting":
            raise ValueError("complete is only allowed while collecting")
        if not self._sources_terminal():
            raise ValueError("complete requires a terminal result for every source")
        published = self._published_event()
        if published is None:
            if envelope is not None:
                raise ValueError("complete cannot accept an envelope without a published observation")
            event = MacroCollectionCompleted(
                run_id=self.run_id,
                status="failed",
                observation_id=None,
                reason="no_admissible_observation",
            )
        else:
            if envelope is None:
                raise ValueError("complete requires a typed envelope proving publication")
            if envelope != published.envelope:
                raise ValueError("complete envelope must match the published MacroObservationEnvelope")
            failed_ids = self._failed_source_ids()
            event = MacroCollectionCompleted(
                run_id=self.run_id,
                status="completed_partial" if failed_ids else "completed",
                observation_id=published.observation_id,
            )
        return MacroCollectionRun(events=(*self.events, event))


def evaluate_macro_point_in_time(
    *,
    evidence: AvailabilityEvidence | None,
    cutoff_at: datetime | str,
    valid_until: datetime | str | None = None,
    superseded: bool = False,
    version: str | None = None,
    admitted_versions: frozenset[str] | None = None,
):
    """Domain PIT gate: effective_ready_at <= cutoff_at and cutoff_at < valid_until."""

    return PointInTimeEligibilityPolicy(admitted_versions=admitted_versions).evaluate(
        evidence=evidence,
        cutoff_at=cutoff_at,
        valid_until=valid_until,
        superseded=superseded,
        version=version,
    )


__all__ = [
    "MACRO_COLLECTION_PLAN_SCHEMA",
    "MACRO_COVERAGE_STATUSES",
    "MACRO_FACT_KINDS",
    "MACRO_FEATURE_KEYS",
    "MACRO_FEATURE_VALUES",
    "MACRO_ADMITTED_PRODUCER_VERSIONS",
    "MACRO_LANE_IDENTITY",
    "MACRO_LANE_IDENTITY_V1",
    "MACRO_POLICY_DENYLIST",
    "MACRO_PRODUCER_VERSION",
    "MACRO_PRODUCER_VERSION_V1",
    "MACRO_REGIME_VALUES",
    "MACRO_SOURCE_REGISTRY_VERSION",
    "MACRO_TERMINAL_STATUSES",
    "MACRO_TRANSFORM_VERSION",
    "MACRO_WORLD_OBSERVATION_SUBJECT_KIND",
    "WORLD_MACRO_COLLECTION_PLAN_ID",
    "WORLD_MACRO_COLLECTION_PLAN_SHA256",
    "MacroObservationProvenance",
    "committed_macro_collection_plan",
    "macro_ancestry_distance_ref",
    "macro_origin_scope_ref",
    "parse_macro_ancestry_distance_ref",
    "parse_macro_origin_scope_ref",
    "require_committed_macro_collection_plan",
    "MacroCategoryValue",
    "MacroCollectionCompleted",
    "MacroCollectionEvent",
    "MacroCollectionEventId",
    "MacroCollectionPlan",
    "MacroCollectionRegistered",
    "MacroCollectionTarget",
    "MacroCollectionRun",
    "MacroCollectionRunId",
    "MacroCollectionStarted",
    "MacroCollectionTerminalResult",
    "MacroContextSearchPlan",
    "MacroContextSelection",
    "MacroCoverage",
    "MacroDerivationPolicy",
    "MacroDimensionState",
    "MacroFactKey",
    "MacroFactSource",
    "MacroNumericValue",
    "MacroObservationEnvelope",
    "MacroObservationId",
    "MacroProviderResolution",
    "MacroScope",
    "MacroSourceCompleted",
    "MacroSourceFailed",
    "MacroSourceFact",
    "MacroSourceFactVersionId",
    "MacroSourceRegistry",
    "MacroSourceRegistryEntry",
    "MacroWorldObservation",
    "WorldScopeMapping",
    "WorldScopeResolution",
    "assert_source_only_payload",
    "evaluate_macro_point_in_time",
    "is_admitted_macro_producer",
    "macro_observes_producer_ref",
    "parse_macro_collection_event",
    "parse_macro_value",
    "reconcile_macro_source_fact",
    "reconcile_macro_world_observation",
]
