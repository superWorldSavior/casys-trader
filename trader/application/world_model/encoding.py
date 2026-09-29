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

from trader.domain.world_context import (
    ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
    ALLOWED_CONTEXT_NUMERIC_FEATURES,
    CONTEXT_FEATURE_CONTRACT_ID,
)
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    PREDICTION_CLASSES,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    canonical_payload,
    canonical_sha256,
)
from trader.domain.world_feature_contract import (
    COMPANY_FEATURE_GROUP_ID,
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_ID,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    MACRO_FEATURE_GROUP_ID,
    MARKET_FEATURE_GROUP_ID,
    STATUS_FEATURE_GROUP_ID,
    GRAPH_PATH_RULE_VERSION,
    GRAPH_WINDOWS_AND_DECAY,
    WorldFeatureContract,
    WorldFeatureMask,
    market_feature_contract,
    context_feature_contract,
    graph_feature_contract,
    graph_content_mask,
    topology_status_only_mask,
)


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
        "return_bucket",
        "atr_bucket",
        "range_position_bucket",
        "family_regime",
        "trend",
        "data_freshness",
        "data_age_bucket",
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
        "data_age_minutes",
        "open_close_return",
        "high_low_range_pct",
    }
)

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
    "data_age_minutes": ("data_age_bucket", (5.0, 15.0, 60.0, 240.0)),
    "open_close_return": ("open_close_return_bucket", (-0.05, -0.015, -0.003, 0.003, 0.015, 0.05)),
    "high_low_range_pct": ("high_low_range_pct_bucket", (0.005, 0.015, 0.03, 0.06)),
}

_COARSE_FEATURES = frozenset(
    {
        "session_phase",
        "market_regime",
        "volatility_state",
        "return_bucket",
        "atr_bucket",
        "range_position_bucket",
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

CONTEXT_CATEGORICAL_FEATURES = ALLOWED_CATEGORICAL_FEATURES | ALLOWED_CONTEXT_CATEGORICAL_FEATURES
CONTEXT_NUMERIC_FEATURES = ALLOWED_NUMERIC_FEATURES | ALLOWED_CONTEXT_NUMERIC_FEATURES
CONTEXT_COARSE_FEATURES = _COARSE_FEATURES | frozenset(
    {
        "context_status",
        "macro_status",
        "company_status",
        "company_thesis_status",
        "company_coverage_status",
        "company_freshness_status",
        "company_source_count_bucket",
    }
)


def _context_feature_contract_fingerprint() -> str:
    payload = {
        "categorical": sorted(CONTEXT_CATEGORICAL_FEATURES),
        "numeric": sorted(CONTEXT_NUMERIC_FEATURES),
        "numeric_buckets": {
            key: {"bucket_key": bucket_key, "thresholds": thresholds}
            for key, (bucket_key, thresholds) in sorted(_NUMERIC_BUCKETS.items())
        },
        "coarse_features": sorted(CONTEXT_COARSE_FEATURES),
        "feature_contract_version": CONTEXT_FEATURE_CONTRACT_ID,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


FEATURE_CONTRACT_FINGERPRINT_CONTEXT = _context_feature_contract_fingerprint()

GRAPH_CATEGORICAL_FEATURES = (
    CONTEXT_CATEGORICAL_FEATURES | GRAPH_STATUS_CATEGORICAL_FEATURES | GRAPH_CONTENT_CATEGORICAL_FEATURES
)
GRAPH_COARSE_FEATURES = CONTEXT_COARSE_FEATURES | frozenset(
    {
        "graph_status",
        "graph_scope_status",
        "graph_coverage_status",
        "graph_missingness_status",
    }
)


def _graph_feature_contract_fingerprint() -> str:
    payload = {
        "categorical": sorted(GRAPH_CATEGORICAL_FEATURES),
        "numeric": sorted(CONTEXT_NUMERIC_FEATURES),
        "numeric_buckets": {
            key: {"bucket_key": bucket_key, "thresholds": thresholds}
            for key, (bucket_key, thresholds) in sorted(_NUMERIC_BUCKETS.items())
        },
        "coarse_features": sorted(GRAPH_COARSE_FEATURES),
        "feature_contract_version": GRAPH_FEATURE_CONTRACT_ID,
        "path_rule_version": GRAPH_PATH_RULE_VERSION,
        "windows_and_decay": canonical_payload(GRAPH_WINDOWS_AND_DECAY),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


FEATURE_CONTRACT_FINGERPRINT_GRAPH = _graph_feature_contract_fingerprint()

_LANE_MASK_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "market": ("market.v1", (MARKET_FEATURE_GROUP_ID,)),
    "status_only": ("status_only.v1", (MARKET_FEATURE_GROUP_ID, STATUS_FEATURE_GROUP_ID)),
    "company": (
        "company.v1",
        (MARKET_FEATURE_GROUP_ID, STATUS_FEATURE_GROUP_ID, COMPANY_FEATURE_GROUP_ID),
    ),
    "macro": (
        "macro.v1",
        (MARKET_FEATURE_GROUP_ID, STATUS_FEATURE_GROUP_ID, MACRO_FEATURE_GROUP_ID),
    ),
    "joint": (
        "joint.v1",
        (MARKET_FEATURE_GROUP_ID, STATUS_FEATURE_GROUP_ID, COMPANY_FEATURE_GROUP_ID, MACRO_FEATURE_GROUP_ID),
    ),
}


@dataclass(frozen=True)
class WorldEncoderProfile:
    """Application projection of a frozen domain contract/mask pair."""

    contract: WorldFeatureContract
    mask: WorldFeatureMask
    allowed_categorical: frozenset[str]
    allowed_numeric: frozenset[str]
    coarse_features: frozenset[str]
    include_context: bool
    encoder_fingerprint: str


@dataclass(frozen=True)
class WorldLaneModelIdentity:
    """Explicit cold-lane lineage consumed by Markov/GRU factories."""

    lane_id: str
    model_id: str
    model_version: str
    seed: int
    sequence_length: int | None
    study_cohort_id: str
    manifest_sha256: str
    started_event_id: str
    replay_bound_event_id: str
    prior_training_lineage: tuple[str, ...] = ()
    trained_through: str | None = None

    def __post_init__(self) -> None:
        if self.trained_through is not None:
            raise ValueError("cold lane must not carry prior training")
        if self.prior_training_lineage:
            raise ValueError("cold lane must not reuse a warm lineage")
        if self.replay_bound_event_id != self.started_event_id:
            raise ValueError("cold lane replay must be bound to WorldCohortStarted")


def _unselected_feature_names(contract: WorldFeatureContract, mask: WorldFeatureMask) -> frozenset[str]:
    mask.assert_compatible_with(contract)
    selected = set(mask.selected_groups)
    names: set[str] = set()
    for group in contract.groups:
        if group.group_id in selected:
            continue
        names.update(group.categorical_features)
        names.update(group.numeric_features)
    return frozenset(names)


def resolve_encoder_profile(contract: WorldFeatureContract, mask: WorldFeatureMask) -> WorldEncoderProfile:
    """Intersect a domain mask with the frozen market/context/graph encoder vocabulary."""

    if not isinstance(contract, WorldFeatureContract):
        raise TypeError("encoder profile requires a WorldFeatureContract")
    if not isinstance(mask, WorldFeatureMask):
        raise TypeError("encoder profile requires a WorldFeatureMask")
    mask.assert_compatible_with(contract)
    if contract.contract_id == MARKET_FEATURE_CONTRACT_ID:
        base_categorical = ALLOWED_CATEGORICAL_FEATURES
        base_numeric = ALLOWED_NUMERIC_FEATURES
        base_coarse = _COARSE_FEATURES
        encoder_fingerprint = FEATURE_CONTRACT_FINGERPRINT
        include_context = False
    elif contract.contract_id == CONTEXT_FEATURE_CONTRACT_ID:
        base_categorical = CONTEXT_CATEGORICAL_FEATURES
        base_numeric = CONTEXT_NUMERIC_FEATURES
        base_coarse = CONTEXT_COARSE_FEATURES
        encoder_fingerprint = FEATURE_CONTRACT_FINGERPRINT_CONTEXT
        include_context = True
    elif contract.contract_id == GRAPH_FEATURE_CONTRACT_ID:
        base_categorical = GRAPH_CATEGORICAL_FEATURES
        base_numeric = CONTEXT_NUMERIC_FEATURES
        base_coarse = GRAPH_COARSE_FEATURES
        encoder_fingerprint = FEATURE_CONTRACT_FINGERPRINT_GRAPH
        include_context = False
    else:
        raise ValueError(f"unsupported WorldFeatureContract: {contract.contract_id}")
    dropped = _unselected_feature_names(contract, mask)
    return WorldEncoderProfile(
        contract=contract,
        mask=mask,
        allowed_categorical=frozenset(name for name in base_categorical if name not in dropped),
        allowed_numeric=frozenset(name for name in base_numeric if name not in dropped),
        coarse_features=frozenset(name for name in base_coarse if name not in dropped),
        include_context=include_context,
        encoder_fingerprint=encoder_fingerprint,
    )


def world_lane_encoder_profile(kind: str) -> WorldEncoderProfile:
    key = kind.strip() if isinstance(kind, str) else ""
    if key == "topology_status_only":
        contract = graph_feature_contract()
        mask = topology_status_only_mask()
        return resolve_encoder_profile(contract, mask)
    if key == "graph_content":
        contract = graph_feature_contract()
        mask = graph_content_mask()
        return resolve_encoder_profile(contract, mask)
    spec = _LANE_MASK_SPECS.get(key)
    if spec is None:
        raise ValueError(f"unknown world lane encoder profile: {kind!r}")
    mask_id, selected_groups = spec
    contract = market_feature_contract() if key == "market" else context_feature_contract()
    mask = WorldFeatureMask.bind(contract, mask_id=mask_id, selected_groups=selected_groups)
    return resolve_encoder_profile(contract, mask)


def bound_encoder_profile(
    contract: WorldFeatureContract | None,
    mask: WorldFeatureMask | None,
    *,
    include_context: bool = False,
) -> WorldEncoderProfile | None:
    if (contract is None) ^ (mask is None):
        raise ValueError("feature_contract and feature_mask must be provided together")
    if contract is None or mask is None:
        return None
    profile = resolve_encoder_profile(contract, mask)
    if include_context and not profile.include_context:
        raise ValueError("include_context contradicts WorldFeatureMask")
    return profile


class PredictorIdentityCollisionError(ValueError):
    """Two predictors claim the same runtime identity.

    Machine-readable: ``code`` classifies the fault, ``context`` carries the
    colliding key and owners, ``recovery`` tells the operator what to do.
    """

    def __init__(
        self, *, code: str, context: Mapping[str, object], recovery: str
    ) -> None:
        self.code = code
        self.context = dict(context)
        self.recovery = recovery
        super().__init__(f"{code}: {self.context} (recovery: {recovery})")


def predictor_runtime_key(predictor: object) -> tuple[str, str, str, str]:
    """Return the dedup identity of one shadow predictor.

    Lane predictors are keyed by study lineage first: the same lane shared by
    two cohorts (or two generations) mints two independent predictors. The
    model pair stays in the key so background predictors without lineage still
    dedup on (model_id, model_version).
    """

    model_id = str(getattr(predictor, "model_id", "") or "").strip()
    model_version = str(getattr(predictor, "model_version", "") or "").strip()
    if not model_id:
        predictor_type = type(predictor)
        model_id = f"{predictor_type.__module__}.{predictor_type.__qualname__}"
    lane_identity = getattr(predictor, "lane_identity", None)
    study_cohort_id = str(getattr(lane_identity, "study_cohort_id", "") or "").strip()
    lane_id = str(getattr(lane_identity, "lane_id", "") or "").strip()
    return (study_cohort_id, lane_id, model_id, model_version or "unversioned")


def lane_prediction_lineage(predictor: object) -> dict[str, str]:
    """Return the study lineage a lane predictor stamps on its predictions.

    Empty for background predictors without lane identity: their prediction
    ids keep the legacy cohort-blind shape.
    """

    lane_identity = getattr(predictor, "lane_identity", None)
    study_cohort_id = getattr(lane_identity, "study_cohort_id", None)
    lane_id = getattr(lane_identity, "lane_id", None)
    if not study_cohort_id or not lane_id:
        return {}
    return {"study_cohort_id": str(study_cohort_id), "lane_id": str(lane_id)}


def bind_cold_lane_identity(
    *,
    lane_id: str,
    model_id: str,
    model_version: str,
    seed: int,
    sequence_length: int | None,
    study_cohort_id: str,
    manifest_sha256: str,
    started_event_id: str,
    prototype: object | None = None,
) -> WorldLaneModelIdentity:
    if prototype is not None:
        raise ValueError("cold factory must not reuse a warm model")
    return WorldLaneModelIdentity(
        lane_id=lane_id,
        model_id=model_id,
        model_version=model_version,
        seed=seed,
        sequence_length=sequence_length,
        study_cohort_id=study_cohort_id,
        manifest_sha256=manifest_sha256,
        started_event_id=started_event_id,
        replay_bound_event_id=started_event_id,
    )


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
    categorical_values: Mapping[str, str]
    numeric_values: Mapping[str, float]

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


def _is_forbidden_key(key: str, *, known_feature_keys: frozenset[str] | None = None) -> bool:
    known = known_feature_keys or (
        ALLOWED_CATEGORICAL_FEATURES
        | ALLOWED_NUMERIC_FEATURES
        | frozenset(bucket_key for bucket_key, _thresholds in _NUMERIC_BUCKETS.values())
    )
    if key in known:
        return False
    if key in _FORBIDDEN_EXACT:
        return True
    return any(fragment in key for fragment in _FORBIDDEN_FRAGMENTS)


def _assert_no_forbidden_keys(
    values: Mapping[object, object],
    *,
    path: str = "features",
    known_feature_keys: frozenset[str] | None = None,
) -> None:
    for raw_key, value in values.items():
        key = _normalise_key(raw_key)
        if _is_forbidden_key(key, known_feature_keys=known_feature_keys):
            raise FeatureBoundaryError(f"forbidden World feature at {path}.{key}")
        if isinstance(value, Mapping):
            _assert_no_forbidden_keys(value, path=f"{path}.{key}", known_feature_keys=known_feature_keys)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, Mapping):
                    _assert_no_forbidden_keys(item, path=f"{path}.{key}", known_feature_keys=known_feature_keys)


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


def _context_feature_maps(source: object) -> tuple[dict[str, object], dict[str, object]]:
    raw = _read_field(source, "context")
    if raw is _MISSING or raw is None:
        return {}, {}
    if not isinstance(raw, Mapping):
        to_dict = getattr(raw, "to_dict", None)
        raw = to_dict() if callable(to_dict) else {}
    if not isinstance(raw, Mapping):
        return {}, {}
    categorical = _mapping_or_empty(raw.get("categorical_features"), name="context.categorical_features")
    numeric = _mapping_or_empty(raw.get("numeric_features"), name="context.numeric_features")
    return dict(categorical), dict(numeric)


def _graph_feature_maps(source: object) -> tuple[dict[str, object], dict[str, object]]:
    raw = _read_field(source, "graph_features")
    if raw is _MISSING or raw is None:
        return {}, {}
    if not isinstance(raw, Mapping):
        to_dict = getattr(raw, "to_dict", None)
        raw = to_dict() if callable(to_dict) else {}
    if not isinstance(raw, Mapping):
        return {}, {}
    categorical = _mapping_or_empty(raw.get("categorical_features"), name="graph_features.categorical_features")
    numeric = _mapping_or_empty(raw.get("numeric_features"), name="graph_features.numeric_features")
    return dict(categorical), dict(numeric)


def revalidate_context_observation(observation: object) -> None:
    """Replay context mappings through the domain object before encoding/training."""

    if isinstance(observation, WorldEpisode):
        if observation.observation.context is None:
            raise FeatureBoundaryError("context World model requires a context snapshot")
        return
    if isinstance(observation, WorldObservation):
        if observation.context is None:
            raise FeatureBoundaryError("context World model requires a context snapshot")
        return
    if not isinstance(observation, Mapping):
        return
    payload = {str(key): item for key, item in observation.items()}
    if "observation" in payload:
        WorldEpisode.from_dict(payload)
        return
    WorldObservation.from_dict(payload)


def revalidate_graph_observation(observation: object) -> None:
    """Replay graph mappings through the domain snapshot before encoding/training.

    Unmapped/ambiguous missingness is a valid observation: hydrate projects it
    to a rootless snapshot instead of selecting another entity. A graph contract
    without a graph payload stays valid status-only evidence.
    """

    if isinstance(observation, (WorldEpisode, WorldObservation)):
        return
    if not isinstance(observation, Mapping):
        return
    payload = {str(key): item for key, item in observation.items()}
    nested = payload.get("observation") if "observation" in payload else payload
    if not isinstance(nested, Mapping):
        return
    features = nested.get("graph_features")
    has_snapshot = nested.get("graph") is not None or (
        isinstance(features, Mapping) and features.get("snapshot") is not None
    )
    if not has_snapshot:
        return
    if "observation" in payload:
        WorldEpisode.from_dict(payload)
        return
    WorldObservation.from_dict(payload)


def _feature_contract_text(source: object) -> str:
    raw = _read_field(source, "feature_contract_version")
    if raw is _MISSING or raw is None:
        return ""
    text = str(raw).strip()
    return text


def feature_contract_version_of(observation: object) -> str:
    """Return the canonical observation contract. Envelope/nested must agree."""

    envelope = _feature_contract_text(observation)
    nested_source = _read_field(observation, "observation")
    nested = ""
    if nested_source is not _MISSING and nested_source is not None:
        nested = _feature_contract_text(nested_source)
        deeper_source = _read_field(nested_source, "observation")
        if deeper_source is not _MISSING and deeper_source is not None:
            deeper = _feature_contract_text(deeper_source)
            if nested and deeper and nested != deeper:
                raise FeatureBoundaryError("feature_contract_version envelope contradicts nested observation")
            nested = nested or deeper
    if envelope and nested and envelope != nested:
        raise FeatureBoundaryError("feature_contract_version envelope contradicts nested observation")
    if nested:
        return nested
    if envelope:
        return envelope
    if isinstance(observation, WorldObservation):
        return observation.feature_contract_version
    return ""


def _feature_maps(
    observation: object,
    *,
    include_context: bool = False,
) -> tuple[Mapping[object, object], Mapping[object, object]]:
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
        categories = {key: value for key, value in source.items() if _normalise_key(key) not in _STRUCTURAL_KEYS}

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
    if include_context:
        extra_categorical, extra_numeric = _context_feature_maps(source)
        nested_observation = _read_field(observation, "observation")
        if not extra_categorical and not extra_numeric and nested_observation is not _MISSING:
            extra_categorical, extra_numeric = _context_feature_maps(nested_observation)
        for key, value in extra_categorical.items():
            categorical_map.setdefault(key, value)
        for key, value in extra_numeric.items():
            numeric_map.setdefault(key, value)
    extra_graph_categorical, extra_graph_numeric = _graph_feature_maps(source)
    nested_observation = _read_field(observation, "observation")
    if (
        not extra_graph_categorical
        and not extra_graph_numeric
        and nested_observation is not _MISSING
        and nested_observation is not None
    ):
        extra_graph_categorical, extra_graph_numeric = _graph_feature_maps(nested_observation)
    for key, value in extra_graph_categorical.items():
        categorical_map.setdefault(key, value)
    for key, value in extra_graph_numeric.items():
        numeric_map.setdefault(key, value)
    return categorical_map, numeric_map


def _bucket(value: float, thresholds: Sequence[float]) -> str:
    for index, threshold in enumerate(thresholds):
        if value < threshold:
            return f"b{index}"
    return f"b{len(thresholds)}"


def build_feature_state(
    observation: object,
    *,
    allowed_categorical: frozenset[str] | None = None,
    allowed_numeric: frozenset[str] | None = None,
    include_context: bool = False,
    coarse_features: frozenset[str] | None = None,
    feature_contract: WorldFeatureContract | None = None,
    feature_mask: WorldFeatureMask | None = None,
) -> FeatureState:
    """Return an immutable allow-listed World-state projection.

    Unknown keys are deliberately ignored; forbidden control/critic/target keys
    raise.  This lets a WorldEpisode retain provenance annotations without
    accidentally growing the model feature surface.  Market callers must keep the
    default allow-lists so ``FEATURE_CONTRACT_FINGERPRINT`` stays frozen.
    ``include_context`` selects context features; cohort lanes pass a frozen
    ``WorldFeatureContract`` + ``WorldFeatureMask`` instead.
    """

    if (feature_contract is None) ^ (feature_mask is None):
        raise ValueError("feature_contract and feature_mask must be provided together")
    if feature_contract is not None and feature_mask is not None:
        if allowed_categorical is not None or allowed_numeric is not None or coarse_features is not None:
            raise ValueError("mask-driven encoding cannot also take explicit allow-lists")
        profile = resolve_encoder_profile(feature_contract, feature_mask)
        allowed_cats = profile.allowed_categorical
        allowed_nums = profile.allowed_numeric
        coarse = profile.coarse_features
        include_context = profile.include_context
    elif include_context:
        allowed_cats = allowed_categorical or CONTEXT_CATEGORICAL_FEATURES
        allowed_nums = allowed_numeric or CONTEXT_NUMERIC_FEATURES
        coarse = coarse_features or CONTEXT_COARSE_FEATURES
    else:
        allowed_cats = allowed_categorical or ALLOWED_CATEGORICAL_FEATURES
        allowed_nums = allowed_numeric or ALLOWED_NUMERIC_FEATURES
        coarse = coarse_features or _COARSE_FEATURES
    known_keys = (
        allowed_cats
        | allowed_nums
        | frozenset(bucket_key for bucket_key, _thresholds in _NUMERIC_BUCKETS.values())
        | ALLOWED_CONTEXT_CATEGORICAL_FEATURES
        | ALLOWED_CONTEXT_NUMERIC_FEATURES
        | GRAPH_STATUS_CATEGORICAL_FEATURES
        | GRAPH_CONTENT_CATEGORICAL_FEATURES
    )
    pull_context = include_context or (
        feature_contract is not None and feature_contract.contract_id == GRAPH_FEATURE_CONTRACT_ID
    )
    categorical, numeric = _feature_maps(observation, include_context=pull_context)
    _assert_no_forbidden_keys(categorical, path="categorical_features", known_feature_keys=known_keys)
    _assert_no_forbidden_keys(numeric, path="numeric_features", known_feature_keys=known_keys)

    canonical: dict[str, str] = {}
    selected_categorical: dict[str, str] = {}
    selected_numeric: dict[str, float] = {}
    for raw_key, raw_value in categorical.items():
        key = _normalise_key(raw_key)
        if key not in allowed_cats:
            continue
        value = _normalise_scalar(raw_value)
        existing = canonical.get(key)
        if existing is not None and existing != value:
            raise FeatureBoundaryError(f"conflicting values for World feature {key!r}")
        canonical[key] = value
        selected_categorical[key] = value

    for raw_key, raw_value in numeric.items():
        key = _normalise_key(raw_key)
        if key not in allowed_nums:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise FeatureBoundaryError(f"numeric World feature {key!r} must be a finite number")
        value = float(raw_value)
        if not math.isfinite(value):
            raise FeatureBoundaryError(f"numeric World feature {key!r} must be finite")
        existing_numeric = selected_numeric.get(key)
        if existing_numeric is not None and existing_numeric != value:
            raise FeatureBoundaryError(f"conflicting values for World feature {key!r}")
        selected_numeric[key] = value
        if key not in _NUMERIC_BUCKETS:
            continue
        bucket_key, thresholds = _NUMERIC_BUCKETS[key]
        bucket_value = _bucket(value, thresholds)
        existing = canonical.get(bucket_key)
        if existing is not None and existing != bucket_value:
            raise FeatureBoundaryError(f"conflicting values for World feature {bucket_key!r}")
        canonical[bucket_key] = bucket_value

    canonical_items = tuple(sorted(canonical.items()))
    coarse_items = tuple(item for item in canonical_items if item[0] in coarse)
    canonical_json = json.dumps(dict(canonical_items), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return FeatureState(
        exact_state=canonical_items,
        coarse_state=coarse_items,
        features=MappingProxyType(dict(canonical_items)),
        feature_hash=sha256(canonical_json.encode("utf-8")).hexdigest(),
        categorical_values=MappingProxyType(dict(sorted(selected_categorical.items()))),
        numeric_values=MappingProxyType(dict(sorted(selected_numeric.items()))),
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
    """Return only the current DOWN/FLAT/UP classes."""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped not in OUTCOME_CLASSES:
            raise ValueError(f"unsupported World outcome class: {value!r}")
        return stripped
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


def _text_or_none(value: object) -> str | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def observation_market_anchor(observation: object) -> tuple[str, str, str, str] | None:
    """Return ``(venue, symbol, bar_interval, as_of_bar_ts)`` independent of context payload."""

    source = observation
    if isinstance(observation, WorldEpisode):
        source = observation.observation
    else:
        nested = _read_field(source, "observation")
        if nested is not _MISSING and nested is not None:
            source = nested
    venue = _text_or_none(_read_field(source, "venue"))
    symbol = _text_or_none(_read_field(source, "symbol"))
    interval = _text_or_none(_read_field(source, "bar_interval")) or _text_or_none(_read_field(source, "interval"))
    as_of = optional_model_timestamp(_read_field(source, "as_of_bar_ts"), field_name="as_of_bar_ts")
    as_of_iso = iso_utc(as_of)
    if venue is None or symbol is None or interval is None or as_of_iso is None:
        return None
    return (venue, symbol, interval, as_of_iso)


def comparison_batch_id(
    *,
    market_anchor: tuple[str, str, str, str],
    horizon_id: str,
    predicted_at: datetime,
) -> str:
    return "world-comparison-batch:v1:" + canonical_sha256(
        {
            "venue": market_anchor[0],
            "symbol": market_anchor[1],
            "bar_interval": market_anchor[2],
            "as_of_bar_ts": market_anchor[3],
            "horizon_id": horizon_id,
            "predicted_at": iso_utc(predicted_at),
        }
    )


def outcome_simple_return(outcome: object) -> float | None:
    if isinstance(outcome, WorldOutcome):
        return outcome.simple_return
    for name in ("simple_return", "forward_return"):
        raw = _outcome_field(outcome, name)
        if raw is _MISSING or raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        number = float(raw)
        if math.isfinite(number):
            return number
    anchor_close = _outcome_field(outcome, "anchor_close")
    endpoint_close = _outcome_field(outcome, "endpoint_close")
    if (
        isinstance(anchor_close, bool)
        or isinstance(endpoint_close, bool)
        or not isinstance(anchor_close, (int, float))
        or not isinstance(endpoint_close, (int, float))
    ):
        return None
    if not math.isfinite(float(anchor_close)) or not math.isfinite(float(endpoint_close)) or float(anchor_close) <= 0.0:
        return None
    return float(endpoint_close) / float(anchor_close) - 1.0


def canonical_training_label_evidence(outcome: object, horizon_id: str) -> list[object]:
    target_at = _text_or_none(_outcome_field(outcome, "target_at"))
    source = _text_or_none(_outcome_field(outcome, "source_raw_sha256")) or _text_or_none(
        _outcome_field(outcome, "source")
    )
    status = _text_or_none(_outcome_field(outcome, "status"))
    eligible = _outcome_field(outcome, "training_eligible")
    if eligible is _MISSING:
        eligible = None
    direction: str | None
    try:
        direction = outcome_move_class(outcome)
    except (TypeError, ValueError):
        direction = None
    return [
        target_at,
        source,
        horizon_id,
        status,
        eligible,
        direction,
        outcome_simple_return(outcome),
    ]


def training_event_signature(
    *,
    market_anchor: tuple[str, str, str, str],
    horizon_id: str,
    label_evidence: Sequence[object],
    sequence_market_anchors: Sequence[tuple[str, str, str, str]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "market_anchor": list(market_anchor),
        "horizon_id": horizon_id,
        "label_evidence": list(label_evidence),
    }
    if sequence_market_anchors is not None:
        payload["sequence_market_anchors"] = [list(item) for item in sequence_market_anchors]
    return canonical_sha256(payload)


def comparison_cohort_fingerprint(event_signatures: Sequence[str]) -> str:
    # Online updates do not commute: keep the supplied training order.
    return "world-comparison-cohort:v1:" + canonical_sha256({"training_events": list(event_signatures)})


def comparison_lineage(
    observation: object,
    *,
    horizon_id: str,
    predicted_at: datetime,
    event_signatures: Sequence[str],
) -> tuple[str | None, str | None]:
    anchor = observation_market_anchor(observation)
    cohort = comparison_cohort_fingerprint(event_signatures)
    if anchor is None:
        return None, cohort
    return (
        comparison_batch_id(market_anchor=anchor, horizon_id=horizon_id, predicted_at=predicted_at),
        cohort,
    )


def common_training_replay_key(
    outcome: object,
    episode: object | None = None,
) -> tuple[datetime, str, str, str, str, str, str]:
    """Lane-independent replay order for capability training updates."""

    available_at: datetime | None = None
    for name in ("label_available_at", "available_at", "sealed_at"):
        raw = _outcome_field(outcome, name)
        if raw is _MISSING or raw is None or raw == "":
            continue
        available_at = optional_model_timestamp(raw, field_name=name)
        if available_at is not None:
            break
    if available_at is None:
        available_at = datetime.max.replace(tzinfo=timezone.utc)

    anchor = None
    for source in (episode, outcome):
        if source is None:
            continue
        anchor = observation_market_anchor(source)
        if anchor is not None:
            break
    if anchor is None:
        venue = _text_or_none(_read_field(outcome, "episode_venue", "venue")) or ""
        symbol = _text_or_none(_read_field(outcome, "episode_symbol", "symbol")) or ""
        interval = _text_or_none(_read_field(outcome, "episode_bar_interval", "bar_interval")) or ""
        as_of_raw = _read_field(outcome, "episode_as_of_bar_ts", "as_of_bar_ts")
        as_of = ""
        if as_of_raw is not _MISSING and as_of_raw not in (None, ""):
            parsed = optional_model_timestamp(as_of_raw, field_name="as_of_bar_ts")
            as_of = iso_utc(parsed) or ""
        anchor = (venue, symbol, interval, as_of)

    horizon_raw = outcome_horizon_id(outcome)
    horizon_id = "" if horizon_raw is _MISSING or horizon_raw is None else str(horizon_raw).strip()
    digest = canonical_sha256(canonical_training_label_evidence(outcome, horizon_id))
    return (available_at, anchor[0], anchor[1], anchor[2], anchor[3], horizon_id, digest)


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
    "CONTEXT_FEATURE_CONTRACT_ID",
    "CONTEXT_CATEGORICAL_FEATURES",
    "CONTEXT_COARSE_FEATURES",
    "CONTEXT_NUMERIC_FEATURES",
    "DIRECTION_BAND",
    "FEATURE_CONTRACT_FINGERPRINT",
    "FEATURE_CONTRACT_FINGERPRINT_CONTEXT",
    "FEATURE_CONTRACT_FINGERPRINT_GRAPH",
    "GRAPH_CATEGORICAL_FEATURES",
    "GRAPH_COARSE_FEATURES",
    "MARKET_FEATURE_CONTRACT_ID",
    "OUTCOME_CLASSES",
    "FeatureBoundaryError",
    "FeatureState",
    "FutureLabelLeakageError",
    "ModelUpdate",
    "OutcomeEventConflictError",
    "WorldEncoderProfile",
    "WorldLaneModelIdentity",
    "bind_cold_lane_identity",
    "bound_encoder_profile",
    "build_feature_state",
    "canonical_move_class",
    "canonical_training_label_evidence",
    "common_training_replay_key",
    "comparison_batch_id",
    "comparison_cohort_fingerprint",
    "comparison_lineage",
    "feature_contract_version_of",
    "iso_utc",
    "move_class_from_simple_return",
    "normalise_horizon_id",
    "observation_market_anchor",
    "optional_model_timestamp",
    "outcome_horizon_id",
    "outcome_move_class",
    "outcome_simple_return",
    "parse_model_timestamp",
    "resolve_encoder_profile",
    "revalidate_context_observation",
    "revalidate_graph_observation",
    "training_event_signature",
    "world_lane_encoder_profile",
]
