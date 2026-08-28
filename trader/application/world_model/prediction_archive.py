"""Application service for verified, export-only prediction partitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from trader.application.world_model.prediction_archive_ports import (
    WorldPredictionArchiveSink,
    WorldPredictionArchiveSource,
)
from trader.domain.world_prediction_archive import (
    InvalidWorldPredictionArchiveError,
    WorldPredictionArchiveManifest,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_today(clock: Callable[[], datetime]) -> date:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("archive clock must return a timezone-aware UTC datetime")
    return now.astimezone(timezone.utc).date()


@dataclass(frozen=True)
class WorldPredictionArchiveService:
    source: WorldPredictionArchiveSource
    sink: WorldPredictionArchiveSink
    clock: Callable[[], datetime] = field(default=_utc_now)

    def execute(self, *, before: date, apply: bool) -> dict[str, Any]:
        """Plan or publish every closed partition before the exclusive cutoff."""

        if not isinstance(before, date) or isinstance(before, datetime):
            raise TypeError("before must be a date")
        today = _utc_today(self.clock)
        if before > today:
            raise ValueError(
                f"before {before.isoformat()} includes the open UTC day {today.isoformat()} or a future day"
            )
        plans = tuple(self.source.plan_before(before))
        partitions: list[dict[str, Any]] = []
        for plan in plans:
            if plan.recorded_date >= today:
                raise ValueError(
                    f"archive partition {plan.partition_id} includes the open UTC day "
                    f"{today.isoformat()} or a future day"
                )
            try:
                existing = self.sink.published(plan)
            except InvalidWorldPredictionArchiveError as exc:
                if not apply:
                    partitions.append(
                        {
                            "status": "invalid",
                            "reason": exc.reason,
                            "plan": plan.to_dict(),
                            "source_retained": True,
                            "authority": "shadow_only",
                            "decision_effect": "none",
                        }
                    )
                    continue
                quarantine = self.sink.quarantine(plan, reason=exc.reason)
                manifest = self.sink.publish(plan, self.source.iter_partition(plan))
                _assert_same_partition(plan.row_count, manifest)
                partitions.append(
                    {
                        "status": "rebuilt",
                        "manifest": manifest.to_dict(),
                        "quarantine": quarantine,
                    }
                )
                continue
            if existing is not None:
                partitions.append({"status": "already_verified", "manifest": existing.to_dict()})
                continue
            if not apply:
                partitions.append({"status": "planned", "plan": plan.to_dict()})
                continue
            manifest = self.sink.publish(plan, self.source.iter_partition(plan))
            _assert_same_partition(plan.row_count, manifest)
            partitions.append({"status": "verified", "manifest": manifest.to_dict()})
        return {
            "schema_version": "world_prediction_archive_run.v1",
            "mode": "apply" if apply else "dry_run",
            "before": before.isoformat(),
            "partition_count": len(partitions),
            "source_retained": True,
            "authority": "shadow_only",
            "decision_effect": "none",
            "partitions": partitions,
        }


def _assert_same_partition(expected_rows: int, manifest: WorldPredictionArchiveManifest) -> None:
    if manifest.row_count != expected_rows:
        raise RuntimeError(f"archive manifest row count mismatch: expected {expected_rows}, got {manifest.row_count}")


__all__ = ["WorldPredictionArchiveService"]
