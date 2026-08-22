"""Shared action-free feature encoding, errors, and model-update DTOs.

This module is the single encoding contract used by the Markov baseline and
the online GRU.  Neither model may import the other's internals; both consume
this allow-listed, point-in-time projection and the same DOWN/FLAT/UP move
classes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType

from trader.domain.world_episode import PREDICTION_CLASSES, WorldOutcome


OUTCOME_CLASSES: tuple[str, str, str] = PREDICTION_CLASSES
DIRECTION_BAND = 0.005

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
        "family_regime",
        "trend",
        "macro_regime",
        "geopolitical_risk_bucket",
        "data_freshness",
        "data_age_bucket",
        "source_status",
        "observation_quality",
    }
)

ALLOWED_NUMERIC_FEATURES = frozenset(
    {
        "return",
        "atr_pct",
        "range_position",
        "z_score",
        "relative_volume",
        "efficiency_ratio",
        "trend_slope",
        "volatility",
        "ohlc_volatility",
        "realized_volatility",
        "family_consensus",
        "data_age_minutes",
        "open_close_return",
        "high_low_range_pct",
    }
)

_CATEGORICAL_ALIASES = {
    "family": "asset_family",
    "market_family": "asset_family",
    "interval": "bar_interval",
    "session": "session_phase",
    "regime": "market_regime",
    "vol_state": "volatility_state",
    "market_return_bucket": "return_bucket",
    "market_atr_bucket": "atr_bucket",
    "range_bucket": "range_position_bucket",
    "family_consensus": "family_consensus_bucket",
    "freshness": "data_freshness",
    "data_source_status": "source_status",
}

_NUMERIC_ALIASES = {
    "market_return": "return",
    "return_1": "return",
    "momentum": "return",
    "atr": "atr_pct",
    "relative_vol": "relative_volume",
    "rvol": "relative_volume",
    "zscore": "z_score",
    "er": "efficiency_ratio",
    "family_consensus_fraction": "family_consensus",
}

_NUMERIC_BUCKETS: dict[str, tuple[str, tuple[float, ...]]] = {
    "return": ("return_bucket", (-0.05, -0.015, -0.003, 0.003, 0.015, 0.05)),
    "atr_pct": ("atr_bucket", (0.003, 0.008, 0.02, 0.05)),
    "range_position": ("range_position_bucket", (0.2, 0.4, 0.6, 0.8)),
    "z_score": ("z_score_bucket", (-2.0, -1.0, 1.0, 2.0)),
    "relative_volume": ("relative_volume_bucket", (0.5, 0.8, 1.2, 2.0)),
    "efficiency_ratio": ("efficiency_ratio_bucket", (0.2, 0.45, 0.7)),
    "trend_slope": ("trend_slope_bucket", (-0.01, -0.002, 0.002, 0.01)),
    "volatility": ("volatility_bucket", (0.005, 0.015, 0.03, 0.06)),
    "ohlc_volatility": ("ohlc_volatility_bucket", (0.005, 0.015, 0.03, 0.06)),
    "realized_volatility": ("realized_volatility_bucket", (0.005, 0.015, 0.03, 0.06)),
    "family_consensus": ("family_consensus_bucket", (0.5, 0.67, 0.84)),
    "data_age_minutes": ("data_age_bucket", (5.0, 15.0, 60.0, 240.0)),
    "open_close_return": ("open_close_return_bucket", (-0.05, -0.015, -0.003, 0.003, 0.015, 0.05)),
    "high_low_range_pct": ("high_low_range_pct_bucket", (0.005, 0.015, 0.03, 0.06)),
}

_COARSE_FEATURES = frozenset(
    {
        "session_phase",
        "market_regime",
        "volatility_state",
        "momentum_bucket",
        "return_bucket",
        "atr_bucket",
        "range_position_bucket",
        "family_direction",
        "family_consensus_bucket",
        "z_score_bucket",
        "relative_volume_bucket",
        "efficiency_ratio_bucket",
        "trend_slope_bucket",
        "volatility_bucket",
        "ohlc_volatility_bucket",
        "realized_volatility_bucket",
        "open_close_return_bucket",
        "high_low_range_pct_bucket",
    }
)

_FORBIDDEN_EXACT = frozenset(
    {
        "action",
        "intent",
        "decision",
        "decision_id",
        "quantity",
        "qty",
        "executed",
        "execution",
        "fill",
        "fills",
        "portfolio",
        "position",
        "positions",
        "pnl",
        "profit_loss",
        "risk",
        "risk_gate",
        "prompt",
        "tool",
        "tools",
        "llm",
        "model",
        "scheduler",
        "schedule",
        "task",
        "wake",
        "broker",
        "order",
        "trade",
        "reward",
        "outcome",
        "label",
        "target",
        "forward_return",
        "future_return",
        "realized_pnl",
        "realised_pnl",
    }
)
_FORBIDDEN_FRAGMENTS = (
    "action",
    "decision",
    "quantity",
    "_qty",
    "execut",
    "fill",
    "portfolio",
    "position",
    "pnl",
    "risk",
    "prompt",
    "tool",
    "llm",
    "scheduler",
    "schedule",
    "broker",
    "order",
    "trade",
    "rationale",
    "hypothesis",
    "confidence",
    "reason",
    "reward",
    "outcome",
    "forward_return",
    "future",
    "target_",
    "mandate",
    "memory",
    "memrl",
    "macro_event_distance",
)

_STRUCTURAL_KEYS = frozenset(
    {
        "episode_id",
        "symbol",
        "as_of_bar_ts",
        "available_at",
        "captured_at",
        "observed_at",
        "feature_contract_version",
        "sampling_policy_version",
        "anchor",
        "freshness",
        "horizon",
        "horizon_id",
        "horizon_code",
        "training_eligible",
        "training_reason",
    }
)

_MISSING = object()
_StateKey = tuple[tuple[str, str], ...]
_LEGACY_MOVE_CLASS = {"up": "UP", "down": "DOWN", "flat": "FLAT"}


def _feature_contract_fingerprint() -> str:
    payload = {
        "categorical": sorted(ALLOWED_CATEGORICAL_FEATURES),
        "numeric": sorted(ALLOWED_NUMERIC_FEATURES),
        "numeric_buckets": {
            key: {"bucket_key": bucket_key, "thresholds": thresholds}
            for key, (bucket_key, thresholds) in sorted(_NUMERIC_BUCKETS.items())
        },
        "coarse_features": sorted(_COARSE_FEATURES),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


FEATURE_CONTRACT_FINGERPRINT = _feature_contract_fingerprint()


class FeatureBoundaryError(ValueError):
    """A control, critic, target, or non-exogenous feature was supplied."""


class FutureLabelLeakageError(ValueError):
    """A historical prediction would consume a label available at/after T0."""


class OutcomeEventConflictError(ValueError):
    """An outcome event id was replayed with different training content."""


@dataclass(frozen=True)
class FeatureState:
    """Frozen canonical feature view used by World models, never the input map."""

    exact_state: _StateKey
    coarse_state: _StateKey
    features: Mapping[str, str]
    feature_hash: str

    def as_dict(self) -> dict[str, str]:
        return dict(self.features)


@dataclass(frozen=True)
class ModelUpdate:
    """Result of one attempted incremental, outcome-driven update."""

    applied: bool
    reason: str
    outcome_event_id: str | None
    horizon_id: str | None
    global_support: int
    training_cutoff: str | None
    model_fingerprint: str | None


def _normalise_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _normalise_scalar(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        normalised = value.strip().lower()
        if not normalised:
            return "unknown"
        if len(normalised) > 80:
            raise FeatureBoundaryError("categorical feature value is too long for a compact World state")
        return normalised
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise FeatureBoundaryError("categorical feature value must be finite")
        return format(numeric, ".12g")
    raise FeatureBoundaryError("categorical World features must be scalar, not structured prompt/context data")


def _is_forbidden_key(key: str) -> bool:
    known_feature_keys = (
        ALLOWED_CATEGORICAL_FEATURES
        | ALLOWED_NUMERIC_FEATURES
        | frozenset(_CATEGORICAL_ALIASES)
        | frozenset(_NUMERIC_ALIASES)
        | frozenset(bucket_key for bucket_key, _thresholds in _NUMERIC_BUCKETS.values())
    )
    if key in known_feature_keys:
        return False
    if key in _FORBIDDEN_EXACT:
        return True
    return any(fragment in key for fragment in _FORBIDDEN_FRAGMENTS)


def _assert_no_forbidden_keys(values: Mapping[object, object], *, path: str = "features") -> None:
    for raw_key, value in values.items():
        key = _normalise_key(raw_key)
        if _is_forbidden_key(key):
            raise FeatureBoundaryError(f"forbidden World feature at {path}.{key}")
        if isinstance(value, Mapping):
            _assert_no_forbidden_keys(value, path=f"{path}.{key}")
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, Mapping):
                    _assert_no_forbidden_keys(item, path=f"{path}.{key}")


def _read_field(source: object, *names: str) -> object:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
    for name in names:
        value = getattr(source, name, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _mapping_or_empty(value: object, *, name: str) -> Mapping[object, object]:
    if value is _MISSING or value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FeatureBoundaryError(f"{name} must be a mapping")
    return value


def _observation_context_features(source: object) -> tuple[dict[str, object], dict[str, object]]:
    categorical: dict[str, object] = {}
    numeric: dict[str, object] = {}
    for name in ("venue", "bar_interval"):
        value = _read_field(source, name)
        if value is not _MISSING and value is not None:
            categorical[name] = value
    freshness = _read_field(source, "freshness")
    if freshness is not _MISSING and freshness is not None:
        status = _read_field(freshness, "status")
        if status is not _MISSING and status is not None:
            categorical["data_freshness"] = status
        age = _read_field(freshness, "data_age_minutes")
        if age is not _MISSING and age is not None:
            numeric["data_age_minutes"] = age
    return categorical, numeric


def _feature_maps(observation: object) -> tuple[Mapping[object, object], Mapping[object, object]]:
    source = observation
    categories = _read_field(source, "categorical_features")
    numerics = _read_field(source, "numeric_features")
    if categories is _MISSING and numerics is _MISSING:
        nested_observation = _read_field(source, "observation")
        if nested_observation is not _MISSING and nested_observation is not None:
            source = nested_observation
            categories = _read_field(source, "categorical_features")
            numerics = _read_field(source, "numeric_features")

    if categories is _MISSING and numerics is _MISSING:
        nested_features = _read_field(source, "world_features", "features")
        if isinstance(nested_features, Mapping):
            source = nested_features
            categories = _read_field(source, "categorical_features")
            numerics = _read_field(source, "numeric_features")
            if categories is _MISSING and numerics is _MISSING:
                categories = nested_features

    if categories is _MISSING and numerics is _MISSING:
        if not isinstance(source, Mapping):
            raise FeatureBoundaryError(
                "World baseline requires a feature mapping or WorldObservation-like record"
            )
        categories = {
            key: value
            for key, value in source.items()
            if _normalise_key(key) not in _STRUCTURAL_KEYS
        }

    categorical_map = dict(_mapping_or_empty(categories, name="categorical_features"))
    numeric_map = dict(_mapping_or_empty(numerics, name="numeric_features"))
    context_categorical, context_numeric = _observation_context_features(source)
    for key, value in context_categorical.items():
        existing = categorical_map.get(key, _MISSING)
        if existing is not _MISSING and existing != value:
            raise FeatureBoundaryError(f"conflicting structural World feature {key!r}")
        categorical_map.setdefault(key, value)
    for key, value in context_numeric.items():
        existing = numeric_map.get(key, _MISSING)
        if existing is not _MISSING and existing != value:
            raise FeatureBoundaryError(f"conflicting structural World feature {key!r}")
        numeric_map.setdefault(key, value)
    return categorical_map, numeric_map


def _bucket(value: float, thresholds: Sequence[float]) -> str:
    for index, threshold in enumerate(thresholds):
        if value < threshold:
            return f"b{index}"
    return f"b{len(thresholds)}"


def build_feature_state(observation: object) -> FeatureState:
    """Return an immutable allow-listed World-state projection.

    Unknown keys are deliberately ignored; forbidden control/critic/target keys
    raise.  This lets a WorldEpisode retain provenance annotations without
    accidentally growing the model feature surface.
    """

    categorical, numeric = _feature_maps(observation)
    _assert_no_forbidden_keys(categorical, path="categorical_features")
    _assert_no_forbidden_keys(numeric, path="numeric_features")

    canonical: dict[str, str] = {}
    for raw_key, raw_value in categorical.items():
        key = _normalise_key(raw_key)
        key = _CATEGORICAL_ALIASES.get(key, key)
        if key not in ALLOWED_CATEGORICAL_FEATURES:
            continue
        value = _normalise_scalar(raw_value)
        existing = canonical.get(key)
        if existing is not None and existing != value:
            raise FeatureBoundaryError(f"conflicting values for World feature {key!r}")
        canonical[key] = value

    for raw_key, raw_value in numeric.items():
        key = _normalise_key(raw_key)
        key = _NUMERIC_ALIASES.get(key, key)
        if key not in ALLOWED_NUMERIC_FEATURES:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise FeatureBoundaryError(f"numeric World feature {key!r} must be a finite number")
        value = float(raw_value)
        if not math.isfinite(value):
            raise FeatureBoundaryError(f"numeric World feature {key!r} must be finite")
        bucket_key, thresholds = _NUMERIC_BUCKETS[key]
        bucket_value = _bucket(value, thresholds)
        existing = canonical.get(bucket_key)
        if existing is not None and existing != bucket_value:
            raise FeatureBoundaryError(f"conflicting values for World feature {bucket_key!r}")
        canonical[bucket_key] = bucket_value

    canonical_items = tuple(sorted(canonical.items()))
    coarse_items = tuple(item for item in canonical_items if item[0] in _COARSE_FEATURES)
    canonical_json = json.dumps(dict(canonical_items), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return FeatureState(
        exact_state=canonical_items,
        coarse_state=coarse_items,
        features=MappingProxyType(dict(canonical_items)),
        feature_hash=sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


def move_class_from_simple_return(value: float, *, band: float = DIRECTION_BAND) -> str:
    """Map a simple return onto the canonical 50bp DOWN/FLAT/UP contract."""

    if not math.isfinite(value):
        raise ValueError("simple_return must be finite")
    if value >= band:
        return "UP"
    if value <= -band:
        return "DOWN"
    return "FLAT"


def canonical_move_class(value: object) -> str:
    """Return DOWN/FLAT/UP, including an explicit adapter for legacy lowercase."""

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("World outcome class must be a non-empty string")
        mapped = _LEGACY_MOVE_CLASS.get(stripped.lower(), stripped.upper())
        if mapped not in OUTCOME_CLASSES:
            raise ValueError(f"unsupported World outcome class: {value!r}")
        return mapped
    raise ValueError("World outcome class must be a string")


def _embedded_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): item for key, item in parsed.items()}
    return {}


def _outcome_field(outcome: object, *names: str) -> object:
    direct = _read_field(outcome, *names)
    if direct is not _MISSING:
        return direct
    for nested_name in ("label", "outcome", "label_json"):
        nested = _embedded_mapping(_read_field(outcome, nested_name))
        for name in names:
            if name in nested:
                return nested[name]
    return _MISSING


def outcome_horizon_id(outcome: object) -> object:
    direct = _outcome_field(outcome, "horizon_id", "horizon_code")
    if direct is not _MISSING:
        return direct
    horizon = _read_field(outcome, "horizon")
    if isinstance(horizon, Mapping):
        return _read_field(horizon, "horizon_id", "horizon_code", "id")
    return _read_field(horizon, "horizon_id", "horizon_code", "id")


def outcome_move_class(outcome: object) -> str:
    """Resolve the canonical move class from a live or legacy outcome payload."""

    raw = _outcome_field(outcome, "move_class", "direction", "outcome_class")
    if raw is not _MISSING and raw is not None:
        return canonical_move_class(raw)
    if isinstance(outcome, WorldOutcome):
        simple_return = outcome.simple_return
        if simple_return is None:
            raise ValueError("WorldOutcome without direction requires a finite simple_return")
        return move_class_from_simple_return(simple_return)
    schema = _outcome_field(outcome, "schema_version")
    if schema == "world_outcome.v1":
        simple_return = _outcome_field(outcome, "simple_return")
        if isinstance(simple_return, bool) or not isinstance(simple_return, (int, float)):
            raise ValueError("WorldOutcome without direction requires a finite simple_return")
        return move_class_from_simple_return(float(simple_return))
    raise ValueError("observed World outcome requires an explicit DOWN/FLAT/UP label projection")


def parse_model_timestamp(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be a timezone-aware datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def optional_model_timestamp(value: object, *, field_name: str) -> datetime | None:
    if value is _MISSING or value is None or value == "":
        return None
    return parse_model_timestamp(value, field_name=field_name)


def iso_utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def normalise_horizon_id(value: object, allowed_horizons: frozenset[str] | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("horizon_id must be a non-empty fixed-horizon string")
    horizon = value.strip()
    if allowed_horizons is not None and horizon not in allowed_horizons:
        raise ValueError(f"unsupported fixed horizon: {horizon!r}")
    return horizon


__all__ = [
    "ALLOWED_CATEGORICAL_FEATURES",
    "ALLOWED_NUMERIC_FEATURES",
    "DIRECTION_BAND",
    "FEATURE_CONTRACT_FINGERPRINT",
    "OUTCOME_CLASSES",
    "FeatureBoundaryError",
    "FeatureState",
    "FutureLabelLeakageError",
    "ModelUpdate",
    "OutcomeEventConflictError",
    "build_feature_state",
    "canonical_move_class",
    "iso_utc",
    "move_class_from_simple_return",
    "normalise_horizon_id",
    "optional_model_timestamp",
    "outcome_horizon_id",
    "outcome_move_class",
    "parse_model_timestamp",
]
