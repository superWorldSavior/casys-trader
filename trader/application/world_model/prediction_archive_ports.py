"""Ports for the export-only World prediction archive lifecycle."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from trader.domain.world_prediction_archive import (
    WorldPredictionArchiveManifest,
    WorldPredictionArchivePlan,
)

PredictionRow = tuple[object, ...]


class WorldPredictionArchiveSource(Protocol):
    def plan_before(self, before: date) -> Sequence[WorldPredictionArchivePlan]: ...

    def iter_partition(self, plan: WorldPredictionArchivePlan) -> Iterator[PredictionRow]: ...


class WorldPredictionArchiveSink(Protocol):
    def published(self, plan: WorldPredictionArchivePlan) -> WorldPredictionArchiveManifest | None: ...

    def publish(
        self,
        plan: WorldPredictionArchivePlan,
        rows: Iterator[PredictionRow],
    ) -> WorldPredictionArchiveManifest: ...

    def quarantine(self, plan: WorldPredictionArchivePlan, *, reason: str) -> Mapping[str, Any]: ...


__all__ = [
    "PredictionRow",
    "WorldPredictionArchiveSink",
    "WorldPredictionArchiveSource",
]
