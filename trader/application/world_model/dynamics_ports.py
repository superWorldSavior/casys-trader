"""Consumer-owned ports for bounded, action-free dynamics experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

import numpy as np

from trader.domain.world_dynamics import SimulatedBar
from trader.domain.world_episode import AnchorBar


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
