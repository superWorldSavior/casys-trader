"""Immutable, action-free contracts for point-in-time world observations.

The records in this module describe what was observable about the market at a
sampling slot.  They deliberately do *not* contain a Trader action, portfolio
state, scheduler state, fill, or realised PnL.  Those are controls or critic
views and must be persisted separately from a market transition dataset.

This module is stdlib-only so capture, storage, labelling, and baseline code
can share the same deterministic identities without importing runtime code.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any

WORLD_EPISODE_SCHEMA_VERSION = "world_episode.v1"
WORLD_OUTCOME_SCHEMA_VERSION = "world_outcome.v1"
WORLD_PREDICTION_SCHEMA_VERSION = "world_prediction.v1"
MARKET_FEATURE_CONTRACT_VERSION = "market_ohlcv_causal.v1"
_INTERVAL_PATTERN = re.compile(r"^(?P<count>\d+(?:\.\d+)?)(?P<unit>[mhd])$")
_BAR_CLOSE_SEMANTICS = frozenset({"bar_close", "bar_end", "close", "end"})
_BAR_START_SEMANTICS = frozenset({"bar_start", "start"})

FRESHNESS_STATUSES = frozenset({"fresh", "stale", "unknown", "missing"})
OUTCOME_STATUSES = frozenset({"pending", "observed", "missing", "stale", "unknown"})
OUTCOME_EVENT_TYPES = frozenset({"outcome_scheduled", "outcome_observed", "outcome_unavailable", "outcome_corrected"})
PREDICTION_CLASSES = ("DOWN", "FLAT", "UP")
PREDICTION_STATUSES = frozenset({"warming_up", "shadow_only"})
PREDICTION_TIERS = frozenset({"uniform", "global", "coarse", "exact"})
_LEGACY_PREDICTION_CLASSES = {"down": "DOWN", "flat": "FLAT", "up": "UP"}
SIMPLE_RETURN_DIRECTION_BAND = 0.005

# The initial feature contract intentionally remains small and semantic.  A
# new feature requires an explicit contract-version bump rather than silently
# widening a historical vector.
ALLOWED_CATEGORICAL_FEATURES = frozenset(
    {
        "asset_family",
        "venue",
        "bar_interval",
        "session_phase",
        "market_regime",
        "volatility_state",
        "momentum_bucket",
        "return_bucket",
        "atr_bucket",
        "range_position_bucket",
        "family_direction",
        "family_consensus_bucket",
        "macro_regime",
        "geopolitical_risk_bucket",
        "data_freshness",
        "source_status",
        # Existing market-domain nomenclature retained for a lossless V1
        # projection; capture policy decides which of these enter a baseline.
        "regime",
        "vol_state",
        "candlestick_signal",
        "chart_breakout",
        "market_status",
        "family_regime",
        "situation_regime",
        "trend",
        "cross_asset_regime",
    }
)

ALLOWED_NUMERIC_FEATURES = frozenset(
    {
        "return",
        "return_1h",
        "return_4h",
        "return_1d",
        "return_5d",
        "momentum",
        "momentum_1h",
        "momentum_4h",
        "momentum_1d",
        "volatility",
        "ohlc_volatility",
        "realized_volatility",
        "atr",
        "atr_pct",
        "relative_volume",
        "volume_zscore",
        "z_score",
        "efficiency_ratio",
        "autocorrelation",
        "relative_strength",
        "spread_zscore",
        "candle_body_ratio",
        "candle_wick_skew",
        "trend_slope",
        "range_position",
        "high_low_range_pct",
        "open_close_return",
        "gap_return",
        "close_to_sma",
        "family_momentum",
        "cross_asset_return",
        "macro_surprise",
    }
)

_FORBIDDEN_FEATURE_KEY_TOKENS = frozenset(
    {
        "action",
        "intent",
        "quantity",
        "qty",
        "confidence",
        "decision",
        "trade",
        "fill",
        "pnl",
        "portfolio",
        "position",
        "holding",
        "equity",
        "cash",
        "risk",
        "scheduler",
        "schedule",
        "task",
        "wake",
        "watch",
        "execution",
        "broker",
        "order",
        "model",
    }
)


__all__ = [
    "ALLOWED_CATEGORICAL_FEATURES",
    "ALLOWED_NUMERIC_FEATURES",
    "DEFAULT_WORLD_HORIZONS",
    "FRESHNESS_STATUSES",
    "OUTCOME_EVENT_TYPES",
    "OUTCOME_STATUSES",
    "PREDICTION_CLASSES",
    "PREDICTION_STATUSES",
    "PREDICTION_TIERS",
    "AnchorBar",
    "Freshness",
    "OutcomeHorizon",
    "SamplingSlotCapture",
    "SamplingSlotCaptureKind",
    "WorldEpisode",
    "WorldObservation",
    "WorldOutcome",
    "WorldPrediction",
    "WORLD_EPISODE_SCHEMA_VERSION",
    "WORLD_OUTCOME_SCHEMA_VERSION",
    "WORLD_PREDICTION_SCHEMA_VERSION",
    "canonical_json",
    "canonical_payload",
    "canonical_prediction_class",
    "canonical_sha256",
    "move_class_from_simple_return",
    "parse_utc_timestamp",
    "validate_action_free_features",
    "world_episode_id",
    "world_outcome_event_id",
    "world_prediction_id",
    "completed_bar_cutoff",
    "bar_timestamp_on_canonical_grid",
    "is_eligible_completed_bar",
    "parse_bar_interval",
    "MARKET_FEATURE_CONTRACT_VERSION",
]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def canonical_prediction_class(value: Any) -> str:
    """Normalize live or legacy up/down/flat labels onto DOWN/FLAT/UP."""

    text = _required_text(value, "prediction class")
    mapped = _LEGACY_PREDICTION_CLASSES.get(text.lower(), text.upper())
    if mapped not in PREDICTION_CLASSES:
        allowed = ", ".join(PREDICTION_CLASSES)
        raise ValueError(f"prediction class must be one of: {allowed}")
    return mapped


def move_class_from_simple_return(
    value: float,
    *,
    band: float = SIMPLE_RETURN_DIRECTION_BAND,
) -> str:
    """Map a simple return onto the canonical 50bp DOWN/FLAT/UP contract."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("simple_return must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("simple_return must be finite")
    if number >= band:
        return "UP"
    if number <= -band:
        return "DOWN"
    return "FLAT"


def parse_utc_timestamp(value: datetime | str, field_name: str = "timestamp") -> datetime:
    """Parse an aware timestamp and normalize it to UTC.

    Naive timestamps are rejected: accepting the local process timezone would
    make an episode identity and its future horizon non-replayable.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_bar_interval(interval: str | None) -> timedelta | None:
    """Parse a compact bar interval such as ``15m``, ``1h``, or ``1d``."""

    if not isinstance(interval, str) or not interval.strip():
        return None
    match = _INTERVAL_PATTERN.fullmatch(interval.strip().lower())
    if match is None:
        return None
    count = float(match.group("count"))
    if not math.isfinite(count) or count <= 0.0:
        return None
    seconds = {"m": 60.0, "h": 3600.0, "d": 86400.0}[match.group("unit")] * count
    return timedelta(seconds=seconds)


def completed_bar_cutoff(
    *,
    as_of_bar_ts: datetime | str,
    timestamp_semantics: str | None,
    bar_interval: str | None,
) -> datetime | None:
    """Return the deterministic completed-bar clock, or None when it cannot be proven."""

    ts = parse_utc_timestamp(as_of_bar_ts, "as_of_bar_ts")
    semantics = str(timestamp_semantics or "").strip().lower()
    if semantics in _BAR_CLOSE_SEMANTICS:
        return ts
    if semantics in _BAR_START_SEMANTICS:
        duration = parse_bar_interval(bar_interval)
        if duration is None:
            return None
        return ts + duration
    return None


def bar_timestamp_on_canonical_grid(
    ts: datetime | str,
    bar_interval: str | None,
) -> bool:
    """True iff *ts* sits on the UTC unix-epoch grid of *bar_interval*."""

    try:
        parsed = parse_utc_timestamp(ts, "ts")
    except (TypeError, ValueError):
        return False
    duration = parse_bar_interval(bar_interval)
    if duration is None:
        return False
    interval_seconds = duration.total_seconds()
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        return False
    if abs(interval_seconds - round(interval_seconds)) > 1e-9:
        return False
    if parsed.microsecond != 0:
        return False
    unix = parsed.timestamp()
    if abs(unix - round(unix)) > 1e-9:
        return False
    return int(round(unix)) % int(round(interval_seconds)) == 0


def is_eligible_completed_bar(
    *,
    ts: datetime | str,
    bar_interval: str | None,
    timestamp_semantics: str | None = None,
    end_at: datetime | str | None = None,
    available_at: datetime | str | None = None,
) -> bool:
    """Return whether a provider row may be treated as a completed interval bar.

    Canonical grid alignment is required. Flat OHLC and zero volume are not
    disqualifiers: those can be a legitimate completed bar. A trailing
    quote-like row with an off-grid timestamp is not a completed bar.
    """

    try:
        parsed_ts = parse_utc_timestamp(ts, "ts")
    except (TypeError, ValueError):
        return False
    if not bar_timestamp_on_canonical_grid(parsed_ts, bar_interval):
        return False
    parsed_end = None
    if end_at is not None:
        try:
            parsed_end = parse_utc_timestamp(end_at, "end_at")
        except (TypeError, ValueError):
            return False
        if parsed_end < parsed_ts:
            return False
    cutoff = None
    try:
        cutoff = completed_bar_cutoff(
            as_of_bar_ts=parsed_ts,
            timestamp_semantics=timestamp_semantics,
            bar_interval=bar_interval,
        )
    except (TypeError, ValueError):
        return False
    if cutoff is not None and parsed_end is not None and parsed_end != cutoff:
        return False
    if cutoff is None and parsed_end is not None and not bar_timestamp_on_canonical_grid(parsed_end, bar_interval):
        return False
    bar_end = parsed_end if parsed_end is not None else cutoff
    if available_at is not None:
        try:
            parsed_available = parse_utc_timestamp(available_at, "available_at")
        except (TypeError, ValueError):
            return False
        if bar_end is not None and parsed_available < bar_end:
            return False
    return True


def _optional_utc_timestamp(value: datetime | str | None, field_name: str) -> datetime | None:
    return None if value is None else parse_utc_timestamp(value, field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _normalise_key(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return separated.strip().lower()


def _forbidden_feature_key(key: str) -> bool:
    # Some deliberately whitelisted market terms contain a control-looking
    # token in an unrelated semantic (for example ``range_position``).  The
    # exact V1 whitelist takes precedence; nested values are still inspected.
    if key in ALLOWED_CATEGORICAL_FEATURES or key in ALLOWED_NUMERIC_FEATURES:
        return False
    tokens = re.findall(r"[a-z0-9]+", _normalise_key(key))
    return any(token in _FORBIDDEN_FEATURE_KEY_TOKENS for token in tokens)


def validate_action_free_features(value: Any, field_name: str = "features") -> None:
    """Reject forbidden control keys at every nested level.

    Feature values are scalar in the concrete V1 contract, but recursive
    validation keeps a future caller from smuggling an action-bearing object
    through a nested Mapping or list before scalar validation reports it.
    """

    def visit(current: Any, path: str) -> None:
        if isinstance(current, Mapping):
            for raw_key, nested in current.items():
                if not isinstance(raw_key, str):
                    raise TypeError(f"{path} keys must be strings")
                if _forbidden_feature_key(raw_key):
                    raise ValueError(f"{path}.{raw_key} is forbidden in an action-free observation")
                visit(nested, f"{path}.{raw_key}")
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            for index, nested in enumerate(current):
                visit(nested, f"{path}[{index}]")

    visit(value, field_name)


def _immutable_categorical_features(value: Mapping[str, Any] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("categorical_features must be a mapping")
    validate_action_free_features(value, "categorical_features")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "categorical feature key")
        if key not in ALLOWED_CATEGORICAL_FEATURES:
            raise ValueError(f"categorical feature is not whitelisted: {key}")
        if not isinstance(raw_value, str):
            raise TypeError(f"categorical feature {key} must be a string")
        result[key] = _required_text(raw_value, f"categorical feature {key}")
    return MappingProxyType(dict(sorted(result.items())))


def _immutable_numeric_features(value: Mapping[str, Any] | None) -> Mapping[str, float]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("numeric_features must be a mapping")
    validate_action_free_features(value, "numeric_features")
    result: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "numeric feature key")
        if key not in ALLOWED_NUMERIC_FEATURES:
            raise ValueError(f"numeric feature is not whitelisted: {key}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TypeError(f"numeric feature {key} must be a finite number")
        number = float(raw_value)
        if not math.isfinite(number):
            raise ValueError(f"numeric feature {key} must be finite")
        result[key] = number
    return MappingProxyType(dict(sorted(result.items())))


def canonical_payload(value: Any) -> Any:
    """Return a JSON-compatible, deterministic deep projection of *value*."""

    if isinstance(value, datetime):
        return _iso(parse_utc_timestamp(value))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[raw_key] = canonical_payload(nested)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_payload(item) for item in value]
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not allow non-finite values")
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return canonical_payload(to_dict())
    raise TypeError(f"value is not canonical JSON compatible: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Encode canonical JSON with stable key order and no non-finite numbers."""

    return json.dumps(
        canonical_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 hex digest of :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_GRAPH_FEATURE_CONTRACT_VERSION = "market_ohlcv_graph.v3"


def world_episode_id(
    *,
    venue: str,
    symbol: str,
    bar_interval: str,
    as_of_bar_ts: datetime | str,
    feature_contract_version: str,
    sampling_policy_version: str,
    context_snapshot_id: str | None = None,
    graph_snapshot_id: str | None = None,
) -> str:
    """Return the deterministic identity of one action-independent sampling slot.

    ``context_snapshot_id`` is omitted from the V1 slot.  A V2 context episode
    includes the snapshot digest so two different proven contexts at the same
    market bar cannot collide, while a market-only observation keeps the
    historical identity byte-for-byte.  A V3 graph episode includes
    ``graph_snapshot_id`` the same way and must not change V1/V2 identities.
    """

    slot = {
        "venue": _required_text(venue, "venue"),
        "symbol": _required_text(symbol, "symbol"),
        "bar_interval": _required_text(bar_interval, "bar_interval"),
        "as_of_bar_ts": _iso(parse_utc_timestamp(as_of_bar_ts, "as_of_bar_ts")),
        "feature_contract_version": _required_text(feature_contract_version, "feature_contract_version"),
        "sampling_policy_version": _required_text(sampling_policy_version, "sampling_policy_version"),
    }
    if context_snapshot_id is not None:
        slot["context_snapshot_id"] = _required_text(context_snapshot_id, "context_snapshot_id")
    if graph_snapshot_id is not None:
        slot["graph_snapshot_id"] = _required_text(graph_snapshot_id, "graph_snapshot_id")
    return f"world-episode:v1:{canonical_sha256(slot)}"


class SamplingSlotCaptureKind(StrEnum):
    """Lifecycle of one sampling-slot capture at the application/domain boundary.

    The first durable WorldEpisode for a slot is the immutable observation.  A
    later poll of that same slot reuses it and must not mutate market evidence.
    Direct store append of the same identity with different content still fails
    closed.
    """

    MISSING = "missing"
    APPENDED = "appended"
    REUSED_CANONICAL = "reused_canonical"


@dataclass(frozen=True)
class SamplingSlotCapture:
    """Typed result for one sampling-slot persistence attempt.

    Application capture uses this instead of bool/string branching: either the
    slot is still empty, the first observation was appended, or a later poll
    resolved to that canonical episode.  The first durable WorldEpisode wins;
    a reused capture never carries a later-revised observation.
    """

    kind: SamplingSlotCaptureKind | str
    episode: WorldEpisode | None = None

    def __post_init__(self) -> None:
        kind = self.kind if isinstance(self.kind, SamplingSlotCaptureKind) else SamplingSlotCaptureKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if kind is SamplingSlotCaptureKind.MISSING:
            if self.episode is not None:
                raise ValueError("missing sampling slot capture cannot carry an episode")
            return
        episode = self.episode
        if episode is None:
            raise ValueError("durable sampling slot capture requires an episode")
        if isinstance(episode, WorldEpisode):
            return
        if isinstance(episode, Mapping):
            object.__setattr__(self, "episode", WorldEpisode.from_dict(episode))
            return
        raise TypeError("durable sampling slot capture requires a WorldEpisode")

    @classmethod
    def missing(cls) -> SamplingSlotCapture:
        return cls(kind=SamplingSlotCaptureKind.MISSING)

    @classmethod
    def appended(cls, episode: WorldEpisode) -> SamplingSlotCapture:
        return cls(kind=SamplingSlotCaptureKind.APPENDED, episode=episode)

    @classmethod
    def reused_canonical(cls, episode: WorldEpisode) -> SamplingSlotCapture:
        return cls(kind=SamplingSlotCaptureKind.REUSED_CANONICAL, episode=episode)


@dataclass(frozen=True)
class AnchorBar:
    """Immutable OHLCV evidence for the exact market anchor of an observation."""

    ts: datetime | str
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    timestamp_semantics: str = "bar_close"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", parse_utc_timestamp(self.ts, "anchor.ts"))
        object.__setattr__(self, "source", _required_text(self.source, "anchor.source"))
        semantics = _required_text(self.timestamp_semantics, "anchor.timestamp_semantics").lower()
        object.__setattr__(self, "timestamp_semantics", semantics)

        values: dict[str, float] = {}
        for name in ("open", "high", "low", "close", "volume"):
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise TypeError(f"anchor.{name} must be a finite number")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"anchor.{name} must be finite")
            values[name] = number
            object.__setattr__(self, name, number)

        if min(values["open"], values["high"], values["low"], values["close"]) <= 0.0:
            raise ValueError("anchor OHLC prices must be positive")
        if values["volume"] < 0.0:
            raise ValueError("anchor.volume must be non-negative")
        if values["high"] < values["low"]:
            raise ValueError("anchor.high must be >= anchor.low")
        if not values["low"] <= values["open"] <= values["high"]:
            raise ValueError("anchor.open must be within [low, high]")
        if not values["low"] <= values["close"] <= values["high"]:
            raise ValueError("anchor.close must be within [low, high]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": _iso(self.ts),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "source": self.source,
            "timestamp_semantics": self.timestamp_semantics,
        }


@dataclass(frozen=True)
class Freshness:
    """Freshness is evidence, not an inferred label from current state."""

    status: str
    data_age_minutes: float | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "freshness.status").lower()
        if status not in FRESHNESS_STATUSES:
            allowed = ", ".join(sorted(FRESHNESS_STATUSES))
            raise ValueError(f"freshness.status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        if self.data_age_minutes is not None:
            raw = self.data_age_minutes
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise TypeError("freshness.data_age_minutes must be a finite number")
            age = float(raw)
            if not math.isfinite(age) or age < 0.0:
                raise ValueError("freshness.data_age_minutes must be finite and non-negative")
            object.__setattr__(self, "data_age_minutes", age)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "data_age_minutes": self.data_age_minutes}


def _immutable_graph_features(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("graph_features must be a mapping")
    detached = dict(value)
    snapshot = detached.pop("snapshot", None)
    validate_action_free_features(detached, "graph_features")
    payload = canonical_payload(detached)
    if not isinstance(payload, dict):
        raise TypeError("graph_features must be a mapping")
    if snapshot is not None:
        payload["snapshot"] = canonical_payload(snapshot)
    return MappingProxyType(payload)


def _graph_snapshot_id(graph: Any, graph_features: Mapping[str, Any] | None) -> str | None:
    snapshot_id = getattr(graph, "snapshot_id", None)
    if isinstance(snapshot_id, str) and snapshot_id.strip():
        return snapshot_id.strip()
    if isinstance(graph, Mapping):
        raw = graph.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if isinstance(graph_features, Mapping):
        nested = graph_features.get("snapshot")
        if isinstance(nested, Mapping):
            raw = nested.get("snapshot_id")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raw = graph_features.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _freshness(value: Freshness | Mapping[str, Any] | str) -> Freshness:
    if isinstance(value, Freshness):
        return value
    if isinstance(value, Mapping):
        return Freshness(
            status=value.get("status", "unknown"),
            data_age_minutes=value.get("data_age_minutes"),
        )
    if isinstance(value, str):
        return Freshness(status=value)
    raise TypeError("freshness must be Freshness, a mapping, or a status string")


def _anchor(value: AnchorBar | Mapping[str, Any]) -> AnchorBar:
    if isinstance(value, AnchorBar):
        return value
    if isinstance(value, Mapping):
        return AnchorBar(
            ts=value.get("ts") or value.get("as_of") or value.get("as_of_bar_ts"),
            open=value.get("open"),
            high=value.get("high"),
            low=value.get("low"),
            close=value.get("close"),
            volume=value.get("volume"),
            source=value.get("source"),
            timestamp_semantics=value.get("timestamp_semantics", "bar_close"),
        )
    raise TypeError("anchor must be AnchorBar or a mapping")


@dataclass(frozen=True)
class WorldObservation:
    """One immutable, action-free market observation at a deterministic slot."""

    venue: str
    symbol: str
    bar_interval: str
    as_of_bar_ts: datetime | str
    feature_contract_version: str
    sampling_policy_version: str
    anchor: AnchorBar | Mapping[str, Any]
    available_at: datetime | str | None = None
    captured_at: datetime | str | None = None
    freshness: Freshness | Mapping[str, Any] | str = field(default_factory=lambda: Freshness(status="unknown"))
    categorical_features: Mapping[str, Any] | None = field(default_factory=dict)
    numeric_features: Mapping[str, Any] | None = field(default_factory=dict)
    context: Mapping[str, Any] | None = None
    graph: Mapping[str, Any] | None = None
    graph_features: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in (
            "venue",
            "symbol",
            "bar_interval",
            "feature_contract_version",
            "sampling_policy_version",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))

        as_of = parse_utc_timestamp(self.as_of_bar_ts, "as_of_bar_ts")
        anchor = _anchor(self.anchor)
        if anchor.ts != as_of:
            raise ValueError("anchor.ts must equal as_of_bar_ts")
        available_at = _optional_utc_timestamp(self.available_at, "available_at")
        captured_at = _optional_utc_timestamp(self.captured_at, "captured_at")
        if captured_at is not None and as_of > captured_at:
            raise ValueError("captured_at must not precede as_of_bar_ts")
        if available_at is not None and captured_at is not None and available_at > captured_at:
            raise ValueError("available_at must not follow captured_at")
        bar_end = completed_bar_cutoff(
            as_of_bar_ts=as_of,
            timestamp_semantics=anchor.timestamp_semantics,
            bar_interval=self.bar_interval,
        )
        if bar_end is not None and available_at is not None and available_at < bar_end:
            raise ValueError("available_at must not precede completed bar end")

        object.__setattr__(self, "as_of_bar_ts", as_of)
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "freshness", _freshness(self.freshness))
        object.__setattr__(self, "categorical_features", _immutable_categorical_features(self.categorical_features))
        object.__setattr__(self, "numeric_features", _immutable_numeric_features(self.numeric_features))
        from trader.domain.world_context import (
            CONTEXT_FEATURE_CONTRACT_VERSION,
            WorldContextSnapshot,
            freeze_context_mapping,
        )

        if self.context is None:
            object.__setattr__(self, "context", None)
        else:
            object.__setattr__(self, "context", freeze_context_mapping(self.context))
        from trader.domain.world_graph import WorldGraphSnapshot

        raw_graph = self.graph
        if raw_graph is None and isinstance(self.graph_features, Mapping):
            raw_graph = self.graph_features.get("snapshot")
        if raw_graph is None:
            object.__setattr__(self, "graph", None)
        else:
            object.__setattr__(self, "graph", WorldGraphSnapshot.from_mapping(raw_graph))
        object.__setattr__(self, "graph_features", _immutable_graph_features(self.graph_features))
        contract = self.feature_contract_version
        if contract == MARKET_FEATURE_CONTRACT_VERSION:
            if self.context is not None:
                raise ValueError("V1 observation must not carry context")
            if self.graph is not None or self.graph_features is not None:
                raise ValueError("V1 observation must not carry graph")
        elif contract == CONTEXT_FEATURE_CONTRACT_VERSION:
            if self.context is None:
                raise ValueError("V2 observation must carry context")
            if self.graph is not None or self.graph_features is not None:
                raise ValueError("V2 observation must not carry graph")
            snapshot = WorldContextSnapshot.from_mapping(self.context)
            if snapshot.instrument.entity_id != self.symbol:
                raise ValueError("context instrument must match observation symbol")
            if snapshot.feature_contract_version != contract:
                raise ValueError("context feature contract must match observation")
            if available_at is None:
                raise ValueError("V2 observation requires available_at to bound context cutoff")
            if snapshot.cutoff_at > available_at:
                raise ValueError("context cutoff must not follow observation available_at")
        elif contract == _GRAPH_FEATURE_CONTRACT_VERSION:
            if self.context is not None:
                raise ValueError("V3 observation must not carry V2 context")
            if self.graph is not None:
                if available_at is None:
                    raise ValueError("V3 observation requires available_at to bound graph cutoff")
                if self.graph.cutoff_at > available_at:
                    raise ValueError("graph cutoff must not follow observation available_at")
        elif self.context is not None:
            raise ValueError("unknown feature contract must not carry context")
        elif self.graph is not None or self.graph_features is not None:
            raise ValueError("unknown feature contract must not carry graph")

    @property
    def observed_at(self) -> datetime:
        """Alias that makes the point-in-time semantics explicit to callers."""

        return self.as_of_bar_ts

    @property
    def episode_id(self) -> str:
        context_snapshot_id = None
        if isinstance(self.context, Mapping):
            raw_id = self.context.get("context_id")
            if isinstance(raw_id, str) and raw_id.strip():
                context_snapshot_id = raw_id.strip()
        return world_episode_id(
            venue=self.venue,
            symbol=self.symbol,
            bar_interval=self.bar_interval,
            as_of_bar_ts=self.as_of_bar_ts,
            feature_contract_version=self.feature_contract_version,
            sampling_policy_version=self.sampling_policy_version,
            context_snapshot_id=context_snapshot_id,
            graph_snapshot_id=_graph_snapshot_id(self.graph, self.graph_features),
        )

    @property
    def feature_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    @property
    def training_ineligibility_reason(self) -> str | None:
        if self.available_at is None:
            return "missing_available_at"
        if self.captured_at is None:
            return "missing_captured_at"
        if self.freshness.status != "fresh":
            return f"freshness_{self.freshness.status}"
        if self.anchor.timestamp_semantics == "unknown":
            return "unknown_timestamp_semantics"
        if not self.categorical_features and not self.numeric_features:
            return "missing_features"
        return None

    @property
    def training_eligible(self) -> bool:
        return self.training_ineligibility_reason is None

    def slot_dict(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "bar_interval": self.bar_interval,
            "as_of_bar_ts": _iso(self.as_of_bar_ts) or "",
            "feature_contract_version": self.feature_contract_version,
            "sampling_policy_version": self.sampling_policy_version,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = {
            **self.slot_dict(),
            "anchor": self.anchor.to_dict(),
            "available_at": _iso(self.available_at),
            "captured_at": _iso(self.captured_at),
            "freshness": self.freshness.to_dict(),
            "categorical_features": dict(self.categorical_features),
            "numeric_features": dict(self.numeric_features),
        }
        if self.context is not None:
            payload["context"] = canonical_payload(self.context)
        if self.graph_features is not None or self.graph is not None:
            features = dict(self.graph_features or {})
            if self.graph is not None:
                features["snapshot"] = canonical_payload(self.graph.to_dict())
            payload["graph_features"] = features
        return payload

    def replay_payload(self) -> dict[str, Any]:
        """Return a detached canonical projection suitable for replay/storage."""

        payload = canonical_payload(self.to_dict())
        if not isinstance(payload, dict):  # pragma: no cover - defensive invariant
            raise TypeError("world observation replay payload must be an object")
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldObservation:
        if not isinstance(value, Mapping):
            raise TypeError("world observation payload must be a mapping")
        return cls(
            venue=value.get("venue"),
            symbol=value.get("symbol"),
            bar_interval=value.get("bar_interval"),
            as_of_bar_ts=value.get("as_of_bar_ts"),
            feature_contract_version=value.get("feature_contract_version"),
            sampling_policy_version=value.get("sampling_policy_version"),
            anchor=value.get("anchor"),
            available_at=value.get("available_at"),
            captured_at=value.get("captured_at"),
            freshness=value.get("freshness", "unknown"),
            categorical_features=value.get("categorical_features", {}),
            numeric_features=value.get("numeric_features", {}),
            context=value.get("context"),
            graph=value.get("graph"),
            graph_features=value.get("graph_features"),
        )


@dataclass(frozen=True)
class WorldEpisode:
    """The root record for a trainable or explicitly non-trainable market slot."""

    observation: WorldObservation | Mapping[str, Any]
    training_eligible: bool | None = None
    training_reason: str | None = None
    schema_version: str = WORLD_EPISODE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        observation = (
            self.observation
            if isinstance(self.observation, WorldObservation)
            else WorldObservation.from_dict(self.observation)
            if isinstance(self.observation, Mapping)
            else None
        )
        if observation is None:
            raise TypeError("observation must be a WorldObservation or mapping")
        object.__setattr__(self, "observation", observation)
        schema_version = _required_text(self.schema_version, "schema_version")
        object.__setattr__(self, "schema_version", schema_version)

        # Old payloads remain auditable, but their feature semantics have not
        # been accepted by this V1 contract.  They must never become a silent
        # source of training rows merely because their fields happen to look
        # complete.
        derived_reason = (
            "unsupported_schema_version"
            if schema_version != WORLD_EPISODE_SCHEMA_VERSION
            else observation.training_ineligibility_reason
        )
        requested = self.training_eligible
        if requested is not None and not isinstance(requested, bool):
            raise TypeError("training_eligible must be a bool or None")
        if requested is True and derived_reason is not None:
            raise ValueError(f"cannot mark incomplete observation trainable: {derived_reason}")
        eligible = derived_reason is None if requested is None else requested
        reason = self.training_reason
        if reason is not None:
            reason = _required_text(reason, "training_reason")
        if eligible:
            if reason is not None:
                raise ValueError("training_reason must be None when training_eligible is true")
        else:
            reason = reason or derived_reason or "explicitly_ineligible"
        object.__setattr__(self, "training_eligible", eligible)
        object.__setattr__(self, "training_reason", reason)

    @property
    def episode_id(self) -> str:
        return self.observation.episode_id

    @property
    def payload_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": "episode_created",
            "episode_id": self.episode_id,
            "observation": self.observation.to_dict(),
            "training_eligible": self.training_eligible,
            "training_reason": self.training_reason,
        }

    def replay_payload(self) -> dict[str, Any]:
        payload = canonical_payload(self.to_dict())
        if not isinstance(payload, dict):  # pragma: no cover - defensive invariant
            raise TypeError("world episode replay payload must be an object")
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldEpisode:
        if not isinstance(value, Mapping):
            raise TypeError("world episode payload must be a mapping")
        episode = cls(
            observation=value.get("observation"),
            training_eligible=value.get("training_eligible"),
            training_reason=value.get("training_reason"),
            # A payload without an explicit schema is legacy/audit-only.  Do
            # not reinterpret it as a current training record on replay.
            schema_version=value.get("schema_version", "legacy.missing_schema_version"),
        )
        persisted_id = value.get("episode_id")
        if persisted_id is not None and _required_text(persisted_id, "episode_id") != episode.episode_id:
            raise ValueError("episode_id does not match the deterministic observation slot")
        return episode


@dataclass(frozen=True)
class OutcomeHorizon:
    """A fixed, explicitly named market horizon; it never falls back silently."""

    horizon_id: str
    duration_seconds: int
    endpoint_rule: str = "first_completed_1h_bar_at_or_after_target"
    max_lateness_seconds: int = 3600

    def __post_init__(self) -> None:
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))
        object.__setattr__(self, "endpoint_rule", _required_text(self.endpoint_rule, "endpoint_rule"))
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, int):
            raise TypeError("duration_seconds must be an integer")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if isinstance(self.max_lateness_seconds, bool) or not isinstance(self.max_lateness_seconds, int):
            raise TypeError("max_lateness_seconds must be an integer")
        if self.max_lateness_seconds < 0:
            raise ValueError("max_lateness_seconds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_id": self.horizon_id,
            "duration_seconds": self.duration_seconds,
            "endpoint_rule": self.endpoint_rule,
            "max_lateness_seconds": self.max_lateness_seconds,
        }


DEFAULT_WORLD_HORIZONS = (
    OutcomeHorizon("elapsed_4h.v1", 4 * 60 * 60),
    OutcomeHorizon("elapsed_1d.v1", 24 * 60 * 60),
)


def _finite_optional(value: float | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first_present(*containers: Any, names: tuple[str, ...]) -> Any:
    """Return the first non-null field without inventing a missing value."""

    for container in containers:
        mapping = _mapping_or_none(container)
        if mapping is None:
            continue
        for name in names:
            if name in mapping and mapping[name] is not None:
                return mapping[name]
    return None


def _nested_bar(*containers: Any, names: tuple[str, ...]) -> Mapping[str, Any]:
    for container in containers:
        mapping = _mapping_or_none(container)
        if mapping is None:
            continue
        for name in names:
            nested = mapping.get(name)
            if isinstance(nested, Mapping):
                return nested
    return {}


def world_outcome_event_id(
    *,
    episode_id: str,
    horizon_id: str,
    status: str,
    source_raw_sha256: str | None = None,
    supersedes_event_id: str | None = None,
) -> str:
    """Return an idempotent ID for one immutable outcome event."""

    identity = {
        "episode_id": _required_text(episode_id, "episode_id"),
        "horizon_id": _required_text(horizon_id, "horizon_id"),
        "status": _required_text(status, "status").lower(),
        "source_raw_sha256": None
        if source_raw_sha256 is None
        else _required_text(source_raw_sha256, "source_raw_sha256"),
        "supersedes_event_id": None
        if supersedes_event_id is None
        else _required_text(supersedes_event_id, "supersedes_event_id"),
    }
    return f"world-outcome:v1:{canonical_sha256(identity)}"


@dataclass(frozen=True)
class WorldOutcome:
    """An exogenous market outcome for one fixed horizon of a world episode."""

    episode_id: str
    horizon: OutcomeHorizon | Mapping[str, Any]
    status: str
    target_at: datetime | str
    available_at: datetime | str | None = None
    computed_at: datetime | str | None = None
    anchor_close: float | None = None
    endpoint_close: float | None = None
    endpoint_bar_ts: datetime | str | None = None
    source: str | None = None
    source_raw_sha256: str | None = None
    reason: str | None = None
    event_type: str | None = None
    supersedes_event_id: str | None = None
    training_eligible: bool | None = None
    direction: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _required_text(self.episode_id, "episode_id"))
        horizon = self.horizon
        if isinstance(horizon, Mapping):
            horizon = OutcomeHorizon(
                horizon_id=horizon.get("horizon_id"),
                duration_seconds=horizon.get("duration_seconds"),
                endpoint_rule=horizon.get("endpoint_rule", "first_completed_1h_bar_at_or_after_target"),
                max_lateness_seconds=horizon.get("max_lateness_seconds", 3600),
            )
        if not isinstance(horizon, OutcomeHorizon):
            raise TypeError("horizon must be an OutcomeHorizon or mapping")
        object.__setattr__(self, "horizon", horizon)

        status = _required_text(self.status, "status").lower()
        if status not in OUTCOME_STATUSES:
            allowed = ", ".join(sorted(OUTCOME_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "target_at", parse_utc_timestamp(self.target_at, "target_at"))
        object.__setattr__(self, "available_at", _optional_utc_timestamp(self.available_at, "available_at"))
        object.__setattr__(self, "computed_at", _optional_utc_timestamp(self.computed_at, "computed_at"))
        object.__setattr__(self, "endpoint_bar_ts", _optional_utc_timestamp(self.endpoint_bar_ts, "endpoint_bar_ts"))
        if self.endpoint_bar_ts is not None and self.endpoint_bar_ts < self.target_at:
            raise ValueError("endpoint_bar_ts must not precede target_at")
        if (
            self.available_at is not None
            and self.endpoint_bar_ts is not None
            and self.available_at < self.endpoint_bar_ts
        ):
            raise ValueError("available_at must not precede endpoint_bar_ts")
        if (
            self.computed_at is not None
            and self.available_at is not None
            and self.computed_at < self.available_at
        ):
            raise ValueError("computed_at must not precede available_at")
        object.__setattr__(self, "anchor_close", _finite_optional(self.anchor_close, "anchor_close"))
        object.__setattr__(self, "endpoint_close", _finite_optional(self.endpoint_close, "endpoint_close"))
        if self.source is not None:
            object.__setattr__(self, "source", _required_text(self.source, "source"))
        if self.source_raw_sha256 is not None:
            object.__setattr__(
                self,
                "source_raw_sha256",
                _required_text(self.source_raw_sha256, "source_raw_sha256"),
            )
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if self.supersedes_event_id is not None:
            object.__setattr__(
                self,
                "supersedes_event_id",
                _required_text(self.supersedes_event_id, "supersedes_event_id"),
            )

        default_event_type = (
            "outcome_observed"
            if status == "observed"
            else "outcome_scheduled"
            if status == "pending"
            else "outcome_unavailable"
        )
        event_type = _required_text(self.event_type or default_event_type, "event_type")
        if event_type not in OUTCOME_EVENT_TYPES:
            allowed = ", ".join(sorted(OUTCOME_EVENT_TYPES))
            raise ValueError(f"event_type must be one of: {allowed}")
        object.__setattr__(self, "event_type", event_type)

        observed_fields = (self.anchor_close, self.endpoint_close, self.endpoint_bar_ts, self.source)
        if status == "observed" and any(value is None for value in observed_fields):
            raise ValueError("observed outcome requires anchor/end prices, endpoint timestamp, and source")
        if self.anchor_close is not None and self.anchor_close <= 0.0:
            raise ValueError("anchor_close must be positive")
        if self.endpoint_close is not None and self.endpoint_close <= 0.0:
            raise ValueError("endpoint_close must be positive")

        derived_direction = None
        if self.anchor_close is not None and self.endpoint_close is not None:
            derived_direction = move_class_from_simple_return(self.endpoint_close / self.anchor_close - 1.0)
        if self.direction is not None:
            normalized_direction = canonical_prediction_class(self.direction)
            if derived_direction is not None and normalized_direction != derived_direction:
                raise ValueError("direction does not match simple_return")
            object.__setattr__(self, "direction", normalized_direction)
        else:
            object.__setattr__(self, "direction", derived_direction)

        requested_eligible = self.training_eligible
        if requested_eligible is not None and not isinstance(requested_eligible, bool):
            raise TypeError("training_eligible must be a bool or None")
        observed_eligible = status == "observed" and self.source_raw_sha256 is not None
        causal_complete = (
            observed_eligible
            and self.endpoint_bar_ts is not None
            and self.available_at is not None
            and self.computed_at is not None
        )
        if requested_eligible is True:
            if not observed_eligible:
                raise ValueError("only an observed outcome with immutable source evidence is trainable")
            if not causal_complete:
                raise ValueError("trainable outcome requires proven causal timings")
        object.__setattr__(
            self,
            "training_eligible",
            causal_complete if requested_eligible is None else requested_eligible,
        )

    @property
    def simple_return(self) -> float | None:
        if self.anchor_close is None or self.endpoint_close is None:
            return None
        return self.endpoint_close / self.anchor_close - 1.0

    @property
    def event_id(self) -> str:
        return world_outcome_event_id(
            episode_id=self.episode_id,
            horizon_id=self.horizon.horizon_id,
            status=self.status,
            source_raw_sha256=self.source_raw_sha256,
            supersedes_event_id=self.supersedes_event_id,
        )

    @property
    def payload_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORLD_OUTCOME_SCHEMA_VERSION,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "episode_id": self.episode_id,
            "horizon_id": self.horizon.horizon_id,
            "horizon": self.horizon.to_dict(),
            "status": self.status,
            "target_at": _iso(self.target_at),
            "available_at": _iso(self.available_at),
            "computed_at": _iso(self.computed_at),
            "anchor_close": self.anchor_close,
            "endpoint_close": self.endpoint_close,
            "simple_return": self.simple_return,
            "direction": self.direction,
            "move_class": self.direction,
            "endpoint_bar_ts": _iso(self.endpoint_bar_ts),
            "source": self.source,
            "source_raw_sha256": self.source_raw_sha256,
            "reason": self.reason,
            "supersedes_event_id": self.supersedes_event_id,
            "training_eligible": self.training_eligible,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldOutcome:
        if not isinstance(value, Mapping):
            raise TypeError("world outcome payload must be a mapping")
        label = _mapping_or_none(value.get("label")) or {}
        evidence = _mapping_or_none(value.get("evidence")) or {}
        nested_horizon = _mapping_or_none(value.get("horizon")) or _mapping_or_none(label.get("horizon")) or {}
        target_bar = _nested_bar(
            value,
            label,
            evidence,
            names=("target_bar", "endpoint_bar"),
        )
        anchor_bar = _nested_bar(value, label, evidence, names=("anchor_bar",))
        horizon = value.get("horizon")
        if horizon is None:
            horizon = label.get("horizon")
        if not isinstance(horizon, (OutcomeHorizon, Mapping)):
            horizon = None
        if horizon is None:
            horizon_id = _first_present(
                value,
                label,
                nested_horizon,
                names=("horizon_id", "horizon_code"),
            )
            known = {item.horizon_id: item for item in DEFAULT_WORLD_HORIZONS}
            match = known.get(str(horizon_id or "").strip())
            if match is not None:
                horizon = match
            elif horizon_id is not None:
                duration_seconds = _first_present(
                    nested_horizon,
                    value,
                    label,
                    names=("duration_seconds",),
                )
                if duration_seconds is None:
                    raise ValueError("horizon duration_seconds is required")
                horizon = {
                    "horizon_id": horizon_id,
                    "duration_seconds": duration_seconds,
                    "endpoint_rule": _first_present(
                        nested_horizon,
                        value,
                        label,
                        names=("endpoint_rule",),
                    )
                    or "first_completed_1h_bar_at_or_after_target",
                    "max_lateness_seconds": _first_present(
                        nested_horizon,
                        value,
                        label,
                        names=("max_lateness_seconds",),
                    )
                    or 3600,
                }
        if "training_eligible" in value:
            training_eligible = value.get("training_eligible")
        elif "training_eligible" in label:
            training_eligible = label.get("training_eligible")
        else:
            training_eligible = None
        return cls(
            episode_id=_first_present(value, label, names=("episode_id",)),
            horizon=horizon,
            status=_first_present(value, label, names=("status",)),
            target_at=_first_present(value, label, evidence, names=("target_at",)),
            available_at=_first_present(
                value,
                label,
                evidence,
                names=("available_at", "label_available_at"),
            ),
            computed_at=_first_present(
                value,
                label,
                evidence,
                names=("computed_at", "sealed_at"),
            ),
            anchor_close=_first_present(
                value,
                label,
                evidence,
                names=("anchor_close",),
            )
            or anchor_bar.get("close"),
            endpoint_close=_first_present(
                value,
                label,
                evidence,
                names=("endpoint_close",),
            )
            or target_bar.get("close"),
            endpoint_bar_ts=_first_present(
                value,
                label,
                evidence,
                names=("endpoint_bar_ts",),
            )
            or target_bar.get("bar_end_at")
            or target_bar.get("end_at")
            or target_bar.get("ts"),
            source=_first_present(value, label, evidence, names=("source",)) or target_bar.get("source"),
            source_raw_sha256=_first_present(
                value,
                label,
                evidence,
                names=("source_raw_sha256",),
            )
            or target_bar.get("fingerprint")
            or target_bar.get("source_raw_sha256"),
            reason=_first_present(value, label, names=("reason",)),
            event_type=_first_present(value, label, names=("event_type",)),
            supersedes_event_id=_first_present(
                value,
                label,
                names=("supersedes_event_id", "supersedes_outcome_event_id"),
            ),
            training_eligible=training_eligible,
            direction=_first_present(
                value,
                label,
                names=("direction", "move_class"),
            ),
        )


def world_prediction_id(
    *,
    episode_id: str,
    horizon_id: str,
    model_id: str,
    model_version: str,
    feature_hash: str,
    model_fingerprint: str | None = None,
    training_cutoff: datetime | str | None = None,
    comparison_batch_id: str | None = None,
    comparison_cohort_fingerprint: str | None = None,
) -> str:
    """Return a deterministic ID for a prediction over an immutable feature view."""

    identity = {
        "episode_id": _required_text(episode_id, "episode_id"),
        "horizon_id": _required_text(horizon_id, "horizon_id"),
        "model_id": _required_text(model_id, "model_id"),
        "model_version": _required_text(model_version, "model_version"),
        "feature_hash": _required_text(feature_hash, "feature_hash"),
        "model_fingerprint": None
        if model_fingerprint is None
        else _required_text(model_fingerprint, "model_fingerprint"),
        "training_cutoff": None
        if training_cutoff is None
        else _iso(parse_utc_timestamp(training_cutoff, "training_cutoff")),
    }
    if comparison_batch_id is not None:
        identity["comparison_batch_id"] = _required_text(comparison_batch_id, "comparison_batch_id")
    if comparison_cohort_fingerprint is not None:
        identity["comparison_cohort_fingerprint"] = _required_text(
            comparison_cohort_fingerprint, "comparison_cohort_fingerprint"
        )
    return f"world-prediction:v1:{canonical_sha256(identity)}"


def _probability_mapping(value: Mapping[str, Any] | None) -> Mapping[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("probabilities must be a mapping keyed by DOWN, FLAT, and UP")
    if set(value) != set(PREDICTION_CLASSES):
        raise ValueError("probabilities must contain exactly DOWN, FLAT, and UP")
    normalized: dict[str, float] = {}
    for label in PREDICTION_CLASSES:
        raw = value[label]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError(f"probability {label} must be a finite number")
        probability = float(raw)
        if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise ValueError(f"probability {label} must be in [0, 1]")
        normalized[label] = probability
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-9):
        raise ValueError("probabilities must sum to 1")
    return MappingProxyType(normalized)


def _default_predicted_class(probabilities: Mapping[str, float]) -> str:
    highest = max(probabilities.values())
    tied = [label for label in PREDICTION_CLASSES if probabilities[label] == highest]
    return "FLAT" if "FLAT" in tied else tied[0]


@dataclass(frozen=True)
class WorldPrediction:
    """A three-class, shadow-only prediction over an immutable feature view."""

    episode_id: str
    horizon_id: str
    model_id: str
    model_version: str
    feature_hash: str
    created_at: datetime | str
    predicted_return: float | None = None
    probabilities: Mapping[str, Any] | None = None
    predicted_class: str | None = None
    status: str = "warming_up"
    tier: str = "uniform"
    support: int = 0
    exact_support: int = 0
    coarse_support: int = 0
    global_support: int = 0
    training_cutoff: datetime | str | None = None
    model_fingerprint: str | None = None
    comparison_batch_id: str | None = None
    comparison_cohort_fingerprint: str | None = None
    recommendation: str = "NO_GO"
    authority: str = "shadow_only"
    decision_effect: str = "none"

    def __post_init__(self) -> None:
        for name in ("episode_id", "horizon_id", "model_id", "model_version", "feature_hash"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        predicted_return = _finite_optional(self.predicted_return, "predicted_return")
        object.__setattr__(self, "predicted_return", predicted_return)
        object.__setattr__(self, "created_at", parse_utc_timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "training_cutoff", _optional_utc_timestamp(self.training_cutoff, "training_cutoff"))
        if self.model_fingerprint is not None:
            object.__setattr__(
                self,
                "model_fingerprint",
                _required_text(self.model_fingerprint, "model_fingerprint"),
            )
        if self.comparison_batch_id is not None:
            object.__setattr__(
                self,
                "comparison_batch_id",
                _required_text(self.comparison_batch_id, "comparison_batch_id"),
            )
        if self.comparison_cohort_fingerprint is not None:
            object.__setattr__(
                self,
                "comparison_cohort_fingerprint",
                _required_text(self.comparison_cohort_fingerprint, "comparison_cohort_fingerprint"),
            )

        status = _required_text(self.status, "status").lower()
        if status not in PREDICTION_STATUSES:
            allowed = ", ".join(sorted(PREDICTION_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        tier = _required_text(self.tier, "tier").lower()
        if tier not in PREDICTION_TIERS:
            allowed = ", ".join(sorted(PREDICTION_TIERS))
            raise ValueError(f"tier must be one of: {allowed}")
        object.__setattr__(self, "tier", tier)
        for name in ("support", "exact_support", "coarse_support", "global_support"):
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise TypeError(f"{name} must be a non-negative integer")
            if raw < 0:
                raise ValueError(f"{name} must be a non-negative integer")

        probabilities = _probability_mapping(self.probabilities)
        if probabilities is None and status == "shadow_only":
            raise ValueError("shadow_only prediction requires DOWN/FLAT/UP probabilities")
        object.__setattr__(self, "probabilities", probabilities)
        predicted_class = self.predicted_class
        if predicted_class is not None:
            predicted_class = _required_text(predicted_class, "predicted_class").upper()
            if predicted_class not in PREDICTION_CLASSES:
                allowed = ", ".join(PREDICTION_CLASSES)
                raise ValueError(f"predicted_class must be one of: {allowed}")
        elif probabilities is not None:
            predicted_class = _default_predicted_class(probabilities)
        if probabilities is not None and predicted_class is not None:
            if probabilities[predicted_class] != max(probabilities.values()):
                raise ValueError("predicted_class must be a highest-probability class")
        object.__setattr__(self, "predicted_class", predicted_class)

        if self.recommendation != "NO_GO":
            raise ValueError("recommendation must remain NO_GO for a shadow prediction")
        if self.authority != "shadow_only":
            raise ValueError("authority must remain shadow_only")
        if self.decision_effect != "none":
            raise ValueError("decision_effect must remain none")

    @property
    def prediction_id(self) -> str:
        return world_prediction_id(
            episode_id=self.episode_id,
            horizon_id=self.horizon_id,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_hash=self.feature_hash,
            model_fingerprint=self.model_fingerprint,
            training_cutoff=self.training_cutoff,
            comparison_batch_id=self.comparison_batch_id,
            comparison_cohort_fingerprint=self.comparison_cohort_fingerprint,
        )

    @property
    def payload_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORLD_PREDICTION_SCHEMA_VERSION,
            "prediction_id": self.prediction_id,
            "episode_id": self.episode_id,
            "horizon_id": self.horizon_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_hash": self.feature_hash,
            "predicted_return": self.predicted_return,
            "created_at": _iso(self.created_at),
            "probabilities": None if self.probabilities is None else dict(self.probabilities),
            "predicted_class": self.predicted_class,
            "status": self.status,
            "tier": self.tier,
            "support": self.support,
            "exact_support": self.exact_support,
            "coarse_support": self.coarse_support,
            "global_support": self.global_support,
            "training_cutoff": _iso(self.training_cutoff),
            "model_fingerprint": self.model_fingerprint,
            "comparison_batch_id": self.comparison_batch_id,
            "comparison_cohort_fingerprint": self.comparison_cohort_fingerprint,
            "recommendation": self.recommendation,
            "authority": self.authority,
            "decision_effect": self.decision_effect,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldPrediction:
        if not isinstance(value, Mapping):
            raise TypeError("world prediction payload must be a mapping")
        return cls(
            episode_id=value.get("episode_id"),
            horizon_id=value.get("horizon_id") or value.get("horizon_code"),
            model_id=value.get("model_id") or value.get("model_kind"),
            model_version=value.get("model_version"),
            feature_hash=value.get("feature_hash") or "",
            created_at=value.get("created_at") or value.get("predicted_at"),
            predicted_return=value.get("predicted_return"),
            probabilities=value.get("probabilities"),
            predicted_class=value.get("predicted_class"),
            status=value.get("status", "warming_up"),
            tier=value.get("tier", "uniform"),
            support=int(value.get("support") or 0),
            exact_support=int(value.get("exact_support") or 0),
            coarse_support=int(value.get("coarse_support") or 0),
            global_support=int(value.get("global_support") or 0),
            training_cutoff=value.get("training_cutoff"),
            model_fingerprint=value.get("model_fingerprint"),
            comparison_batch_id=value.get("comparison_batch_id"),
            comparison_cohort_fingerprint=value.get("comparison_cohort_fingerprint"),
            recommendation=value.get("recommendation", "NO_GO"),
            authority=value.get("authority", "shadow_only"),
            decision_effect=value.get("decision_effect", "none"),
        )
