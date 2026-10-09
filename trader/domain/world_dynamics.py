"""Market-only transition evidence and explicitly simulated OHLCV paths.

Real observations retain their canonical WorldEpisode identity. Simulations
use separate value types and schemas, so no rollout step is a market episode,
an availability receipt, or a three-class WorldPrediction. This module is
stdlib-only and owns geometry, clocks, and immutable provenance only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    bar_timestamp_on_canonical_grid,
    canonical_sha256,
    is_eligible_completed_bar,
    parse_bar_interval,
    parse_utc_timestamp,
)


WORLD_DYNAMICS_TARGET_CONTRACT_VERSION = "next_completed_bar_ohlcv.v1"
NEXT_BAR_TRANSITION_SCHEMA_VERSION = "world_next_bar_transition.v1"
SIMULATED_BAR_SCHEMA_VERSION = "world_simulated_bar.v1"
WORLD_TRAJECTORY_SCHEMA_VERSION = "world_trajectory.v1"


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


@dataclass(frozen=True)
class NextBarTransition:
    """An observed next-bar target, with no future features in its projection."""

    source: EpisodeEvidence
    target: EpisodeEvidence

    def __post_init__(self) -> None:
        if type(self.source) is not EpisodeEvidence or type(self.target) is not EpisodeEvidence:
            raise TypeError("source and target must be EpisodeEvidence")
        for evidence in (self.source, self.target):
            episode = evidence.episode
            observation = episode.observation
            if not episode.training_eligible:
                raise ValueError("transition requires training-eligible real episodes")
            if observation.feature_contract_version != MARKET_FEATURE_CONTRACT_ID:
                raise ValueError("transition requires the market-only feature contract")
            if observation.completed_bar_end_at is None or not is_eligible_completed_bar(
                ts=observation.as_of_bar_ts,
                bar_interval=observation.bar_interval,
                timestamp_semantics=observation.anchor.timestamp_semantics,
                end_at=observation.completed_bar_end_at,
                available_at=observation.available_at,
            ):
                raise ValueError("transition requires canonical completed bars")
        source = self.source.episode.observation
        target = self.target.episode.observation
        for name in ("venue", "symbol", "bar_interval", "feature_contract_version", "sampling_policy_version"):
            if getattr(source, name) != getattr(target, name):
                raise ValueError(f"transition {name} must match")
        for name in ("source", "timestamp_semantics"):
            if getattr(source.anchor, name) != getattr(target.anchor, name):
                raise ValueError(f"transition anchor.{name} must match")
        duration = _positive_interval(source.bar_interval)
        if target.completed_bar_end_at != source.completed_bar_end_at + duration:
            raise ValueError("transition requires exactly adjacent completed bars")
        if self.source.effective_available_at > self.target.effective_available_at:
            raise ValueError("source evidence must be known by target availability")

    @property
    def label_available_at(self) -> datetime:
        return self.target.effective_available_at

    @property
    def transition_id(self) -> str:
        return "world-next-bar-transition:v1:" + canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NEXT_BAR_TRANSITION_SCHEMA_VERSION,
            "target_contract_version": WORLD_DYNAMICS_TARGET_CONTRACT_VERSION,
            "source_episode_id": self.source.episode.episode_id,
            "source_episode_hash": self.source.episode.payload_hash,
            "source_available_at": _iso(self.source.effective_available_at),
            "target_episode_id": self.target.episode.episode_id,
            "target_episode_hash": self.target.episode.payload_hash,
            "target_bar": self.target.episode.observation.anchor.to_dict(),
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

    origin_episode_id: str
    origin_episode_hash: str
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
        for name in ("origin_episode_id", "origin_episode_hash", "symbol", "venue", "bar_interval", "model_id", "model_fingerprint"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
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
        object.__setattr__(self, "bars", bars)

    @property
    def trajectory_id(self) -> str:
        return "world-trajectory:v1:" + canonical_sha256(self.to_dict())

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
            "origin_episode_id": self.origin_episode_id,
            "origin_episode_hash": self.origin_episode_hash,
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
    "NextBarTransition",
    "SimulatedBar",
    "WorldTrajectory",
    "WORLD_DYNAMICS_TARGET_CONTRACT_VERSION",
    "WORLD_TRAJECTORY_SCHEMA_VERSION",
]
