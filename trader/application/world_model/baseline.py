"""Deterministic, shadow-only baseline for exogenous WorldEpisodes.

The baseline predicts a market transition, never a Trader decision.  Feature
encoding, errors, and update DTOs live in
:mod:`trader.application.world_model.encoding` so the GRU challenger can share
them without importing this module's internals.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any

from trader.application.world_model import encoding as world_encoding
from trader.application.world_model.encoding import (
    ALLOWED_CATEGORICAL_FEATURES,
    ALLOWED_NUMERIC_FEATURES,
    CONTEXT_FEATURE_CONTRACT_VERSION,
    CONTEXT_V2_CATEGORICAL_FEATURES,
    CONTEXT_V2_COARSE_FEATURES,
    CONTEXT_V2_NUMERIC_FEATURES,
    FEATURE_CONTRACT_FINGERPRINT,
    FEATURE_CONTRACT_FINGERPRINT_V2,
    MARKET_FEATURE_CONTRACT_VERSION,
    feature_contract_version_of,
    OUTCOME_CLASSES,
    FeatureBoundaryError,
    FeatureState,
    FutureLabelLeakageError,
    ModelUpdate,
    OutcomeEventConflictError,
    WorldLaneModelIdentity,
    bind_cold_lane_identity,
    build_feature_state,
    canonical_training_label_evidence,
    comparison_lineage,
    iso_utc,
    normalise_horizon_id,
    observation_market_anchor,
    optional_model_timestamp,
    outcome_horizon_id,
    outcome_move_class,
    bound_encoder_profile,
    revalidate_context_observation,
    training_event_signature,
    world_encoder_profile_for_include_context,
)
from trader.domain.world_cohort import ModelFamily, WorldLaneDefinition
from trader.domain.world_episode import (
    DEFAULT_WORLD_HORIZONS,
    WorldEpisode,
    WorldPrediction,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureMask


MODEL_ID = "hierarchical_dirichlet_world_baseline"
MODEL_VERSION = "v1"
_MISSING = world_encoding._MISSING
_StateKey = tuple[tuple[str, str], ...]
_DEFAULT_HORIZONS = tuple(item.horizon_id for item in DEFAULT_WORLD_HORIZONS)


@dataclass(frozen=True)
class BaselinePrediction:
    """Compatibility projection around the canonical :class:`WorldPrediction`.

    Live callers should consume :class:`WorldPrediction`.  This record retains
    Markov-specific state keys for older audit payloads.
    """

    world_prediction: WorldPrediction
    run_id: str
    state_key: _StateKey
    coarse_state_key: _StateKey
    feature_contract_fingerprint: str = FEATURE_CONTRACT_FINGERPRINT

    def __getattr__(self, name: str) -> Any:
        return getattr(self.world_prediction, name)

    @property
    def non_authoritative(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        payload = self.world_prediction.to_dict()
        payload.update(
            {
                "feature_contract_fingerprint": self.feature_contract_fingerprint,
                "predicted_at": payload["created_at"],
                "run_id": self.run_id,
                "state_key": list(self.state_key),
                "coarse_state_key": list(self.coarse_state_key),
                "non_authoritative": True,
            }
        )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.world_prediction.to_dict()


@dataclass
class _HorizonCounts:
    exact: dict[_StateKey, Counter[str]] = field(default_factory=dict)
    coarse: dict[_StateKey, Counter[str]] = field(default_factory=dict)
    global_counts: Counter[str] = field(default_factory=Counter)
    training_cutoff: datetime | None = None
    applied_events: dict[str, str] = field(default_factory=dict)
    comparison_event_signatures: list[str] = field(default_factory=list)


def _as_status(value: object) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate).strip().lower()


def _extract_observation_timestamp(observation: object) -> datetime | None:
    source = observation
    for _ in range(2):
        value = world_encoding._read_field(source, "available_at", "as_of_bar_ts", "captured_at", "observed_at")
        parsed = optional_model_timestamp(value, field_name="observation timestamp")
        if parsed is not None:
            return parsed
        nested = world_encoding._read_field(source, "observation")
        if nested is _MISSING or nested is None:
            break
        source = nested
    if isinstance(observation, WorldEpisode):
        return observation.observation.available_at or observation.observation.as_of_bar_ts
    return None


def _extract_episode_id(observation: object) -> str:
    if isinstance(observation, WorldEpisode):
        return observation.episode_id
    source = observation
    for _ in range(2):
        raw = world_encoding._read_field(source, "episode_id", "world_episode_id")
        if raw is not _MISSING and raw is not None:
            value = str(raw).strip()
            if value:
                return value
        nested = world_encoding._read_field(source, "observation")
        if nested is _MISSING or nested is None:
            break
        source = nested
    return "anonymous-observation"


def _predicted_class(probabilities: Mapping[str, float]) -> str:
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
        allowed_horizons: Iterable[str] | None = _DEFAULT_HORIZONS,
        model_id: str | None = None,
        model_version: str | None = None,
        accepted_feature_contracts: frozenset[str] | None = None,
        include_context: bool = False,
        feature_contract: WorldFeatureContract | None = None,
        feature_mask: WorldFeatureMask | None = None,
        seed: int | None = None,
        lane_identity: WorldLaneModelIdentity | None = None,
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
            normalised_horizons = frozenset(normalise_horizon_id(item, None) for item in allowed_horizons)
            if not normalised_horizons:
                raise ValueError("allowed_horizons must not be empty")
        self.alpha = float(alpha)
        self.minimum_global_support = minimum_global_support
        self.minimum_coarse_support = minimum_coarse_support
        self.minimum_exact_support = minimum_exact_support
        self._allowed_horizons = normalised_horizons
        self._horizons: dict[str, _HorizonCounts] = {}
        self.model_id = model_id or MODEL_ID
        self.model_version = model_version or MODEL_VERSION
        self._accepted_feature_contracts = accepted_feature_contracts
        self._profile = bound_encoder_profile(feature_contract, feature_mask, include_context=include_context)
        self._include_context = self._profile.include_context if self._profile is not None else include_context
        self.seed = seed
        self.lane_identity = lane_identity
        self._feature_contract_fingerprint = (
            self._profile.encoder_fingerprint
            if self._profile is not None
            else FEATURE_CONTRACT_FINGERPRINT_V2 if include_context else FEATURE_CONTRACT_FINGERPRINT
        )

    @property
    def feature_contract(self) -> WorldFeatureContract:
        if self._profile is not None:
            return self._profile.contract
        return world_encoder_profile_for_include_context(self._include_context).contract

    @property
    def feature_mask(self) -> WorldFeatureMask:
        if self._profile is not None:
            return self._profile.mask
        return world_encoder_profile_for_include_context(self._include_context).mask

    def accepts_episode(self, episode: object) -> bool:
        version = feature_contract_version_of(episode)
        if self._accepted_feature_contracts is not None:
            return version in self._accepted_feature_contracts
        expected = (
            self._profile.contract.accepted_episode_contract
            if self._profile is not None
            else CONTEXT_FEATURE_CONTRACT_VERSION if self._include_context else MARKET_FEATURE_CONTRACT_VERSION
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
            return build_feature_state(
                observation,
                feature_contract=self._profile.contract,
                feature_mask=self._profile.mask,
            )
        if self._include_context:
            revalidate_context_observation(observation)
            return build_feature_state(
                observation,
                allowed_categorical=CONTEXT_V2_CATEGORICAL_FEATURES,
                allowed_numeric=CONTEXT_V2_NUMERIC_FEATURES,
                include_context=True,
                coarse_features=CONTEXT_V2_COARSE_FEATURES,
            )
        return build_feature_state(observation)

    def reset_for_replay(self) -> None:
        """Clear learned counts before an authoritative active-leaf replay."""

        self._horizons.clear()

    def predict(
        self,
        observation: object,
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> WorldPrediction:
        """Predict from the current state without modifying any counts."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=horizon)
        state = self._feature_state(observation)
        counts = self._horizons.get(horizon_key, _HorizonCounts())
        reference_at = optional_model_timestamp(prediction_at, field_name="prediction_at")
        if reference_at is None:
            reference_at = _extract_observation_timestamp(observation)
        if reference_at is None:
            raise ValueError("prediction_at or observation available_at is required")
        if counts.training_cutoff is not None and counts.training_cutoff > reference_at:
            raise FutureLabelLeakageError(
                "training cutoff is after prediction_at; rebuild a causally earlier model state"
            )

        probabilities, tier, support, exact_support, coarse_support, global_support = self._predict_from_counts(
            counts, state
        )
        status = "warming_up" if global_support < self.minimum_global_support else "shadow_only"
        batch_id, cohort = comparison_lineage(
            observation,
            horizon_id=horizon_key,
            predicted_at=reference_at,
            event_signatures=counts.comparison_event_signatures,
        )
        return WorldPrediction(
            episode_id=_extract_episode_id(observation),
            horizon_id=horizon_key,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_hash=state.feature_hash,
            created_at=reference_at,
            probabilities=MappingProxyType(dict(probabilities)),
            predicted_class=_predicted_class(probabilities),
            status=status,
            tier=tier,
            support=support,
            exact_support=exact_support,
            coarse_support=coarse_support,
            global_support=global_support,
            training_cutoff=counts.training_cutoff,
            model_fingerprint=self.model_fingerprint(horizon_key),
            comparison_batch_id=batch_id,
            comparison_cohort_fingerprint=cohort,
        )

    def predict_audit(
        self,
        observation: object,
        horizon_id: str | None = None,
        *,
        horizon: str | None = None,
        prediction_at: datetime | str | None = None,
    ) -> BaselinePrediction:
        """Return the canonical prediction plus Markov-specific audit keys."""

        prediction = self.predict(
            observation,
            horizon_id,
            horizon=horizon,
            prediction_at=prediction_at,
        )
        state = self._feature_state(observation)
        return BaselinePrediction(
            world_prediction=prediction,
            run_id="world_shadow.v1",
            state_key=state.exact_state,
            coarse_state_key=state.coarse_state,
            feature_contract_fingerprint=self._feature_contract_fingerprint,
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

        probabilities = self.predict(
            observation,
            horizon_id,
            horizon=horizon,
            prediction_at=prediction_at,
        ).probabilities
        return {} if probabilities is None else dict(probabilities)

    def apply_outcome(
        self,
        outcome: object,
        observation: object | None = None,
        *,
        available_through: datetime | str | None = None,
    ) -> ModelUpdate:
        """Apply one causally available, observed, eligible label exactly once."""

        event_id_value = world_encoding._outcome_field(outcome, "outcome_event_id", "event_id", "outcome_id")
        event_id = None if event_id_value is _MISSING else str(event_id_value).strip() or None
        status = _as_status(world_encoding._outcome_field(outcome, "status"))
        if status != "observed":
            return self._no_update("outcome_not_observed", event_id=event_id, horizon_id=None)
        sealed = world_encoding._outcome_field(outcome, "sealed")
        if sealed is False:
            return self._no_update("outcome_not_sealed", event_id=event_id, horizon_id=None)

        eligible = world_encoding._outcome_field(outcome, "training_eligible")
        if eligible is _MISSING:
            episode = world_encoding._read_field(outcome, "episode")
            eligible = world_encoding._read_field(episode, "training_eligible") if episode is not _MISSING else _MISSING
        if eligible is not True:
            return self._no_update("outcome_not_training_eligible", event_id=event_id, horizon_id=None)
        if event_id is None:
            raise ValueError("observed World outcome requires a durable outcome_event_id")

        horizon_value = outcome_horizon_id(outcome)
        horizon_key = self._resolve_horizon(horizon_id=horizon_value, horizon=None)
        outcome_class = outcome_move_class(outcome)
        label_available_at = optional_model_timestamp(
            world_encoding._outcome_field(outcome, "available_at", "label_available_at", "sealed_at"),
            field_name="outcome available_at",
        )
        if label_available_at is None:
            return self._no_update("missing_label_available_at", event_id=event_id, horizon_id=horizon_key)

        cutoff = optional_model_timestamp(available_through, field_name="available_through")
        if cutoff is None:
            cutoff = label_available_at
        if label_available_at > cutoff:
            return self._no_update("label_not_available_at_cutoff", event_id=event_id, horizon_id=horizon_key)

        source_observation = observation
        if source_observation is None:
            source_observation = world_encoding._read_field(outcome, "observation", "episode")
        if source_observation is _MISSING or source_observation is None:
            raise ValueError("observed World outcome requires its immutable WorldObservation")
        if not self.accepts_episode(source_observation):
            return self._no_update(
                "feature_contract_rejected",
                event_id=event_id,
                horizon_id=horizon_key,
            )
        observation_at = _extract_observation_timestamp(source_observation)
        if observation_at is not None and label_available_at <= observation_at:
            return self._no_update("label_not_after_observation", event_id=event_id, horizon_id=horizon_key)
        state = self._feature_state(source_observation)
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
        market_anchor = observation_market_anchor(source_observation)
        if market_anchor is not None:
            counts.comparison_event_signatures.append(
                training_event_signature(
                    market_anchor=market_anchor,
                    horizon_id=horizon_key,
                    label_evidence=canonical_training_label_evidence(outcome, horizon_key),
                )
            )
        if counts.training_cutoff is None or label_available_at > counts.training_cutoff:
            counts.training_cutoff = label_available_at
        return ModelUpdate(
            applied=True,
            reason="applied",
            outcome_event_id=event_id,
            horizon_id=horizon_key,
            global_support=self._support(counts.global_counts),
            training_cutoff=iso_utc(counts.training_cutoff),
            model_fingerprint=self.model_fingerprint(horizon_key),
        )

    def training_cutoff(self, horizon_id: str) -> str | None:
        """Return the latest causal label availability included for one horizon."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        counts = self._horizons.get(horizon_key)
        return None if counts is None else iso_utc(counts.training_cutoff)

    def model_fingerprint(self, horizon_id: str) -> str:
        """Stable fingerprint of one horizon's learned counts and cutoff."""

        horizon_key = self._resolve_horizon(horizon_id=horizon_id, horizon=None)
        counts = self._horizons.get(horizon_key, _HorizonCounts())
        payload = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_contract_fingerprint": self._feature_contract_fingerprint,
            "horizon_id": horizon_key,
            "alpha": self.alpha,
            "minimum_global_support": self.minimum_global_support,
            "minimum_coarse_support": self.minimum_coarse_support,
            "minimum_exact_support": self.minimum_exact_support,
            "training_cutoff": iso_utc(counts.training_cutoff),
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
            first = normalise_horizon_id(horizon_id, self._allowed_horizons)
            second = normalise_horizon_id(horizon, self._allowed_horizons)
            if first != second:
                raise ValueError("horizon_id and horizon disagree")
            return first
        return normalise_horizon_id(horizon_id, self._allowed_horizons)

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
            "label_available_at": iso_utc(label_available_at),
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
            training_cutoff=iso_utc(counts.training_cutoff),
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


WorldBaseline = HierarchicalDirichletWorldBaseline


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


def cold_markov_challenger(
    *,
    lane: WorldLaneDefinition,
    contract: WorldFeatureContract,
    mask: WorldFeatureMask,
    study_cohort_id: str,
    manifest_sha256: str,
    started_event_id: str,
    prototype: HierarchicalDirichletWorldBaseline | None = None,
    **kwargs: object,
) -> HierarchicalDirichletWorldBaseline:
    """Mint a cold Markov lane. Never copies a warm prototype."""

    if lane.model_family is not ModelFamily.MARKOV:
        raise ValueError("cold Markov challenger requires model_family=markov")
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
    model = HierarchicalDirichletWorldBaseline(
        model_id=lane.model_id,
        model_version=lane.model_version,
        feature_contract=contract,
        feature_mask=mask,
        seed=lane.seed,
        lane_identity=identity,
        **kwargs,  # type: ignore[arg-type]
    )
    if model._horizons:
        raise ValueError("cold Markov challenger must start untrained")
    return model


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
    "cold_markov_challenger",
]
