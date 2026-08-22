"""Deterministic, shadow-only baseline for exogenous WorldEpisodes.

The baseline deliberately predicts a *market* transition, never a Trader
decision.  It therefore accepts only an allow-listed, point-in-time feature
mapping and has no dependency on the planner, RiskGate, broker, portfolio,
prompt, tools, or decision ledger.

The model is a small hierarchical empirical-Bayes classifier:

``exact state -> coarse market state -> horizon-global prior -> uniform``.

Each level uses a Dirichlet-smoothed multinomial posterior.  State counts are
kept separately for every fixed horizon, updates are idempotent by durable
``outcome_event_id``, and a prediction captures the training cutoff/fingerprint
that existed *before* its future label can be learned.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


OUTCOME_CLASSES: tuple[str, str, str] = ("DOWN", "FLAT", "UP")
MODEL_ID = "hierarchical_dirichlet_world_baseline"
MODEL_VERSION = "v1"

# Only these compact, exogenous state descriptors may enter the discrete model.
# The aliases below preserve ergonomic compatibility with existing market naming,
# but the canonical names are the only values retained in a FeatureState.
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

# Numeric values are never learned/normalised from the corpus: fixed buckets are
# part of the feature contract, so their use cannot leak future test statistics.
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

# Deliberately fixed, human-reviewable bins.  The labels are semantic only; no
# component of this mapping is fitted from outcomes or from a test set.
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

# This list is intentionally broader than the feature aliases.  Unknown benign
# values are ignored by the strict whitelist, but a forbidden control/critic
# field is rejected loudly rather than silently becoming a model input later.
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
    """Frozen canonical feature view used by the baseline, never the input map."""

    exact_state: _StateKey
    coarse_state: _StateKey
    features: Mapping[str, str]
    feature_hash: str

    def as_dict(self) -> dict[str, str]:
        return dict(self.features)


@dataclass(frozen=True)
class BaselinePrediction:
    """An immutable, non-authoritative forecast audit record."""

    model_id: str
    model_version: str
    episode_id: str | None
    prediction_id: str | None
    horizon_id: str
    created_at: str | None
    run_id: str
    probabilities: Mapping[str, float]
    predicted_class: str
    tier: str
    support: int
    exact_support: int
    coarse_support: int
    global_support: int
    status: str
    recommendation: str
    authority: str
    decision_effect: str
    feature_hash: str
    state_key: _StateKey
    coarse_state_key: _StateKey
    training_cutoff: str | None
    model_fingerprint: str

    @property
    def non_authoritative(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_contract_fingerprint": FEATURE_CONTRACT_FINGERPRINT,
            "episode_id": self.episode_id,
            "prediction_id": self.prediction_id,
            "horizon_id": self.horizon_id,
            "created_at": self.created_at,
            "predicted_at": self.created_at,
            "run_id": self.run_id,
            "probabilities": dict(self.probabilities),
            "predicted_class": self.predicted_class,
            "tier": self.tier,
            "support": self.support,
            "exact_support": self.exact_support,
            "coarse_support": self.coarse_support,
            "global_support": self.global_support,
            "status": self.status,
            "recommendation": self.recommendation,
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "feature_hash": self.feature_hash,
            "state_key": list(self.state_key),
            "coarse_state_key": list(self.coarse_state_key),
            "training_cutoff": self.training_cutoff,
            "model_fingerprint": self.model_fingerprint,
            "non_authoritative": True,
        }

    def to_dict(self) -> dict[str, Any]:
        """Compatibility projection for append-only shadow stores/runtimes."""

        return self.as_dict()


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


@dataclass
class _HorizonCounts:
    exact: dict[_StateKey, Counter[str]] = field(default_factory=dict)
    coarse: dict[_StateKey, Counter[str]] = field(default_factory=dict)
    global_counts: Counter[str] = field(default_factory=Counter)
    training_cutoff: datetime | None = None
    applied_events: dict[str, str] = field(default_factory=dict)


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
    # The exact World whitelist takes precedence over token matching.  For
    # example, ``range_position`` is a market indicator, not a portfolio
    # position.  The same protection applies to aliases accepted below.
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
    """Extract safe structural market metadata from a WorldObservation-like value."""

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
    """Extract only the feature payload, not a full episode/decision wrapper."""

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
            raise FeatureBoundaryError("World baseline requires a feature mapping or WorldObservation-like record")
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
    accidentally growing the baseline feature surface.
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


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
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


def _optional_timestamp(value: object, *, field_name: str) -> datetime | None:
    if value is _MISSING or value is None or value == "":
        return None
    return _parse_timestamp(value, field_name=field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _normalise_horizon(value: object, allowed_horizons: frozenset[str] | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("horizon_id must be a non-empty fixed-horizon string")
    horizon = value.strip().lower()
    if allowed_horizons is not None and horizon not in allowed_horizons:
        raise ValueError(f"unsupported fixed horizon: {horizon!r}")
    return horizon


def _normalise_outcome_class(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("World outcome class must be a string")
    outcome_class = value.strip().upper()
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"unsupported World outcome class: {outcome_class!r}")
    return outcome_class


def _as_status(value: object) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate).strip().lower()


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
    payload = _embedded_mapping(_read_field(outcome, "label_json"))
    for name in names:
        if name in payload:
            return payload[name]
    return _MISSING


def _outcome_horizon(outcome: object) -> object:
    direct = _outcome_field(outcome, "horizon_id", "horizon_code")
    if direct is not _MISSING:
        return direct
    raw_horizon = _read_field(outcome, "horizon")
    if isinstance(raw_horizon, Mapping):
        return _read_field(raw_horizon, "horizon_id", "horizon_code", "id")
    if raw_horizon is not _MISSING and raw_horizon is not None:
        return _read_field(raw_horizon, "horizon_id", "horizon_code", "id")
    return _MISSING


def _outcome_class(outcome: object) -> str:
    raw = _outcome_field(outcome, "move_class", "direction", "outcome_class", "label")
    if raw is not _MISSING and raw is not None:
        return _normalise_outcome_class(raw)
    raise ValueError(
        "observed World outcome requires an explicit versioned DOWN/FLAT/UP label projection"
    )


def _extract_observation_timestamp(observation: object) -> datetime | None:
    source = observation
    for _ in range(2):
        value = _read_field(source, "available_at", "as_of_bar_ts", "captured_at", "observed_at")
        parsed = _optional_timestamp(value, field_name="observation timestamp")
        if parsed is not None:
            return parsed
        nested = _read_field(source, "observation")
        if nested is _MISSING or nested is None:
            break
        source = nested
    return None


def _extract_episode_id(observation: object) -> str | None:
    """Read a durable WorldEpisode identity without looking at Trader records."""

    source = observation
    for _ in range(2):
        raw = _read_field(source, "episode_id", "world_episode_id")
        if raw is not _MISSING and raw is not None:
            value = str(raw).strip()
            if value:
                return value
        nested = _read_field(source, "observation")
        if nested is _MISSING or nested is None:
            break
        source = nested
    return None


def _prediction_id(
    *,
    episode_id: str | None,
    horizon_id: str,
    feature_hash: str,
    model_fingerprint: str,
    training_cutoff: str | None,
) -> str | None:
    if episode_id is None:
        return None
    payload = {
        "episode_id": episode_id,
        "horizon_id": horizon_id,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "feature_hash": feature_hash,
        "model_fingerprint": model_fingerprint,
        "training_cutoff": training_cutoff,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"world-prediction:v1:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _predicted_class(probabilities: Mapping[str, float]) -> str:
    """Use FLAT for an exact tie so a cold-start audit is non-directional."""

    highest = max(probabilities.values())
    if probabilities["FLAT"] == highest:
        return "FLAT"
    return next(label for label in OUTCOME_CLASSES if probabilities[label] == highest)


class HierarchicalDirichletWorldBaseline:
    """Incremental, deterministic and permanently non-authoritative baseline.

    ``minimum_*_support`` changes only which already-smoothed hierarchy tier is
    exposed.  It never authorises a Trader action: every prediction remains
    ``shadow_only`` and ``NO_GO``.
    """

    model_id = MODEL_ID
    model_version = MODEL_VERSION

    def __init__(
        self,
        *,
        alpha: float = 3.0,
        minimum_global_support: int = 20,
        minimum_coarse_support: int = 8,
        minimum_exact_support: int = 5,
        allowed_horizons: Iterable[str] | None = ("elapsed_4h.v1", "elapsed_1d.v1"),
    ) -> None:
        if not math.isfinite(alpha) or alpha <= 0:
            raise ValueError("alpha must be finite and strictly positive")
        for name, value in (
            ("minimum_global_support", minimum_global_support),
            ("minimum_coarse_support", minimum_coarse_support),
            ("minimum_exact_support", minimum_exact_support),
        ):
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        normalised_horizons = None
        if allowed_horizons is not None:
            normalised_horizons = frozenset(
                _normalise_horizon(item, None) for item in allowed_horizons
            )
            if not normalised_horizons:
                raise ValueError("allowed_horizons must not be empty")
        self.alpha = float(alpha)
        self.minimum_global_support = minimum_global_support
        self.minimum_coarse_support = minimum_coarse_support
        self.minimum_exact_support = minimum_exact_support
        self._allowed_horizons = normalised_horizons
        self._horizons: dict[str, _HorizonCounts] = {}

    def predict(
        self,
        observation: object,
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> BaselinePrediction:
        """Predict from the current state without modifying any counts.

        Passing ``prediction_at`` (or an observation with ``available_at``) is
        mandatory for historical/prequential evaluation: labels after that
        instant are rejected rather than silently leaking into the prediction.
        Evidence available exactly at the frozen snapshot cutoff is admissible;
        the runtime matures it before predicting that same snapshot.
        """

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=horizon)
        state = build_feature_state(observation)
        counts = self._horizons.get(horizon_key, _HorizonCounts())
        reference_at = _optional_timestamp(prediction_at, field_name="prediction_at")
        if reference_at is None:
            reference_at = _extract_observation_timestamp(observation)
        if (
            reference_at is not None
            and counts.training_cutoff is not None
            and counts.training_cutoff > reference_at
        ):
            raise FutureLabelLeakageError(
                "training cutoff is after prediction_at; rebuild a causally earlier model state"
            )

        probabilities, tier, support, exact_support, coarse_support, global_support = self._predict_from_counts(
            counts,
            state,
        )
        status = "warming_up" if global_support < self.minimum_global_support else "shadow_only"
        predicted_class = _predicted_class(probabilities)
        episode_id = _extract_episode_id(observation)
        training_cutoff = _iso(counts.training_cutoff)
        model_fingerprint = self.model_fingerprint(horizon_key)
        return BaselinePrediction(
            model_id=self.model_id,
            model_version=self.model_version,
            episode_id=episode_id,
            prediction_id=_prediction_id(
                episode_id=episode_id,
                horizon_id=horizon_key,
                feature_hash=state.feature_hash,
                model_fingerprint=model_fingerprint,
                training_cutoff=training_cutoff,
            ),
            horizon_id=horizon_key,
            created_at=_iso(reference_at),
            run_id="world_shadow.v1",
            probabilities=MappingProxyType(dict(probabilities)),
            predicted_class=predicted_class,
            tier=tier,
            support=support,
            exact_support=exact_support,
            coarse_support=coarse_support,
            global_support=global_support,
            status=status,
            recommendation="NO_GO",
            authority="shadow_only",
            decision_effect="none",
            feature_hash=state.feature_hash,
            state_key=state.exact_state,
            coarse_state_key=state.coarse_state,
            training_cutoff=training_cutoff,
            model_fingerprint=model_fingerprint,
        )

    def predict_proba(
        self,
        observation: object,
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> dict[str, float]:
        """Convenience view; use :meth:`predict` for complete provenance."""

        return dict(
            self.predict(
                observation,
                horizon_id,
                horizon=horizon,
                prediction_at=prediction_at,
            ).probabilities
        )

    def apply_outcome(
        self,
        outcome: object,
        observation: object | None = None,
        *,
        available_through: datetime | str | None = None,
    ) -> ModelUpdate:
        """Apply one causally available, observed, eligible label exactly once.

        Pending/missing/unknown outcomes and labels not available by
        ``available_through`` are intentionally no-ops.  A caller processing a
        historical walk-forward run must pass its cutoff explicitly.
        """

        event_id_value = _outcome_field(outcome, "outcome_event_id", "event_id", "outcome_id")
        event_id = None if event_id_value is _MISSING else str(event_id_value).strip() or None
        status = _as_status(_outcome_field(outcome, "status"))
        if status != "observed":
            return self._no_update("outcome_not_observed", event_id=event_id, horizon_id=None)
        sealed = _outcome_field(outcome, "sealed")
        if sealed is False:
            return self._no_update("outcome_not_sealed", event_id=event_id, horizon_id=None)

        eligible = _outcome_field(outcome, "training_eligible")
        if eligible is _MISSING:
            episode = _read_field(outcome, "episode")
            eligible = _read_field(episode, "training_eligible") if episode is not _MISSING else _MISSING
        if eligible is not True:
            return self._no_update("outcome_not_training_eligible", event_id=event_id, horizon_id=None)
        if event_id is None:
            raise ValueError("observed World outcome requires a durable outcome_event_id")

        horizon_value = _outcome_horizon(outcome)
        horizon_key = self._resolve_horizon(horizon_id=horizon_value, horizon=None)
        outcome_class = _outcome_class(outcome)
        label_available_at = _optional_timestamp(
            _outcome_field(outcome, "available_at", "label_available_at", "sealed_at"),
            field_name="outcome available_at",
        )
        if label_available_at is None:
            return self._no_update("missing_label_available_at", event_id=event_id, horizon_id=horizon_key)

        cutoff = _optional_timestamp(available_through, field_name="available_through")
        if cutoff is None:
            cutoff = label_available_at
        if label_available_at > cutoff:
            return self._no_update("label_not_available_at_cutoff", event_id=event_id, horizon_id=horizon_key)

        source_observation = observation
        if source_observation is None:
            source_observation = _read_field(outcome, "observation", "episode")
        if source_observation is _MISSING or source_observation is None:
            raise ValueError("observed World outcome requires its immutable WorldObservation")
        observation_at = _extract_observation_timestamp(source_observation)
        if observation_at is not None and label_available_at <= observation_at:
            return self._no_update("label_not_after_observation", event_id=event_id, horizon_id=horizon_key)
        state = build_feature_state(source_observation)
        signature = self._event_signature(
            horizon_id=horizon_key,
            outcome_class=outcome_class,
            label_available_at=label_available_at,
            state=state,
        )
        counts = self._horizons.setdefault(horizon_key, _HorizonCounts())
        previous_signature = counts.applied_events.get(event_id)
        if previous_signature is not None:
            if previous_signature != signature:
                raise OutcomeEventConflictError(
                    f"outcome_event_id {event_id!r} already applied with different World-model content"
                )
            return self._no_update("duplicate_outcome_event", event_id=event_id, horizon_id=horizon_key)
        if counts.training_cutoff is not None and label_available_at < counts.training_cutoff:
            return self._no_update("out_of_order_label", event_id=event_id, horizon_id=horizon_key)

        counts.global_counts[outcome_class] += 1
        if state.exact_state:
            counts.exact.setdefault(state.exact_state, Counter())[outcome_class] += 1
        if state.coarse_state:
            counts.coarse.setdefault(state.coarse_state, Counter())[outcome_class] += 1
        counts.applied_events[event_id] = signature
        if counts.training_cutoff is None or label_available_at > counts.training_cutoff:
            counts.training_cutoff = label_available_at
        return ModelUpdate(
            applied=True,
            reason="applied",
            outcome_event_id=event_id,
            horizon_id=horizon_key,
            global_support=self._support(counts.global_counts),
            training_cutoff=_iso(counts.training_cutoff),
            model_fingerprint=self.model_fingerprint(horizon_key),
        )

    # Familiar aliases make a future runner/GRU adapter depend on a tiny common
    # contract without importing the baseline implementation details.
    learn = apply_outcome
    update = apply_outcome
    update_baseline = apply_outcome

    def training_cutoff(self, horizon_id: str) -> str | None:
        """Return the latest causal label availability included for one horizon."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        counts = self._horizons.get(horizon_key)
        return None if counts is None else _iso(counts.training_cutoff)

    def model_fingerprint(self, horizon_id: str) -> str:
        """Stable fingerprint of one horizon's learned counts and cutoff."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        counts = self._horizons.get(horizon_key, _HorizonCounts())
        payload = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_contract_fingerprint": FEATURE_CONTRACT_FINGERPRINT,
            "horizon_id": horizon_key,
            "alpha": self.alpha,
            "minimum_global_support": self.minimum_global_support,
            "minimum_coarse_support": self.minimum_coarse_support,
            "minimum_exact_support": self.minimum_exact_support,
            "training_cutoff": _iso(counts.training_cutoff),
            "global": self._counter_payload(counts.global_counts),
            "coarse": self._state_counts_payload(counts.coarse),
            "exact": self._state_counts_payload(counts.exact),
            "applied_events": sorted(counts.applied_events),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return sha256(encoded.encode("utf-8")).hexdigest()

    def _resolve_horizon(self, *, horizon_id: object, horizon: object | None) -> str:
        if horizon_id is None:
            horizon_id = horizon
        elif horizon is not None:
            first = _normalise_horizon(horizon_id, self._allowed_horizons)
            second = _normalise_horizon(horizon, self._allowed_horizons)
            if first != second:
                raise ValueError("horizon_id and horizon disagree")
            return first
        return _normalise_horizon(horizon_id, self._allowed_horizons)

    def _predict_from_counts(
        self,
        counts: _HorizonCounts,
        state: FeatureState,
    ) -> tuple[dict[str, float], str, int, int, int, int]:
        global_support = self._support(counts.global_counts)
        exact_counts = counts.exact.get(state.exact_state, Counter()) if state.exact_state else Counter()
        coarse_counts = counts.coarse.get(state.coarse_state, Counter()) if state.coarse_state else Counter()
        exact_support = self._support(exact_counts)
        coarse_support = self._support(coarse_counts)
        uniform = {label: 1.0 / len(OUTCOME_CLASSES) for label in OUTCOME_CLASSES}
        if global_support == 0:
            return uniform, "uniform", 0, exact_support, coarse_support, 0

        global_probabilities = self._posterior(counts.global_counts, uniform)
        if coarse_support >= self.minimum_coarse_support:
            coarse_probabilities = self._posterior(coarse_counts, global_probabilities)
        else:
            coarse_probabilities = global_probabilities
        if exact_support >= self.minimum_exact_support:
            exact_probabilities = self._posterior(exact_counts, coarse_probabilities)
            return (
                exact_probabilities,
                "exact",
                exact_support,
                exact_support,
                coarse_support,
                global_support,
            )
        if coarse_support >= self.minimum_coarse_support:
            return (
                coarse_probabilities,
                "coarse",
                coarse_support,
                exact_support,
                coarse_support,
                global_support,
            )
        return (
            global_probabilities,
            "global",
            global_support,
            exact_support,
            coarse_support,
            global_support,
        )

    def _posterior(self, observed: Mapping[str, int], parent: Mapping[str, float]) -> dict[str, float]:
        support = self._support(observed)
        denominator = support + self.alpha
        probabilities = {
            label: (float(observed.get(label, 0)) + self.alpha * float(parent[label])) / denominator
            for label in OUTCOME_CLASSES
        }
        # Guard only against floating-point sum drift; no clipping or calibration
        # is performed with post-T0 data.
        normaliser = sum(probabilities.values())
        return {label: probabilities[label] / normaliser for label in OUTCOME_CLASSES}

    def _event_signature(
        self,
        *,
        horizon_id: str,
        outcome_class: str,
        label_available_at: datetime,
        state: FeatureState,
    ) -> str:
        payload = {
            "horizon_id": horizon_id,
            "outcome_class": outcome_class,
            "label_available_at": _iso(label_available_at),
            "feature_hash": state.feature_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return sha256(encoded.encode("utf-8")).hexdigest()

    def _no_update(self, reason: str, *, event_id: str | None, horizon_id: str | None) -> ModelUpdate:
        if horizon_id is None:
            return ModelUpdate(False, reason, event_id, None, 0, None, None)
        counts = self._horizons.get(horizon_id, _HorizonCounts())
        return ModelUpdate(
            applied=False,
            reason=reason,
            outcome_event_id=event_id,
            horizon_id=horizon_id,
            global_support=self._support(counts.global_counts),
            training_cutoff=_iso(counts.training_cutoff),
            model_fingerprint=self.model_fingerprint(horizon_id),
        )

    @staticmethod
    def _support(counts: Mapping[str, int]) -> int:
        return sum(int(counts.get(label, 0)) for label in OUTCOME_CLASSES)

    @staticmethod
    def _counter_payload(counts: Mapping[str, int]) -> dict[str, int]:
        return {label: int(counts.get(label, 0)) for label in OUTCOME_CLASSES}

    def _state_counts_payload(self, counts: Mapping[_StateKey, Mapping[str, int]]) -> list[dict[str, object]]:
        return [
            {
                "state": list(state),
                "counts": self._counter_payload(counter),
            }
            for state, counter in sorted(counts.items())
        ]


# Short name for callers that do not need to care about the current baseline
# family.  A future GRU challenger can implement the same predict/update shape.
WorldBaseline = HierarchicalDirichletWorldBaseline


__all__ = [
    "ALLOWED_CATEGORICAL_FEATURES",
    "ALLOWED_NUMERIC_FEATURES",
    "OUTCOME_CLASSES",
    "BaselinePrediction",
    "FeatureBoundaryError",
    "FeatureState",
    "FutureLabelLeakageError",
    "HierarchicalDirichletWorldBaseline",
    "MODEL_ID",
    "MODEL_VERSION",
    "ModelUpdate",
    "OutcomeEventConflictError",
    "WorldBaseline",
    "build_feature_state",
]
