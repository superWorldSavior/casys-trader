"""A small causal, online GRU challenger for action-free WorldEpisodes.

This is deliberately a *challenger*, not an execution model.  It sees only
the immutable market observations defined by :mod:`trader.domain.world_episode`
and produces a three-way market-transition forecast (``DOWN``/``FLAT``/``UP``).
It has no import path to the Trader planner, prompt, broker, portfolio,
schedule, decision ledger, or FLAIR/MemRL state.

The model is intentionally small and replayable:

* a fixed, hashed World feature encoder avoids fitting a vocabulary or a
  normalizer on future data;
* every horizon owns a separate GRU and is updated only from labels available
  at its explicit cutoff;
* training is one deterministic BPTT step per durable ``outcome_event_id``;
* the episode registry is an input cache only.  Sequence construction filters
  by each target episode's ``available_at``, so registering a later episode
  cannot change an earlier prediction;
* all predictions remain ``shadow_only`` and ``NO_GO`` permanently.

The runtime/store layer is intentionally not imported here.  A caller may
rebuild an instance by first calling :meth:`observe_episodes` with its
append-only episode ledger and then replaying outcomes in label-availability
order through :meth:`apply_outcome` (or :meth:`replay`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any, TypeVar

import numpy as np

from trader.application.world_model.encoding import (
    CONTEXT_FEATURE_CONTRACT_ID,
    CONTEXT_CATEGORICAL_FEATURES,
    CONTEXT_COARSE_FEATURES,
    CONTEXT_NUMERIC_FEATURES,
    FEATURE_CONTRACT_FINGERPRINT,
    FEATURE_CONTRACT_FINGERPRINT_CONTEXT,
    GRAPH_CATEGORICAL_FEATURES,
    MARKET_FEATURE_CONTRACT_ID,
    FeatureBoundaryError,
    FutureLabelLeakageError,
    ModelUpdate,
    OutcomeEventConflictError,
    WorldLaneModelIdentity,
    bind_cold_lane_identity,
    bound_encoder_profile,
    build_feature_state,
    canonical_training_label_evidence,
    common_training_replay_key,
    comparison_lineage,
    feature_contract_version_of,
    normalise_horizon_id,
    observation_market_anchor,
    outcome_horizon_id,
    outcome_move_class,
    revalidate_context_observation,
    revalidate_graph_observation,
    training_event_signature,
    world_lane_encoder_profile,
)
from trader.application.world_model.hydration_snapshot import SnapshotError, to_jsonable
from trader.domain.world_cohort import ModelFamily, WorldLaneDefinition
from trader.domain.world_episode import (
    SUPPORTED_WORLD_HORIZONS,
    PREDICTION_CLASSES,
    WorldEpisode,
    WorldObservation,
    WorldPrediction,
    canonical_sha256,
    parse_utc_timestamp,
)
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_ID,
    WorldFeatureContract,
    WorldFeatureMask,
)


OUTCOME_CLASSES: tuple[str, str, str] = PREDICTION_CLASSES
MODEL_ID = "online_gru_world_challenger"
MODEL_VERSION = "v1"
ENCODER_VERSION = "world_gru_encoder.v1"
CONTEXT_GRU_ENCODER_IDENTITY = "world_gru_encoder.context.v1"
GRAPH_GRU_ENCODER_IDENTITY = "world_gru_encoder.graph.v1"
DEFAULT_DIRECTION_BAND = 0.005

# These are fixed, reviewed market inputs.  There is no corpus-fitted mean,
# standard deviation, vocabulary, embedding, or hidden Trader context.
_NUMERIC_SCALES: tuple[tuple[str, float], ...] = (
    ("return", 0.10),
    ("atr_pct", 0.10),
    ("range_position", 1.00),
    ("z_score", 5.00),
    ("relative_volume", 5.00),
    ("efficiency_ratio", 1.00),
    ("trend_slope", 0.10),
    ("volatility", 0.10),
    ("ohlc_volatility", 0.10),
    ("realized_volatility", 0.10),
    ("family_consensus", 1.00),
    ("data_age_minutes", 240.00),
    ("open_close_return", 0.10),
    ("high_low_range_pct", 0.10),
)
_CATEGORICAL_KEYS = frozenset(
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
        "source_status",
    }
)
_DEFAULT_HORIZONS = tuple(item.horizon_id for item in SUPPORTED_WORLD_HORIZONS)
_EPISODE_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "event_type",
        "episode_id",
        "observation",
        "training_eligible",
        "training_reason",
        "feature_contract_version",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "venue",
        "symbol",
        "bar_interval",
        "as_of_bar_ts",
        "feature_contract_version",
        "sampling_policy_version",
        "anchor",
        "available_at",
        "captured_at",
        "freshness",
        "categorical_features",
        "numeric_features",
        "context",
        "graph_features",
    }
)
_MISSING = object()


@dataclass
class _HorizonState:
    """Mutable parameters and append-only learning metadata for one horizon."""

    parameters: dict[str, np.ndarray]
    applied_events: dict[str, str] = field(default_factory=dict)
    comparison_event_signatures: list[str] = field(default_factory=list)
    support: int = 0
    training_steps: int = 0
    training_cutoff: datetime | None = None


def _restore_parameters(raw: object) -> dict[str, np.ndarray]:
    # A horizon state without parameters is never valid: first-party saves
    # always carry the full weight set, and names/shapes beyond that are
    # covered by the state checksum plus fingerprint round-trip tests.
    if not isinstance(raw, dict) or not raw:
        raise SnapshotError("snapshot_state_invalid:parameters")
    restored: dict[str, np.ndarray] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise SnapshotError("snapshot_state_invalid:parameters")
        shape = entry.get("shape")
        data = entry.get("data")
        if (
            not isinstance(shape, list)
            or not all(type(dim) is int for dim in shape)
            or data is None
            or isinstance(data, (str, bytes, dict))
        ):
            raise SnapshotError("snapshot_state_invalid:parameters")
        try:
            matrix = np.asarray(data, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("snapshot_state_invalid:parameters") from exc
        if tuple(matrix.shape) != tuple(shape):
            raise SnapshotError("snapshot_state_invalid:parameters")
        restored[name] = matrix
    return restored


@dataclass(frozen=True)
class SequenceMetadata:
    """Audit metadata for one encoded causal sequence.

    ``padded_steps`` is informational only.  Padding is masked in the GRU, so
    a cold-start sequence does not drift hidden state through learned biases.
    """

    episode_ids: tuple[str, ...]
    observed_steps: int
    padded_steps: int
    feature_hash: str


def _normalise_horizon(value: object, allowed: frozenset[str] | None) -> str:
    return normalise_horizon_id(value, allowed)


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return parse_utc_timestamp(value, field_name)
    if isinstance(value, str):
        return parse_utc_timestamp(value, field_name)
    raise ValueError(f"{field_name} must be a timezone-aware datetime or ISO-8601 string")


def _optional_timestamp(value: object, *, field_name: str) -> datetime | None:
    if value is _MISSING or value is None or value == "":
        return None
    return _parse_timestamp(value, field_name=field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


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


def _outcome_horizon(outcome: object) -> object:
    return outcome_horizon_id(outcome)


def _outcome_class(outcome: object) -> str:
    return outcome_move_class(outcome)


def _status(value: object) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate).strip().lower()


def _episode_time(episode: WorldEpisode) -> datetime:
    observation = episode.observation
    return observation.available_at or observation.as_of_bar_ts


def _episode_sort_key(episode: WorldEpisode) -> tuple[datetime, datetime, str]:
    return (_episode_time(episode), episode.observation.as_of_bar_ts, episode.episode_id)


_T = TypeVar("_T")


def _append_preserving_order(items: list[_T], item: _T, key: Callable[[_T], Any]) -> None:
    """Append to a sorted list, re-sorting only when order would break.

    Series arrive in causal order, so the common case is a no-op check;
    out-of-order arrivals (corrections, ad-hoc predicts) fall back to a
    full sort. Same order either way; ties append last, matching a
    stable full sort.
    """
    if items and key(items[-1]) > key(item):
        items.append(item)
        items.sort(key=key)
    else:
        items.append(item)


def _series_key(episode: WorldEpisode) -> tuple[str, str, str, str, str]:
    observation = episode.observation
    return (
        observation.venue,
        observation.symbol,
        observation.bar_interval,
        observation.feature_contract_version,
        observation.sampling_policy_version,
    )


def _coerce_episode(value: object) -> WorldEpisode:
    """Accept only the domain record or an exact action-free replay envelope."""

    if isinstance(value, Mapping):
        feature_contract_version_of(value)
    if isinstance(value, WorldEpisode):
        return value
    if isinstance(value, WorldObservation):
        return WorldEpisode(value)
    if not isinstance(value, Mapping):
        raise TypeError("GRU World challenger requires a WorldEpisode or action-free replay mapping")

    payload = {str(key): item for key, item in value.items()}
    if "observation" in payload:
        unexpected = set(payload).difference(_EPISODE_ENVELOPE_KEYS)
        if unexpected:
            rendered = ", ".join(sorted(unexpected))
            raise FeatureBoundaryError(f"GRU episode envelope contains non-World fields: {rendered}")
        return WorldEpisode.from_dict(payload)

    unexpected = set(payload).difference(_OBSERVATION_KEYS)
    if unexpected:
        rendered = ", ".join(sorted(unexpected))
        raise FeatureBoundaryError(f"GRU observation contains non-World fields: {rendered}")
    return WorldEpisode(WorldObservation.from_dict(payload))


class OnlineGRUWorldChallenger:
    """A deterministic NumPy GRU trained incrementally from causal outcomes.

    The model never emits an executable recommendation.  ``minimum_global_support``
    controls only the audit status exposed on a prediction; even after warm-up,
    it stays a shadow-only ``NO_GO`` challenger.
    """

    model_id = MODEL_ID
    model_version = MODEL_VERSION

    def __init__(
        self,
        *,
        sequence_len: int = 12,
        hidden_size: int = 12,
        learning_rate: float = 0.025,
        gradient_clip: float = 1.0,
        minimum_global_support: int = 40,
        categorical_hash_buckets: int = 16,
        seed: int = 20260822,
        allowed_horizons: Iterable[str] | None = _DEFAULT_HORIZONS,
        model_id: str | None = None,
        model_version: str | None = None,
        encoder_version: str | None = None,
        accepted_feature_contracts: frozenset[str] | None = None,
        include_context: bool = False,
        extra_categorical_keys: frozenset[str] | None = None,
        feature_contract_fingerprint: str | None = None,
        feature_contract: WorldFeatureContract | None = None,
        feature_mask: WorldFeatureMask | None = None,
        lane_identity: WorldLaneModelIdentity | None = None,
    ) -> None:
        for name, value in (
            ("sequence_len", sequence_len),
            ("hidden_size", hidden_size),
            ("minimum_global_support", minimum_global_support),
            ("categorical_hash_buckets", categorical_hash_buckets),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        for name, value in (("learning_rate", learning_rate), ("gradient_clip", gradient_clip)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise ValueError(f"{name} must be a finite positive number")

        normalised_horizons = None
        if allowed_horizons is not None:
            normalised_horizons = frozenset(_normalise_horizon(item, None) for item in allowed_horizons)
            if not normalised_horizons:
                raise ValueError("allowed_horizons must not be empty")

        self.sequence_len = sequence_len
        self.hidden_size = hidden_size
        self.learning_rate = float(learning_rate)
        self.gradient_clip = float(gradient_clip)
        self.minimum_global_support = minimum_global_support
        self.categorical_hash_buckets = categorical_hash_buckets
        self.seed = seed
        self._allowed_horizons = normalised_horizons
        self._states: dict[str, _HorizonState] = {}
        self._episodes: dict[str, WorldEpisode] = {}
        self._series: dict[tuple[str, str, str, str, str], list[str]] = {}
        self.model_id = model_id or MODEL_ID
        self.model_version = model_version or MODEL_VERSION
        self._accepted_feature_contracts = accepted_feature_contracts
        self._profile = bound_encoder_profile(feature_contract, feature_mask, include_context=include_context)
        self._include_context = self._profile.include_context if self._profile is not None else include_context
        self.lane_identity = lane_identity
        extra_keys = extra_categorical_keys or frozenset()
        if self._include_context:
            extra_keys = extra_keys | CONTEXT_CATEGORICAL_FEATURES
        if self._profile is not None and self._profile.contract.contract_id == GRAPH_FEATURE_CONTRACT_ID:
            extra_keys = extra_keys | GRAPH_CATEGORICAL_FEATURES
        categorical_keys = _CATEGORICAL_KEYS | extra_keys
        if self._profile is not None:
            categorical_keys = frozenset(name for name in categorical_keys if name in self._profile.allowed_categorical)
        self._categorical_keys = categorical_keys
        if encoder_version is not None:
            self.encoder_version = encoder_version
        elif self._profile is not None and self._profile.contract.contract_id == GRAPH_FEATURE_CONTRACT_ID:
            self.encoder_version = GRAPH_GRU_ENCODER_IDENTITY
        elif self._include_context:
            self.encoder_version = CONTEXT_GRU_ENCODER_IDENTITY
        else:
            self.encoder_version = ENCODER_VERSION
        self._feature_contract_fingerprint = feature_contract_fingerprint or (
            self._profile.encoder_fingerprint
            if self._profile is not None
            else FEATURE_CONTRACT_FINGERPRINT_CONTEXT
            if self._include_context
            else FEATURE_CONTRACT_FINGERPRINT
        )

    @property
    def feature_contract(self) -> WorldFeatureContract:
        if self._profile is not None:
            return self._profile.contract
        return world_lane_encoder_profile("joint" if self._include_context else "market").contract

    @property
    def feature_mask(self) -> WorldFeatureMask:
        if self._profile is not None:
            return self._profile.mask
        return world_lane_encoder_profile("joint" if self._include_context else "market").mask

    def reset_for_replay(self) -> None:
        """Clear learned state and sequence caches before ledger reconciliation."""

        self._states.clear()
        self._episodes.clear()
        self._series.clear()

    @property
    def snapshot_contract(self) -> dict[str, object]:
        """JSON-stable config the learned weights interpretation depends on.

        Any change forces a replay instead of a restore.  The payload must
        survive a JSON round trip unchanged (lists, never tuples).
        """

        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "encoder_version": self.encoder_version,
            "feature_contract_fingerprint": self._feature_contract_fingerprint,
            "sequence_len": self.sequence_len,
            "hidden_size": self.hidden_size,
            "input_size": self.input_size,
            "categorical_hash_buckets": self.categorical_hash_buckets,
            "categorical_keys": sorted(self._categorical_keys),
            "learning_rate": self.learning_rate,
            "gradient_clip": self.gradient_clip,
            "minimum_global_support": self.minimum_global_support,
            "seed": self.seed,
            "include_context": self._include_context,
            "allowed_horizons": None if self._allowed_horizons is None else sorted(self._allowed_horizons),
            "accepted_feature_contracts": None
            if self._accepted_feature_contracts is None
            else sorted(self._accepted_feature_contracts),
            "lane_identity": None
            if self.lane_identity is None
            else to_jsonable(asdict(self.lane_identity)),
        }

    def snapshot_learned_state(self) -> dict[str, object]:
        """Export learned weights and lineage as JSON-native structures.

        The episode/sequence registries are NOT exported: they rebuild from
        the canonical ledger by re-observing after a restore.
        """

        return {
            horizon: {
                "parameters": {
                    name: {"shape": list(matrix.shape), "data": matrix.tolist()}
                    for name, matrix in sorted(state.parameters.items())
                },
                "applied_events": dict(state.applied_events),
                "comparison_event_signatures": list(state.comparison_event_signatures),
                "support": state.support,
                "training_steps": state.training_steps,
                "training_cutoff": _iso(state.training_cutoff),
            }
            for horizon, state in sorted(self._states.items())
        }

    def restore_learned_state(self, state: dict[str, object]) -> None:
        """Replace learned weights with a snapshot payload.  Strict, no merge.

        Raises :class:`SnapshotError` on any shape mismatch.  The previous
        state is kept when validation fails: the reset happens only after
        the whole payload validated.  Sequence registries are untouched;
        the caller re-observes episodes after restoring.
        """

        if not isinstance(state, dict):
            raise SnapshotError("snapshot_state_invalid:root")
        restored: dict[str, _HorizonState] = {}
        for horizon, raw in state.items():
            if not isinstance(horizon, str) or not isinstance(raw, dict):
                raise SnapshotError("snapshot_state_invalid:horizon")
            support = raw.get("support")
            training_steps = raw.get("training_steps")
            if type(support) is not int or type(training_steps) is not int:
                raise SnapshotError("snapshot_state_invalid:support")
            cutoff = raw.get("training_cutoff")
            if cutoff is not None:
                if not isinstance(cutoff, str):
                    raise SnapshotError("snapshot_state_invalid:training_cutoff")
                try:
                    parsed_cutoff = datetime.fromisoformat(cutoff)
                except ValueError as exc:
                    raise SnapshotError("snapshot_state_invalid:training_cutoff") from exc
                if parsed_cutoff.tzinfo is None:
                    raise SnapshotError("snapshot_state_invalid:training_cutoff")
            else:
                parsed_cutoff = None
            applied = raw.get("applied_events")
            if not isinstance(applied, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in applied.items()
            ):
                raise SnapshotError("snapshot_state_invalid:applied_events")
            signatures = raw.get("comparison_event_signatures")
            if not isinstance(signatures, list) or not all(isinstance(item, str) for item in signatures):
                raise SnapshotError("snapshot_state_invalid:comparison_event_signatures")
            restored[horizon] = _HorizonState(
                parameters=_restore_parameters(raw.get("parameters")),
                applied_events=dict(applied),
                comparison_event_signatures=list(signatures),
                support=support,
                training_steps=training_steps,
                training_cutoff=parsed_cutoff,
            )
        self.reset_for_replay()
        self._states.update(restored)

    def accepts_episode(self, episode: object) -> bool:
        version = feature_contract_version_of(episode)
        if self._accepted_feature_contracts is not None:
            return version in self._accepted_feature_contracts
        expected = (
            self._profile.contract.accepted_episode_contract
            if self._profile is not None
            else CONTEXT_FEATURE_CONTRACT_ID
            if self._include_context
            else MARKET_FEATURE_CONTRACT_ID
        )
        return version == expected

    def _require_accepted(self, episode: object) -> None:
        if not self.accepts_episode(episode):
            raise FeatureBoundaryError("World model rejected an episode outside its feature contract")

    def _feature_state(self, observation: object):
        self._require_accepted(observation)
        if self._profile is not None:
            if self._profile.include_context:
                revalidate_context_observation(observation)
            elif self._profile.contract.contract_id == GRAPH_FEATURE_CONTRACT_ID:
                revalidate_graph_observation(observation)
            return build_feature_state(
                observation,
                feature_contract=self._profile.contract,
                feature_mask=self._profile.mask,
            )
        if self._include_context:
            revalidate_context_observation(observation)
            return build_feature_state(
                observation,
                allowed_categorical=CONTEXT_CATEGORICAL_FEATURES,
                allowed_numeric=CONTEXT_NUMERIC_FEATURES,
                include_context=True,
                coarse_features=CONTEXT_COARSE_FEATURES,
            )
        return build_feature_state(observation)

    @property
    def input_size(self) -> int:
        """Fixed input width; numeric value/missing pairs plus hashed categories."""

        return len(_NUMERIC_SCALES) * 2 + self.categorical_hash_buckets

    def observe_episode(self, episode: WorldEpisode | WorldObservation | Mapping[str, object]) -> str:
        """Register immutable market evidence without changing model weights.

        Calling this for future episodes is safe: :meth:`predict` and
        :meth:`apply_outcome` rebuild their target sequence with an explicit
        point-in-time filter.  A reused episode id with a different payload is
        rejected rather than silently changing history.
        """

        canonical = _coerce_episode(episode)
        self._require_accepted(canonical)
        existing = self._episodes.get(canonical.episode_id)
        if existing is not None:
            if existing.payload_hash != canonical.payload_hash:
                raise ValueError("episode_id already observed with different immutable WorldEpisode content")
            return canonical.episode_id

        # Validate the same safe feature boundary used by the baseline before
        # retaining any vector in the sequence registry.
        self._feature_state(canonical)
        self._episodes[canonical.episode_id] = canonical
        key = _series_key(canonical)
        members = self._series.setdefault(key, [])
        _append_preserving_order(
            members,
            canonical.episode_id,
            lambda identifier: _episode_sort_key(self._episodes[identifier]),
        )
        return canonical.episode_id

    def observe_episodes(self, episodes: Iterable[WorldEpisode | WorldObservation | Mapping[str, object]]) -> int:
        """Register an arbitrary iterable in deterministic point-in-time order."""

        canonical = [_coerce_episode(value) for value in episodes]
        for episode in sorted(canonical, key=_episode_sort_key):
            self.observe_episode(episode)
        return len(canonical)

    def predict(
        self,
        episode: WorldEpisode | WorldObservation | Mapping[str, object],
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> WorldPrediction:
        """Forecast one causal episode sequence without modifying model weights."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=horizon)
        canonical = _coerce_episode(episode)
        self._require_accepted(canonical)
        self.observe_episode(canonical)
        state = self._state_for(horizon_key)

        reference_at = _optional_timestamp(prediction_at, field_name="prediction_at")
        if reference_at is None:
            reference_at = _episode_time(canonical)
        if state.training_cutoff is not None and state.training_cutoff > reference_at:
            raise FutureLabelLeakageError(
                "training cutoff is after prediction_at; rebuild a causally earlier model state"
            )

        matrix, mask, metadata = self._sequence_for(canonical)
        if state.support == 0:
            probabilities = self._uniform_probabilities()
            tier = "uniform"
        else:
            probabilities = self._probabilities(state.parameters, matrix, mask)
            tier = "global"
        status = "warming_up" if state.support < self.minimum_global_support else "shadow_only"
        batch_id, cohort = comparison_lineage(
            canonical,
            horizon_id=horizon_key,
            predicted_at=reference_at,
            event_signatures=state.comparison_event_signatures,
        )
        return WorldPrediction(
            episode_id=canonical.episode_id,
            horizon_id=horizon_key,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_hash=metadata.feature_hash,
            created_at=reference_at,
            probabilities=probabilities,
            status=status,
            tier=tier,
            support=state.support,
            exact_support=0,
            coarse_support=0,
            global_support=state.support,
            training_cutoff=state.training_cutoff,
            model_fingerprint=self.model_fingerprint(horizon_key),
            comparison_batch_id=batch_id,
            comparison_cohort_fingerprint=cohort,
        )

    def predict_proba(
        self,
        episode: WorldEpisode | WorldObservation | Mapping[str, object],
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> dict[str, float]:
        """Return only the shadow probabilities; use :meth:`predict` for provenance."""

        return dict(
            self.predict(
                episode,
                horizon_id,
                horizon=horizon,
                prediction_at=prediction_at,
            ).probabilities
        )

    def apply_outcome(
        self,
        outcome: object,
        episode: WorldEpisode | WorldObservation | Mapping[str, object] | None = None,
        *,
        available_through: datetime | str | None = None,
    ) -> ModelUpdate:
        """Apply exactly one observed, eligible, causally available World label.

        This never learns from pending/missing/unknown outcomes, Trader
        outcomes, or labels that were not available at the requested cutoff.
        The durable outcome event id makes repeated worker/restart replays
        idempotent and makes conflicting replays fail closed.
        """

        event_id_value = _outcome_field(outcome, "outcome_event_id", "event_id", "outcome_id")
        event_id = None if event_id_value is _MISSING else str(event_id_value).strip() or None
        if _status(_outcome_field(outcome, "status")) != "observed":
            return self._no_update("outcome_not_observed", event_id=event_id, horizon_id=None)
        if _outcome_field(outcome, "sealed") is False:
            return self._no_update("outcome_not_sealed", event_id=event_id, horizon_id=None)
        if _outcome_field(outcome, "training_eligible") is not True:
            return self._no_update("outcome_not_training_eligible", event_id=event_id, horizon_id=None)
        if event_id is None:
            raise ValueError("observed World outcome requires a durable outcome_event_id")

        horizon_key = self._resolve_horizon(horizon_id=_outcome_horizon(outcome), horizon=None)
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

        source_episode = episode
        if source_episode is None:
            source_episode = _outcome_field(outcome, "episode", "observation")
        if source_episode is _MISSING or source_episode is None:
            raise ValueError("observed World outcome requires its immutable WorldEpisode")
        canonical = _coerce_episode(source_episode)
        if not self.accepts_episode(canonical):
            return self._no_update(
                "feature_contract_rejected",
                event_id=event_id,
                horizon_id=horizon_key,
            )
        outcome_episode_id = _outcome_field(outcome, "episode_id")
        if outcome_episode_id is not _MISSING and str(outcome_episode_id).strip() != canonical.episode_id:
            raise ValueError("outcome episode_id does not match its immutable WorldEpisode")
        if canonical.training_eligible is not True:
            return self._no_update("episode_not_training_eligible", event_id=event_id, horizon_id=horizon_key)
        observation_at = _episode_time(canonical)
        if label_available_at <= observation_at:
            return self._no_update("label_not_after_observation", event_id=event_id, horizon_id=horizon_key)

        outcome_class = _outcome_class(outcome)
        self.observe_episode(canonical)
        state = self._state_for(horizon_key)
        matrix, mask, metadata = self._sequence_for(canonical)
        signature = self._event_signature(
            horizon_id=horizon_key,
            outcome_class=outcome_class,
            label_available_at=label_available_at,
            metadata=metadata,
        )
        previous_signature = state.applied_events.get(event_id)
        if previous_signature is not None:
            if previous_signature != signature:
                raise OutcomeEventConflictError(
                    f"outcome_event_id {event_id!r} already applied with different World-model content"
                )
            return self._no_update("duplicate_outcome_event", event_id=event_id, horizon_id=horizon_key)
        if state.training_cutoff is not None and label_available_at < state.training_cutoff:
            return self._no_update("out_of_order_label", event_id=event_id, horizon_id=horizon_key)

        target_index = OUTCOME_CLASSES.index(outcome_class)
        self._train_step(state.parameters, matrix, mask, target_index)
        state.applied_events[event_id] = signature
        market_anchor = observation_market_anchor(canonical)
        if market_anchor is not None:
            state.comparison_event_signatures.append(
                training_event_signature(
                    market_anchor=market_anchor,
                    horizon_id=horizon_key,
                    label_evidence=canonical_training_label_evidence(outcome, horizon_key),
                    sequence_market_anchors=self._sequence_market_anchors(metadata),
                )
            )
        state.support += 1
        state.training_steps += 1
        if state.training_cutoff is None or label_available_at > state.training_cutoff:
            state.training_cutoff = label_available_at
        return ModelUpdate(
            applied=True,
            reason="applied",
            outcome_event_id=event_id,
            horizon_id=horizon_key,
            global_support=state.support,
            training_cutoff=_iso(state.training_cutoff),
            # No per-update fingerprint: hashing full horizon state per row
            # is O(history^2) over a replay and no consumer reads this
            # field (fingerprints stay available via model_fingerprint()).
            model_fingerprint=None,
        )

    # Keep the incremental predictor contract familiar to the baseline/runtime.
    learn = apply_outcome
    update = apply_outcome

    def replay(
        self,
        episodes: Iterable[WorldEpisode | WorldObservation | Mapping[str, object]],
        outcomes: Iterable[object],
        *,
        available_through: datetime | str | None = None,
    ) -> tuple[ModelUpdate, ...]:
        """Deterministically rebuild online state from append-only evidence.

        Episode registration is performed first; per-target sequence filtering
        still prevents an observation with a later ``available_at`` from being
        used while training an older target.  Outcomes are replayed in causal
        availability order, then by stable identity for ties.
        """

        accepted: list[WorldEpisode] = []
        for value in episodes:
            canonical = _coerce_episode(value)
            if self.accepts_episode(canonical):
                accepted.append(canonical)
        self.observe_episodes(accepted)
        by_id = {episode.episode_id: episode for episode in accepted}

        def _replay_source(outcome: object) -> WorldEpisode | None:
            episode_id = _outcome_field(outcome, "episode_id")
            if episode_id is _MISSING or episode_id is None:
                return None
            return by_id.get(str(episode_id).strip())

        ordered = sorted(
            list(outcomes),
            key=lambda outcome: common_training_replay_key(outcome, _replay_source(outcome)),
        )
        updates: list[ModelUpdate] = []
        for outcome in ordered:
            episode_id = _outcome_field(outcome, "episode_id")
            source = self._episodes.get(str(episode_id).strip()) if episode_id is not _MISSING else None
            if source is None:
                event_id_value = _outcome_field(outcome, "outcome_event_id", "event_id", "outcome_id")
                event_id = None if event_id_value is _MISSING else str(event_id_value).strip() or None
                updates.append(self._no_update("feature_contract_rejected", event_id=event_id, horizon_id=None))
                continue
            updates.append(self.apply_outcome(outcome, source, available_through=available_through))
        return tuple(updates)

    def training_cutoff(self, horizon_id: str) -> str | None:
        """Return the latest causal label availability used by this horizon."""

        state = self._state_for(self._resolve_horizon(horizon_id=horizon_id, horizon=None))
        return _iso(state.training_cutoff)

    def support(self, horizon_id: str) -> int:
        """Return observed, eligible labels learned for one fixed horizon."""

        state = self._state_for(self._resolve_horizon(horizon_id=horizon_id, horizon=None))
        return state.support

    def training_steps(self, horizon_id: str) -> int:
        """Return successful online BPTT updates for one fixed horizon."""

        state = self._state_for(self._resolve_horizon(horizon_id=horizon_id, horizon=None))
        return state.training_steps

    def diagnostics(self, horizon_id: str) -> Mapping[str, object]:
        """Return compact, action-free introspection for shadow monitoring."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        state = self._state_for(horizon_key)
        return MappingProxyType(
            {
                "model_id": self.model_id,
                "model_version": self.model_version,
                "horizon_id": horizon_key,
                "sequence_len": self.sequence_len,
                "hidden_size": self.hidden_size,
                "input_size": self.input_size,
                "support": state.support,
                "training_steps": state.training_steps,
                "training_cutoff": _iso(state.training_cutoff),
                "status": "warming_up" if state.support < self.minimum_global_support else "shadow_only",
                "recommendation": "NO_GO",
                "authority": "shadow_only",
                "decision_effect": "none",
                "model_fingerprint": self.model_fingerprint(horizon_key),
            }
        )

    def sequence_metadata(
        self,
        episode: WorldEpisode | WorldObservation | Mapping[str, object],
    ) -> SequenceMetadata:
        """Expose the exact causal sequence identity without model inference."""

        canonical = _coerce_episode(episode)
        self.observe_episode(canonical)
        _matrix, _mask, metadata = self._sequence_for(canonical)
        return metadata

    def model_fingerprint(self, horizon_id: str) -> str:
        """Fingerprint model parameters plus immutable online-learning lineage."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        state = self._state_for(horizon_key)
        digest = sha256()
        config = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "encoder_version": self.encoder_version,
            "feature_contract_fingerprint": self._feature_contract_fingerprint,
            "horizon_id": horizon_key,
            "sequence_len": self.sequence_len,
            "hidden_size": self.hidden_size,
            "input_size": self.input_size,
            "categorical_hash_buckets": self.categorical_hash_buckets,
            "learning_rate": self.learning_rate,
            "gradient_clip": self.gradient_clip,
            "minimum_global_support": self.minimum_global_support,
            "seed": self.seed,
            "support": state.support,
            "training_steps": state.training_steps,
            "training_cutoff": _iso(state.training_cutoff),
            "applied_events": sorted(state.applied_events.items()),
        }
        digest.update(json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        for name in sorted(state.parameters):
            value = np.asarray(state.parameters[name], dtype="<f8")
            digest.update(name.encode("ascii"))
            digest.update(str(tuple(value.shape)).encode("ascii"))
            digest.update(value.tobytes(order="C"))
        return digest.hexdigest()

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

    def _state_for(self, horizon_id: str) -> _HorizonState:
        state = self._states.get(horizon_id)
        if state is None:
            state = _HorizonState(parameters=self._initial_parameters(horizon_id))
            self._states[horizon_id] = state
        return state

    def _initial_parameters(self, horizon_id: str) -> dict[str, np.ndarray]:
        """Create a horizon-stable small GRU initialization from a fixed seed."""

        seed_material = f"{self.seed}:{horizon_id}:{self.input_size}:{self.hidden_size}".encode("utf-8")
        horizon_seed = int.from_bytes(sha256(seed_material).digest()[:8], "big", signed=False)
        rng = np.random.default_rng(horizon_seed)
        input_scale = 1.0 / math.sqrt(self.input_size + self.hidden_size)
        recurrent_scale = 1.0 / math.sqrt(2 * self.hidden_size)
        output_scale = 1.0 / math.sqrt(self.hidden_size + len(OUTCOME_CLASSES))

        def normal(shape: tuple[int, ...], scale: float) -> np.ndarray:
            return np.asarray(rng.normal(0.0, scale, size=shape), dtype=np.float64)

        return {
            "Wz": normal((self.input_size, self.hidden_size), input_scale),
            "Uz": normal((self.hidden_size, self.hidden_size), recurrent_scale),
            "bz": np.zeros(self.hidden_size, dtype=np.float64),
            "Wr": normal((self.input_size, self.hidden_size), input_scale),
            "Ur": normal((self.hidden_size, self.hidden_size), recurrent_scale),
            "br": np.zeros(self.hidden_size, dtype=np.float64),
            "Wh": normal((self.input_size, self.hidden_size), input_scale),
            "Uh": normal((self.hidden_size, self.hidden_size), recurrent_scale),
            "bh": np.zeros(self.hidden_size, dtype=np.float64),
            "Wo": normal((self.hidden_size, len(OUTCOME_CLASSES)), output_scale),
            "bo": np.zeros(len(OUTCOME_CLASSES), dtype=np.float64),
        }

    def _sequence_market_anchors(self, metadata: SequenceMetadata) -> tuple[tuple[str, str, str, str], ...]:
        anchors: list[tuple[str, str, str, str]] = []
        for identifier in metadata.episode_ids:
            episode = self._episodes.get(identifier)
            if episode is None:
                continue
            anchor = observation_market_anchor(episode)
            if anchor is not None:
                anchors.append(anchor)
        return tuple(anchors)

    def _sequence_for(self, target: WorldEpisode) -> tuple[np.ndarray, np.ndarray, SequenceMetadata]:
        """Encode at most ``sequence_len`` causally available episodes for target."""

        target_time = _episode_time(target)
        series_key = _series_key(target)
        identifiers = self._series.get(series_key, [])
        candidates = [
            self._episodes[identifier]
            for identifier in identifiers
            if self._episodes[identifier].training_eligible
            and _episode_time(self._episodes[identifier]) <= target_time
            and self._episodes[identifier].observation.as_of_bar_ts <= target.observation.as_of_bar_ts
        ]
        # A target can be predictably represented even before it has an
        # outcome, but an explicitly non-trainable target must not enter a
        # trainable sequence later by accident.
        if target.training_eligible and target.episode_id not in {item.episode_id for item in candidates}:
            _append_preserving_order(candidates, target, _episode_sort_key)
        selected = candidates[-self.sequence_len :]

        matrix = np.zeros((self.sequence_len, self.input_size), dtype=np.float64)
        mask = np.zeros(self.sequence_len, dtype=bool)
        start = self.sequence_len - len(selected)
        for offset, episode in enumerate(selected):
            matrix[start + offset] = self._encode_episode(episode)
            mask[start + offset] = True
        metadata = SequenceMetadata(
            episode_ids=tuple(episode.episode_id for episode in selected),
            observed_steps=len(selected),
            padded_steps=self.sequence_len - len(selected),
            feature_hash=canonical_sha256(
                {
                    "encoder_version": self.encoder_version,
                    "sequence_len": self.sequence_len,
                    "episode_ids": [episode.episode_id for episode in selected],
                    "episode_hashes": [episode.payload_hash for episode in selected],
                }
            ),
        )
        return matrix, mask, metadata

    def _encode_episode(self, episode: WorldEpisode) -> np.ndarray:
        """Encode the masked FeatureState; no fitted preprocessing is allowed."""

        state = self._feature_state(episode)
        vector = np.zeros(self.input_size, dtype=np.float64)
        allowed_numeric = None if self._profile is None else self._profile.allowed_numeric
        for index, (name, scale) in enumerate(_NUMERIC_SCALES):
            if allowed_numeric is not None and name not in allowed_numeric:
                continue
            value = state.numeric_values.get(name)
            if value is None:
                continue
            numeric = float(value)
            if not math.isfinite(numeric):  # domain contract should already reject this
                raise FeatureBoundaryError(f"numeric World feature {name!r} must be finite")
            vector[index * 2] = math.tanh(numeric / scale)
            vector[index * 2 + 1] = 1.0

        categorical = {
            key: value for key, value in state.categorical_values.items() if key in self._categorical_keys and value
        }
        category_offset = len(_NUMERIC_SCALES) * 2
        populated = 0
        for key, value in sorted(categorical.items()):
            token = f"{key}={value}".encode("utf-8")
            digest = sha256(token).digest()
            bucket = int.from_bytes(digest[:8], "big", signed=False) % self.categorical_hash_buckets
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[category_offset + bucket] += sign
            populated += 1
        if populated:
            vector[category_offset:] /= math.sqrt(populated)
        return vector

    @staticmethod
    def _uniform_probabilities() -> dict[str, float]:
        return {label: 1.0 / len(OUTCOME_CLASSES) for label in OUTCOME_CLASSES}

    def _probabilities(
        self,
        parameters: Mapping[str, np.ndarray],
        matrix: np.ndarray,
        mask: np.ndarray,
    ) -> dict[str, float]:
        hidden, _cache = self._forward(parameters, matrix, mask)
        logits = hidden @ parameters["Wo"] + parameters["bo"]
        probabilities = self._softmax(logits)
        return {label: float(probabilities[index]) for index, label in enumerate(OUTCOME_CLASSES)}

    def _train_step(
        self,
        parameters: dict[str, np.ndarray],
        matrix: np.ndarray,
        mask: np.ndarray,
        target_index: int,
    ) -> None:
        """Perform a single cross-entropy BPTT update with global clipping."""

        hidden, cache = self._forward(parameters, matrix, mask)
        logits = hidden @ parameters["Wo"] + parameters["bo"]
        probabilities = self._softmax(logits)
        d_logits = probabilities.copy()
        d_logits[target_index] -= 1.0
        gradients = {name: np.zeros_like(value) for name, value in parameters.items()}
        gradients["Wo"] += np.outer(hidden, d_logits)
        gradients["bo"] += d_logits
        d_hidden = d_logits @ parameters["Wo"].T

        # ``cache`` contains None for left padding.  Those steps preserve
        # hidden state and therefore contribute neither a gate gradient nor a
        # learned-bias drift at cold start.
        for item in reversed(cache):
            if item is None:
                continue
            x, h_prev, z, r, candidate = item
            d_candidate = d_hidden * (1.0 - z)
            d_update = d_hidden * (h_prev - candidate)
            d_prev = d_hidden * z

            d_candidate_pre = d_candidate * (1.0 - candidate * candidate)
            gradients["Wh"] += np.outer(x, d_candidate_pre)
            gradients["Uh"] += np.outer(r * h_prev, d_candidate_pre)
            gradients["bh"] += d_candidate_pre
            d_reset_times_prev = d_candidate_pre @ parameters["Uh"].T
            d_reset = d_reset_times_prev * h_prev
            d_prev += d_reset_times_prev * r

            d_reset_pre = d_reset * r * (1.0 - r)
            gradients["Wr"] += np.outer(x, d_reset_pre)
            gradients["Ur"] += np.outer(h_prev, d_reset_pre)
            gradients["br"] += d_reset_pre
            d_prev += d_reset_pre @ parameters["Ur"].T

            d_update_pre = d_update * z * (1.0 - z)
            gradients["Wz"] += np.outer(x, d_update_pre)
            gradients["Uz"] += np.outer(h_prev, d_update_pre)
            gradients["bz"] += d_update_pre
            d_prev += d_update_pre @ parameters["Uz"].T
            d_hidden = d_prev

        squared_norm = sum(float(np.sum(gradient * gradient)) for gradient in gradients.values())
        global_norm = math.sqrt(squared_norm)
        if not math.isfinite(global_norm):
            raise FloatingPointError("non-finite GRU gradient")
        scale = 1.0 if global_norm <= self.gradient_clip else self.gradient_clip / global_norm
        updated = {
            name: parameters[name] - self.learning_rate * scale * gradient for name, gradient in gradients.items()
        }
        for value in updated.values():
            if not np.all(np.isfinite(value)):
                raise FloatingPointError("non-finite GRU parameter after update")
        parameters.update(updated)

    def _forward(
        self,
        parameters: Mapping[str, np.ndarray],
        matrix: np.ndarray,
        mask: np.ndarray,
    ) -> tuple[np.ndarray, list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None]]:
        hidden = np.zeros(self.hidden_size, dtype=np.float64)
        cache: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None] = []
        for x, valid in zip(matrix, mask, strict=True):
            if not bool(valid):
                cache.append(None)
                continue
            previous = hidden
            update = self._sigmoid(x @ parameters["Wz"] + previous @ parameters["Uz"] + parameters["bz"])
            reset = self._sigmoid(x @ parameters["Wr"] + previous @ parameters["Ur"] + parameters["br"])
            candidate = np.tanh(x @ parameters["Wh"] + (reset * previous) @ parameters["Uh"] + parameters["bh"])
            hidden = (1.0 - update) * candidate + update * previous
            cache.append((x, previous, update, reset, candidate))
        return hidden, cache

    @staticmethod
    def _sigmoid(value: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(value, -50.0, 50.0)))

    @staticmethod
    def _softmax(logits: np.ndarray) -> np.ndarray:
        shifted = logits - np.max(logits)
        exponentials = np.exp(shifted)
        return exponentials / np.sum(exponentials)

    def _event_signature(
        self,
        *,
        horizon_id: str,
        outcome_class: str,
        label_available_at: datetime,
        metadata: SequenceMetadata,
    ) -> str:
        return canonical_sha256(
            {
                "horizon_id": horizon_id,
                "outcome_class": outcome_class,
                "label_available_at": _iso(label_available_at),
                "feature_hash": metadata.feature_hash,
                "episode_ids": list(metadata.episode_ids),
            }
        )

    def _no_update(self, reason: str, *, event_id: str | None, horizon_id: str | None) -> ModelUpdate:
        if horizon_id is None:
            return ModelUpdate(False, reason, event_id, None, 0, None, None)
        state = self._state_for(horizon_id)
        return ModelUpdate(
            applied=False,
            reason=reason,
            outcome_event_id=event_id,
            horizon_id=horizon_id,
            global_support=state.support,
            training_cutoff=_iso(state.training_cutoff),
            # Same as apply path: per-update fingerprints are never read.
            model_fingerprint=None,
        )


# A concise name for future runtime composition code.  It deliberately does
# not claim to replace the baseline or acquire any decision authority.
WorldGRUChallenger = OnlineGRUWorldChallenger


def _assert_lane_contract_mask(
    lane: WorldLaneDefinition,
    contract: WorldFeatureContract,
    mask: WorldFeatureMask,
) -> None:
    mask.assert_compatible_with(contract)
    if lane.feature_contract_id != contract.contract_id or lane.feature_contract_fingerprint != contract.fingerprint:
        raise ValueError("lane feature_contract fingerprint does not match WorldFeatureContract")
    if lane.feature_mask_id != mask.mask_id or lane.feature_mask_fingerprint != mask.fingerprint:
        raise ValueError("lane feature_mask fingerprint does not match WorldFeatureMask")


def cold_gru_challenger(
    *,
    lane: WorldLaneDefinition,
    contract: WorldFeatureContract,
    mask: WorldFeatureMask,
    study_cohort_id: str,
    manifest_sha256: str,
    started_event_id: str,
    prototype: OnlineGRUWorldChallenger | None = None,
    **kwargs: object,
) -> OnlineGRUWorldChallenger:
    """Mint a cold GRU lane. Never copies a warm prototype."""

    if lane.model_family is not ModelFamily.GRU:
        raise ValueError("cold GRU challenger requires model_family=gru")
    if lane.sequence_length is None:
        raise ValueError("sequence_length is required for gru")
    _assert_lane_contract_mask(lane, contract, mask)
    identity = bind_cold_lane_identity(
        lane_id=lane.lane_id,
        model_id=lane.model_id,
        model_version=lane.model_version,
        seed=lane.seed,
        sequence_length=lane.sequence_length,
        study_cohort_id=study_cohort_id,
        manifest_sha256=manifest_sha256,
        started_event_id=started_event_id,
        prototype=prototype,
    )
    sequence_len = kwargs.pop("sequence_len", None)
    if sequence_len is not None and sequence_len != lane.sequence_length:
        raise ValueError("sequence_len disagrees with WorldLaneDefinition")
    seed = kwargs.pop("seed", None)
    if seed is not None and seed != lane.seed:
        raise ValueError("seed disagrees with WorldLaneDefinition")
    model = OnlineGRUWorldChallenger(
        sequence_len=lane.sequence_length,
        seed=lane.seed,
        model_id=lane.model_id,
        model_version=lane.model_version,
        feature_contract=contract,
        feature_mask=mask,
        lane_identity=identity,
        **kwargs,  # type: ignore[arg-type]
    )
    if any(state.support or state.training_steps for state in model._states.values()):
        raise ValueError("cold GRU challenger must start untrained")
    return model


__all__ = [
    "CONTEXT_GRU_ENCODER_IDENTITY",
    "DEFAULT_DIRECTION_BAND",
    "ENCODER_VERSION",
    "GRAPH_GRU_ENCODER_IDENTITY",
    "MODEL_ID",
    "MODEL_VERSION",
    "OUTCOME_CLASSES",
    "OnlineGRUWorldChallenger",
    "SequenceMetadata",
    "WorldGRUChallenger",
    "cold_gru_challenger",
]
