"""Application service for verified, export-only prediction partitions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
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
    WorldPredictionArchivePlan,
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
    storage_lease: Callable[[], AbstractContextManager[Any]] | None = None

    def execute(self, *, before: date, apply: bool) -> dict[str, Any]:
        """Plan or publish every closed partition before the exclusive cutoff."""

        if not isinstance(before, date) or isinstance(before, datetime):
            raise TypeError("before must be a date")
        today = _utc_today(self.clock)
        if before > today:
            raise ValueError(
                f"before {before.isoformat()} includes the open UTC day {today.isoformat()} or a future day"
            )
        with _operation_lease(self, apply=apply):
            return self._execute_locked(before=before, apply=apply, today=today)

    def _execute_locked(self, *, before: date, apply: bool, today: date) -> dict[str, Any]:
        catalog = _canonical_catalog(self.source)
        catalog_by_date = {manifest.recorded_date: manifest for manifest in catalog}
        _require_canonical_catalog(catalog, self.sink)
        plans = tuple(self.source.plan_before(before))
        partitions: list[dict[str, Any]] = []
        for plan in plans:
            if plan.recorded_date >= today:
                raise ValueError(
                    f"archive partition {plan.partition_id} includes the open UTC day "
                    f"{today.isoformat()} or a future day"
                )
            canonical = catalog_by_date.get(plan.recorded_date)
            if canonical is not None:
                partitions.append(
                    {
                        "status": "canonical_active",
                        "late_hot_count": plan.row_count,
                        "plan": plan.to_dict(),
                        "manifest": canonical.to_dict(),
                        "source_retained": True,
                        "authority": "shadow_only",
                        "decision_effect": "none",
                    }
                )
                continue
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


def _operation_lease(service: WorldPredictionArchiveService, *, apply: bool) -> AbstractContextManager[Any]:
    if service.storage_lease is not None:
        return service.storage_lease()
    factory = getattr(service.source, "export_storage_lease", None)
    if callable(factory):
        return factory(apply=apply)
    return nullcontext()


def _canonical_catalog(source: WorldPredictionArchiveSource) -> tuple[WorldPredictionArchiveManifest, ...]:
    catalog = getattr(source, "canonical_catalog", None)
    if not callable(catalog):
        return ()
    return tuple(catalog())


def _plan_from_manifest(manifest: WorldPredictionArchiveManifest) -> WorldPredictionArchivePlan:
    return WorldPredictionArchivePlan(
        recorded_date=manifest.recorded_date,
        row_count=manifest.row_count,
        first_key=manifest.first_key,
        last_key=manifest.last_key,
        column_names=manifest.column_names,
        column_types=manifest.column_types,
        schema_sha256=manifest.schema_sha256,
    )


def _require_canonical_catalog(
    catalog: tuple[WorldPredictionArchiveManifest, ...],
    sink: WorldPredictionArchiveSink,
) -> None:
    """Fail closed on an active canonical partition; never quarantine from this path."""

    for manifest in catalog:
        plan = _plan_from_manifest(manifest)
        try:
            existing = sink.published(plan)
        except InvalidWorldPredictionArchiveError as exc:
            raise InvalidWorldPredictionArchiveError(
                f"canonical partition {manifest.recorded_date.isoformat()} unavailable: {exc.reason}"
            ) from exc
        if existing is None:
            raise InvalidWorldPredictionArchiveError(
                f"canonical partition {manifest.recorded_date.isoformat()} is missing"
            )
        if existing.to_dict() != manifest.to_dict():
            raise InvalidWorldPredictionArchiveError(
                f"canonical partition {manifest.recorded_date.isoformat()} does not match the registered manifest"
            )


def _assert_same_partition(expected_rows: int, manifest: WorldPredictionArchiveManifest) -> None:
    if manifest.row_count != expected_rows:
        raise RuntimeError(f"archive manifest row count mismatch: expected {expected_rows}, got {manifest.row_count}")


__all__ = ["WorldPredictionArchiveService"]
