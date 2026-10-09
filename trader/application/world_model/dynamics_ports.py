"""Consumer-owned ports for bounded, action-free dynamics experiments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, Sequence

import numpy as np

from trader.domain.world_dynamics import ObservedDynamicsBar, SimulatedBar
from trader.domain.world_episode import AnchorBar

if TYPE_CHECKING:
    from trader.application.world_model.dynamics_workflow import DynamicsCycleResult


@dataclass(frozen=True)
class DynamicsJournalResult:
    """First-known durable evidence retained within the pinned journal bounds."""

    bars: tuple[ObservedDynamicsBar, ...]
    accepted_series: int
    new_bars: int
    duplicate_bars: int
    conflicting_bars: int
    evicted_bars: int
    rejected_series: int


class DynamicsJournal(Protocol):
    """Persists actual retrieval clocks before evidence becomes trainable.

    Capture candidates use their retrieval clock as a provisional recorded_at.
    This port assigns the actual durable recording clock, retains the original
    clocks on identical retries, and returns only verified persisted evidence.
    An empty merge recovers pinned series without minting availability clocks.
    """

    def merge(
        self, bars: tuple[ObservedDynamicsBar, ...], *,
        max_series: int, max_bars_per_series: int,
    ) -> DynamicsJournalResult: ...


class DynamicsPublisher(Protocol):
    """Publishes the latest cycle using an injected reporting projection."""

    def publish(self, publication: DynamicsCycleResult) -> None: ...


class OHLCVDynamicsModel(Protocol):
    """Learns real targets; simulation never enters the learning stream."""

    model_id: str
    support: int
    residual_support: int

    def update(self, history: Sequence[AnchorBar], target: AnchorBar) -> None: ...

    def sample_next(
        self, history: Sequence[AnchorBar | SimulatedBar], rng: np.random.Generator,
        *, end_at: datetime, step_index: int,
    ) -> SimulatedBar: ...

    def fingerprint(self) -> str: ...

    def diagnostics(self) -> dict[str, object]: ...
