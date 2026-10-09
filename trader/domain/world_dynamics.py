"""Market-only transition evidence and explicitly simulated OHLCV paths.

Real observations retain their canonical WorldEpisode identity. Simulations
use separate value types and schemas, so no rollout step is a market episode,
an availability receipt, or a three-class WorldPrediction. This module is
stdlib-only and owns geometry, clocks, and immutable provenance only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    AnchorBar,
    bar_timestamp_on_canonical_grid,
    canonical_sha256,
    completed_bar_cutoff,
    is_eligible_completed_bar,
    parse_bar_interval,
    parse_utc_timestamp,
)


WORLD_DYNAMICS_TARGET_CONTRACT_VERSION = "next_completed_bar_ohlcv.v1"
NEXT_BAR_TRANSITION_SCHEMA_VERSION = "world_next_bar_transition.v2"
SIMULATED_BAR_SCHEMA_VERSION = "world_simulated_bar.v1"
WORLD_TRAJECTORY_SCHEMA_VERSION = "world_trajectory.v2"
OBSERVED_DYNAMICS_BAR_SCHEMA_VERSION = "observed_dynamics_bar.v1"
OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION = "world_dynamics.market_bar.v1"
OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION = "first_seen_completed_market_bar.v1"
DYNAMICS_EVIDENCE_KINDS = frozenset({"world_episode", "observed_market_bar"})


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _positive_interval(value: str) -> timedelta:
    duration = parse_bar_interval(value)
    if duration is None:
        raise ValueError("bar_interval must be a supported positive interval")
    return duration


@dataclass(frozen=True)
class EpisodeEvidence:
    """A real episode plus the persistence clock supplied by a verified reader.

    This wrapper does not attest store I/O. The read adapter must verify the
    episode identity, payload hash, and persisted row before constructing it.
    Its effective clock conservatively includes provider, capture, and ledger
    availability; a missing clock is never inferred from the market bar.
    """

    episode: WorldEpisode
    recorded_at: datetime | str

    def __post_init__(self) -> None:
        if type(self.episode) is not WorldEpisode:
            raise TypeError("episode must be a real WorldEpisode")
        observation = self.episode.observation
        if observation.available_at is None or observation.captured_at is None:
            raise ValueError("episode evidence requires available_at and captured_at")
        object.__setattr__(self, "recorded_at", parse_utc_timestamp(self.recorded_at, "recorded_at"))

    @property
    def effective_available_at(self) -> datetime:
        observation = self.episode.observation
        # The constructor establishes the presence of both observation clocks.
        return max(observation.available_at, observation.captured_at, self.recorded_at)

    @property
    def evidence_id(self) -> str:
        return self.episode.episode_id

    @property
    def payload_hash(self) -> str:
        return self.episode.payload_hash

    @property
    def kind(self) -> str:
        return "world_episode"

    @property
    def venue(self) -> str:
        return self.episode.observation.venue

    @property
    def symbol(self) -> str:
        return self.episode.observation.symbol

    @property
    def bar_interval(self) -> str:
        return self.episode.observation.bar_interval

    @property
    def feature_contract_version(self) -> str:
        return self.episode.observation.feature_contract_version

    @property
    def sampling_policy_version(self) -> str:
        return self.episode.observation.sampling_policy_version

    @property
    def anchor(self) -> AnchorBar:
        return self.episode.observation.anchor

    @property
    def training_eligible(self) -> bool:
        return self.episode.training_eligible

    @property
    def completed_end_at(self) -> datetime | None:
        return self.episode.observation.completed_bar_end_at

    @property
    def series_key(self) -> tuple[str, ...]:
        return _dynamics_series_key(self)


@dataclass(frozen=True)
class ObservedDynamicsBar:
    """A historical market bar first known at the current retrieval clock.

    No historical provider availability or feature freshness is inferred.
    Identical geometry keeps its identity; the journal preserves its original
    first-seen receipt instead of minting a fresh receipt on every retrieval.
    A revision to OHLCV evidence has a different identity.
    """

    venue: str
    symbol: str
    bar_interval: str
    anchor: AnchorBar
    first_seen_at: datetime | str
    recorded_at: datetime | str

    def __post_init__(self) -> None:
        for name in ("venue", "symbol", "bar_interval"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if type(self.anchor) is not AnchorBar:
            raise TypeError("observed market bar requires a real AnchorBar")
        for name in ("first_seen_at", "recorded_at"):
            object.__setattr__(self, name, parse_utc_timestamp(getattr(self, name), name))
        end_at = self.completed_end_at
        if end_at is None or not is_eligible_completed_bar(
            ts=self.anchor.ts,
            bar_interval=self.bar_interval,
            timestamp_semantics=self.anchor.timestamp_semantics,
            end_at=end_at,
            available_at=self.first_seen_at,
        ):
            raise ValueError("observed market bar requires a canonical completed bar known by first_seen_at")
        if end_at > self.first_seen_at:
            raise ValueError("observed market bar completion must not follow first_seen_at")
        if self.recorded_at < self.first_seen_at:
            raise ValueError("recorded_at must not precede first_seen_at")

    @property
    def kind(self) -> str:
        return "observed_market_bar"

    @property
    def feature_contract_version(self) -> str:
        return OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION

    @property
    def sampling_policy_version(self) -> str:
        return OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION

    @property
    def training_eligible(self) -> bool:
        return True

    @property
    def completed_end_at(self) -> datetime | None:
        return completed_bar_cutoff(
            as_of_bar_ts=self.anchor.ts,
            timestamp_semantics=self.anchor.timestamp_semantics,
            bar_interval=self.bar_interval,
        )

    @property
    def effective_available_at(self) -> datetime:
        return self.recorded_at

    @property
    def series_key(self) -> tuple[str, ...]:
        return _dynamics_series_key(self)

    @property
    def evidence_id(self) -> str:
        identity = {
            "schema_version": OBSERVED_DYNAMICS_BAR_SCHEMA_VERSION,
            "series_key": self.series_key,
            "anchor": self.anchor.to_dict(),
        }
        return "world-observed-dynamics-bar:v1:" + canonical_sha256(identity)

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVED_DYNAMICS_BAR_SCHEMA_VERSION,
            "kind": self.kind,
            "venue": self.venue,
            "symbol": self.symbol,
            "bar_interval": self.bar_interval,
            "feature_contract_version": self.feature_contract_version,
            "sampling_policy_version": self.sampling_policy_version,
            "anchor": self.anchor.to_dict(),
            "first_seen_at": _iso(self.first_seen_at),
            "recorded_at": _iso(self.recorded_at),
        }

    @property
    def payload_hash(self) -> str:
        return canonical_sha256(self._content_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "evidence_id": self.evidence_id,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ObservedDynamicsBar:
        if not isinstance(payload, Mapping):
            raise TypeError("observed market bar payload must be a mapping")
        expected = {
            "schema_version", "kind", "venue", "symbol", "bar_interval",
            "feature_contract_version", "sampling_policy_version", "anchor",
            "first_seen_at", "recorded_at", "evidence_id", "payload_hash",
        }
        if set(payload) != expected:
            raise ValueError("observed market bar payload must contain exactly its schema fields")
        for name, required in (
            ("schema_version", OBSERVED_DYNAMICS_BAR_SCHEMA_VERSION),
            ("kind", "observed_market_bar"),
            ("feature_contract_version", OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION),
            ("sampling_policy_version", OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION),
        ):
            if payload[name] != required:
                raise ValueError(f"observed market bar {name} must be {required}")
        anchor = payload["anchor"]
        anchor_fields = {"ts", "open", "high", "low", "close", "volume", "source", "timestamp_semantics"}
        if not isinstance(anchor, Mapping) or set(anchor) != anchor_fields:
            raise ValueError("observed market bar anchor must contain exactly the AnchorBar fields")
        evidence = cls(
            venue=payload["venue"], symbol=payload["symbol"], bar_interval=payload["bar_interval"],
            anchor=AnchorBar(**anchor), first_seen_at=payload["first_seen_at"], recorded_at=payload["recorded_at"],
        )
        if payload["evidence_id"] != evidence.evidence_id:
            raise ValueError("observed market bar evidence_id does not match canonical identity")
        if payload["payload_hash"] != evidence.payload_hash:
            raise ValueError("observed market bar payload_hash does not match canonical content")
        return evidence


DynamicsEvidence = EpisodeEvidence | ObservedDynamicsBar


def _dynamics_series_key(evidence: DynamicsEvidence) -> tuple[str, ...]:
    return (
        evidence.venue, evidence.symbol, evidence.bar_interval,
        evidence.feature_contract_version, evidence.sampling_policy_version,
        evidence.anchor.source, evidence.anchor.timestamp_semantics,
    )


@dataclass(frozen=True)
class NextBarTransition:
    """An observed next-bar target, with no future features in its projection."""

    source: DynamicsEvidence
    target: DynamicsEvidence

    def __post_init__(self) -> None:
        allowed_types = (EpisodeEvidence, ObservedDynamicsBar)
        if type(self.source) not in allowed_types or type(self.target) not in allowed_types:
            raise TypeError("source and target must be EpisodeEvidence or ObservedDynamicsBar")
        if type(self.source) is not type(self.target):
            raise TypeError("source and target must have the same evidence type")
        for evidence in (self.source, self.target):
            if not evidence.training_eligible:
                raise ValueError("transition requires training-eligible real market evidence")
            expected_contract = (
                MARKET_FEATURE_CONTRACT_ID if type(evidence) is EpisodeEvidence
                else OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION
            )
            if evidence.feature_contract_version != expected_contract:
                raise ValueError("transition requires the market-only feature contract")
            if evidence.completed_end_at is None or not is_eligible_completed_bar(
                ts=evidence.anchor.ts,
                bar_interval=evidence.bar_interval,
                timestamp_semantics=evidence.anchor.timestamp_semantics,
                end_at=evidence.completed_end_at,
                available_at=evidence.effective_available_at,
            ):
                raise ValueError("transition requires canonical completed bars")
        source, target = self.source, self.target
        for name in ("venue", "symbol", "bar_interval", "feature_contract_version", "sampling_policy_version"):
            if getattr(source, name) != getattr(target, name):
                raise ValueError(f"transition {name} must match")
        for name in ("source", "timestamp_semantics"):
            if getattr(source.anchor, name) != getattr(target.anchor, name):
                raise ValueError(f"transition anchor.{name} must match")
        duration = _positive_interval(source.bar_interval)
        if target.completed_end_at != source.completed_end_at + duration:
            raise ValueError("transition requires exactly adjacent completed bars")
        if self.source.effective_available_at > self.target.effective_available_at:
            raise ValueError("source evidence must be known by target availability")

    @property
    def label_available_at(self) -> datetime:
        return self.target.effective_available_at

    @property
    def transition_id(self) -> str:
        return "world-next-bar-transition:v2:" + canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NEXT_BAR_TRANSITION_SCHEMA_VERSION,
            "target_contract_version": WORLD_DYNAMICS_TARGET_CONTRACT_VERSION,
            "source_evidence_id": self.source.evidence_id,
            "source_evidence_hash": self.source.payload_hash,
            "source_evidence_kind": self.source.kind,
            "source_available_at": _iso(self.source.effective_available_at),
            "target_evidence_id": self.target.evidence_id,
            "target_evidence_hash": self.target.payload_hash,
            "target_evidence_kind": self.target.kind,
            "target_bar": self.target.anchor.to_dict(),
            "label_available_at": _iso(self.label_available_at),
        }


@dataclass(frozen=True)
class SimulatedBar:
    """One imagined completed bar; it has no real-world availability claim."""

    step_index: int
    end_at: datetime | str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
            raise TypeError("step_index must be an integer")
        if self.step_index < 1:
            raise ValueError("step_index must be positive")
        object.__setattr__(self, "end_at", parse_utc_timestamp(self.end_at, "end_at"))
        for name in ("open", "high", "low", "close", "volume"):
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise TypeError(f"simulated {name} must be a finite number")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"simulated {name} must be finite")
            object.__setattr__(self, name, number)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("simulated OHLC prices must be positive")
        if self.volume < 0:
            raise ValueError("simulated volume must be non-negative")
        if self.high < self.low or not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("simulated OHLC geometry must be coherent")

    @property
    def simulated(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SIMULATED_BAR_SCHEMA_VERSION,
            "kind": "simulated_bar",
            "step_index": self.step_index,
            "end_at": _iso(self.end_at),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class WorldTrajectory:
    """A market-only simulation with immutable origin and model provenance."""

    origin_evidence_id: str
    origin_evidence_hash: str
    origin_evidence_kind: str
    symbol: str
    venue: str
    bar_interval: str
    origin_end_at: datetime | str
    prediction_at: datetime | str
    training_cutoff: datetime | str
    model_id: str
    model_fingerprint: str
    seed: int
    bars: tuple[SimulatedBar, ...]
    context_policy: str = "market_only"

    def __post_init__(self) -> None:
        for name in ("origin_evidence_id", "origin_evidence_hash", "symbol", "venue", "bar_interval", "model_id", "model_fingerprint"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.origin_evidence_kind not in DYNAMICS_EVIDENCE_KINDS:
            raise ValueError("origin_evidence_kind must identify real dynamics evidence")
        for name in ("origin_end_at", "prediction_at", "training_cutoff"):
            object.__setattr__(self, name, parse_utc_timestamp(getattr(self, name), name))
        if self.prediction_at < self.origin_end_at:
            raise ValueError("prediction_at must not precede origin completion")
        if self.training_cutoff > self.prediction_at:
            raise ValueError("training_cutoff must not follow prediction_at")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if self.context_policy != "market_only":
            raise ValueError("context_policy must be market_only")
        if not isinstance(self.bars, (list, tuple)) or not self.bars:
            raise ValueError("trajectory requires non-empty simulated bars")
        bars = tuple(self.bars)
        if any(type(bar) is not SimulatedBar for bar in bars):
            raise TypeError("trajectory bars must be SimulatedBar, never real episodes or anchors")
        duration = _positive_interval(self.bar_interval)
        if not bar_timestamp_on_canonical_grid(self.origin_end_at, self.bar_interval):
            raise ValueError("origin completion must be on the canonical bar grid")
        expected_end = self.origin_end_at
        for index, bar in enumerate(bars, start=1):
            expected_end += duration
            if bar.step_index != index or bar.end_at != expected_end:
                raise ValueError("trajectory requires contiguous step indices and completed-bar times")
        if bars[0].end_at <= self.prediction_at:
            raise ValueError("prediction_at must precede the first simulated bar completion")
        object.__setattr__(self, "bars", bars)

    @property
    def trajectory_id(self) -> str:
        return "world-trajectory:v2:" + canonical_sha256(self.to_dict())

    @property
    def authority(self) -> str:
        return "shadow_only"

    @property
    def decision_effect(self) -> str:
        return "none"

    @property
    def recommendation(self) -> str:
        return "NO_GO"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORLD_TRAJECTORY_SCHEMA_VERSION,
            "kind": "simulated_trajectory",
            "target_contract_version": WORLD_DYNAMICS_TARGET_CONTRACT_VERSION,
            "origin_evidence_id": self.origin_evidence_id,
            "origin_evidence_hash": self.origin_evidence_hash,
            "origin_evidence_kind": self.origin_evidence_kind,
            "symbol": self.symbol,
            "venue": self.venue,
            "bar_interval": self.bar_interval,
            "origin_end_at": _iso(self.origin_end_at),
            "prediction_at": _iso(self.prediction_at),
            "training_cutoff": _iso(self.training_cutoff),
            "model_id": self.model_id,
            "model_fingerprint": self.model_fingerprint,
            "seed": self.seed,
            "context_policy": self.context_policy,
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "recommendation": self.recommendation,
            "bars": [bar.to_dict() for bar in self.bars],
        }


__all__ = [
    "EpisodeEvidence",
    "ObservedDynamicsBar",
    "DynamicsEvidence",
    "NextBarTransition",
    "SimulatedBar",
    "WorldTrajectory",
    "WORLD_DYNAMICS_TARGET_CONTRACT_VERSION",
    "WORLD_TRAJECTORY_SCHEMA_VERSION",
    "OBSERVED_DYNAMICS_BAR_SCHEMA_VERSION",
    "OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION",
    "OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION",
]
