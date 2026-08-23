"""Consumer-owned ports for the shadow World Model use-case.

These contracts live next to the application service that consumes them.  The
daemon remains the composition root: it injects the store, models, labeler,
and bar adapter.  Reflective alias discovery is not part of the live path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trader.application.world_model.encoding import ModelUpdate
from trader.domain.world_episode import WorldEpisode, WorldOutcome, WorldPrediction


class WorldModelLedger(Protocol):
    """Append-only ledger of episodes, outcomes, and shadow predictions."""

    def append_episode(self, episode: WorldEpisode | Mapping[str, object]) -> bool: ...

    def append_prediction(self, prediction: WorldPrediction | Mapping[str, object]) -> bool: ...

    def append_outcome_event(self, outcome: WorldOutcome | Mapping[str, object]) -> bool: ...

    def get_episode(self, episode_id: str) -> Mapping[str, object] | None: ...

    def list_eligible_episodes(self) -> Sequence[Mapping[str, object]]: ...

    def list_pending_episodes(
        self,
        *,
        horizon_id: str | None = None,
        training_eligible: bool | None = None,
    ) -> Sequence[Mapping[str, object]]: ...

    def list_predictions(self) -> Sequence[Mapping[str, object]]: ...

    def list_observed_outcomes(
        self,
        *,
        active_only: bool = True,
        training_eligible: bool | None = None,
    ) -> Sequence[Mapping[str, object]]: ...

    def list_outcome_events(
        self,
        *,
        active_only: bool = True,
        training_eligible: bool | None = None,
    ) -> Sequence[Mapping[str, object]]: ...


class WorldPredictor(Protocol):
    """Shadow-only market-transition predictor with a stable model identity."""

    model_id: str
    model_version: str

    def predict(
        self,
        episode: object,
        horizon_id: str,
        *,
        prediction_at: datetime | None = None,
    ) -> WorldPrediction: ...

    def apply_outcome(
        self,
        outcome: object,
        episode: object,
        *,
        available_through: datetime | None = None,
    ) -> ModelUpdate: ...

    def accepts_episode(self, episode: object) -> bool: ...


class WorldSequencePredictor(WorldPredictor, Protocol):
    """Predictor that reconstructs a causal sequence from eligible episodes."""

    def observe_episode(self, episode: object) -> object: ...

    def reset_for_replay(self) -> None: ...


class WorldLabeler(Protocol):
    """Fixed-horizon labeler that emits the canonical WorldOutcome live contract."""

    def label_horizon(
        self,
        episode: object,
        bars: object,
        horizon: str,
        *,
        now: datetime,
    ) -> WorldOutcome | Mapping[str, object]: ...


class WorldBarProvider(Protocol):
    """Market-bar provider used only to mature pending World horizons."""

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> object: ...


__all__ = [
    "WorldBarProvider",
    "WorldLabeler",
    "WorldModelLedger",
    "WorldPredictor",
    "WorldSequencePredictor",
]
