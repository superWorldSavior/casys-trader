"""Lifecycle objects for immutable World Model prediction archives.

Parquet is a cold analytical projection.  Until a composite hot/cold ledger is
introduced, ``world_model.db`` remains the canonical append-only authority and
an archive manifest never authorises deletion from SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping

PREDICTION_ARCHIVE_SCHEMA = "world-shadow-predictions.parquet.v1"
PREDICTION_ARCHIVE_QUARANTINE_SCHEMA = "world_prediction_archive_quarantine.v1"
PREDICTION_ARCHIVE_AUTHORITY = "shadow_only"
PREDICTION_ARCHIVE_DECISION_EFFECT = "none"


class PredictionArchiveState(StrEnum):
    PLANNED = "planned"
    VERIFIED = "verified"
    QUARANTINED = "quarantined"


class InvalidWorldPredictionArchiveError(RuntimeError):
    """A derived Parquet partition exists but is not a verified SQLite mirror."""

    def __init__(self, reason: str) -> None:
        self.reason = _required_text(reason, "reason")
        super().__init__(self.reason)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256(value: object, field: str) -> str:
    rendered = _required_text(value, field)
    if not rendered.startswith("sha256:") or len(rendered) != 71:
        raise ValueError(f"{field} must be a sha256:<64 hex> digest")
    try:
        int(rendered[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a sha256:<64 hex> digest") from exc
    return rendered.lower()


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class WorldPredictionArchivePlan:
    """One closed UTC-day partition eligible for an export-only projection."""

    recorded_date: date
    row_count: int
    first_key: tuple[str, str]
    last_key: tuple[str, str]
    column_names: tuple[str, ...]
    column_types: tuple[str, ...]
    schema_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.recorded_date, date) or isinstance(self.recorded_date, datetime):
            raise TypeError("recorded_date must be a date")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or self.row_count < 1:
            raise ValueError("row_count must be >= 1")
        for name, key in (("first_key", self.first_key), ("last_key", self.last_key)):
            if len(key) != 2 or any(not isinstance(item, str) or not item for item in key):
                raise ValueError(f"{name} must contain recorded_at and prediction_id")
        if self.first_key > self.last_key:
            raise ValueError("first_key must not be after last_key")
        if not self.column_names or len(set(self.column_names)) != len(self.column_names):
            raise ValueError("column_names must be non-empty and unique")
        if len(self.column_types) != len(self.column_names) or any(
            not isinstance(item, str) or not item for item in self.column_types
        ):
            raise ValueError("column_types must match column_names")
        object.__setattr__(self, "schema_sha256", _sha256(self.schema_sha256, "schema_sha256"))

    @property
    def partition_id(self) -> str:
        return self.recorded_date.isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_date": self.recorded_date.isoformat(),
            "row_count": self.row_count,
            "first_key": list(self.first_key),
            "last_key": list(self.last_key),
            "column_names": list(self.column_names),
            "column_types": list(self.column_types),
            "schema_sha256": self.schema_sha256,
        }


@dataclass(frozen=True)
class WorldPredictionArchiveManifest:
    """Proof that one Parquet partition round-tripped against its SQLite source."""

    schema_version: str
    state: PredictionArchiveState
    recorded_date: date
    relative_path: str
    row_count: int
    first_key: tuple[str, str]
    last_key: tuple[str, str]
    column_names: tuple[str, ...]
    column_types: tuple[str, ...]
    schema_sha256: str
    content_sha256: str
    parquet_sha256: str
    parquet_bytes: int
    compression: str
    producer: str
    verified_at: datetime
    authority: str = PREDICTION_ARCHIVE_AUTHORITY
    decision_effect: str = PREDICTION_ARCHIVE_DECISION_EFFECT
    source_retained: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != PREDICTION_ARCHIVE_SCHEMA:
            raise ValueError(f"schema_version must be {PREDICTION_ARCHIVE_SCHEMA}")
        if self.state is not PredictionArchiveState.VERIFIED:
            raise ValueError("published archive manifest must be verified")
        if self.authority != PREDICTION_ARCHIVE_AUTHORITY:
            raise ValueError("archive authority must remain shadow_only")
        if self.decision_effect != PREDICTION_ARCHIVE_DECISION_EFFECT:
            raise ValueError("archive decision_effect must remain none")
        if self.source_retained is not True:
            raise ValueError("export-only manifests must retain the SQLite source")
        plan = WorldPredictionArchivePlan(
            recorded_date=self.recorded_date,
            row_count=self.row_count,
            first_key=self.first_key,
            last_key=self.last_key,
            column_names=self.column_names,
            column_types=self.column_types,
            schema_sha256=self.schema_sha256,
        )
        relative = PurePosixPath(_required_text(self.relative_path, "relative_path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("relative_path must stay inside the archive root")
        object.__setattr__(self, "relative_path", str(relative))
        object.__setattr__(self, "schema_sha256", plan.schema_sha256)
        object.__setattr__(self, "content_sha256", _sha256(self.content_sha256, "content_sha256"))
        object.__setattr__(self, "parquet_sha256", _sha256(self.parquet_sha256, "parquet_sha256"))
        if isinstance(self.parquet_bytes, bool) or not isinstance(self.parquet_bytes, int) or self.parquet_bytes < 1:
            raise ValueError("parquet_bytes must be >= 1")
        object.__setattr__(self, "compression", _required_text(self.compression, "compression").lower())
        object.__setattr__(self, "producer", _required_text(self.producer, "producer"))
        object.__setattr__(self, "verified_at", _utc(self.verified_at, "verified_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "recorded_date": self.recorded_date.isoformat(),
            "relative_path": self.relative_path,
            "row_count": self.row_count,
            "first_key": list(self.first_key),
            "last_key": list(self.last_key),
            "column_names": list(self.column_names),
            "column_types": list(self.column_types),
            "schema_sha256": self.schema_sha256,
            "content_sha256": self.content_sha256,
            "parquet_sha256": self.parquet_sha256,
            "parquet_bytes": self.parquet_bytes,
            "compression": self.compression,
            "producer": self.producer,
            "verified_at": self.verified_at.isoformat(),
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "source_retained": self.source_retained,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> WorldPredictionArchiveManifest:
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            state=PredictionArchiveState(str(payload.get("state") or "")),
            recorded_date=date.fromisoformat(str(payload.get("recorded_date") or "")),
            relative_path=str(payload.get("relative_path") or ""),
            row_count=int(payload.get("row_count") or 0),
            first_key=tuple(payload.get("first_key") or ()),  # type: ignore[arg-type]
            last_key=tuple(payload.get("last_key") or ()),  # type: ignore[arg-type]
            column_names=tuple(payload.get("column_names") or ()),  # type: ignore[arg-type]
            column_types=tuple(payload.get("column_types") or ()),  # type: ignore[arg-type]
            schema_sha256=str(payload.get("schema_sha256") or ""),
            content_sha256=str(payload.get("content_sha256") or ""),
            parquet_sha256=str(payload.get("parquet_sha256") or ""),
            parquet_bytes=int(payload.get("parquet_bytes") or 0),
            compression=str(payload.get("compression") or ""),
            producer=str(payload.get("producer") or ""),
            verified_at=datetime.fromisoformat(str(payload.get("verified_at") or "")),
            authority=str(payload.get("authority") or ""),
            decision_effect=str(payload.get("decision_effect") or ""),
            source_retained=payload.get("source_retained") is True,
        )

    def semantic_identity(self) -> tuple[object, ...]:
        return (
            self.schema_version,
            self.recorded_date,
            self.relative_path,
            self.row_count,
            self.first_key,
            self.last_key,
            self.column_names,
            self.column_types,
            self.schema_sha256,
            self.content_sha256,
        )


@dataclass(frozen=True)
class WorldPredictionArchiveQuarantine:
    """Forensic record for a damaged derived Parquet partition that was moved, not deleted."""

    schema_version: str
    state: PredictionArchiveState
    recorded_date: date
    relative_path: str
    original_relative_path: str
    reason: str
    detected_at: datetime
    authority: str = PREDICTION_ARCHIVE_AUTHORITY
    decision_effect: str = PREDICTION_ARCHIVE_DECISION_EFFECT
    source_retained: bool = True
    rebuild_from: str = "sqlite"

    def __post_init__(self) -> None:
        if self.schema_version != PREDICTION_ARCHIVE_QUARANTINE_SCHEMA:
            raise ValueError(f"schema_version must be {PREDICTION_ARCHIVE_QUARANTINE_SCHEMA}")
        if self.state is not PredictionArchiveState.QUARANTINED:
            raise ValueError("quarantine record must be quarantined")
        if self.authority != PREDICTION_ARCHIVE_AUTHORITY:
            raise ValueError("archive authority must remain shadow_only")
        if self.decision_effect != PREDICTION_ARCHIVE_DECISION_EFFECT:
            raise ValueError("archive decision_effect must remain none")
        if self.source_retained is not True:
            raise ValueError("export-only quarantines must retain the SQLite source")
        if self.rebuild_from != "sqlite":
            raise ValueError("damaged mirrors rebuild from canonical SQLite")
        for name in ("relative_path", "original_relative_path"):
            relative = PurePosixPath(_required_text(getattr(self, name), name))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"{name} must stay inside the archive root")
            object.__setattr__(self, name, str(relative))
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        object.__setattr__(self, "detected_at", _utc(self.detected_at, "detected_at"))
        if not isinstance(self.recorded_date, date) or isinstance(self.recorded_date, datetime):
            raise TypeError("recorded_date must be a date")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "recorded_date": self.recorded_date.isoformat(),
            "relative_path": self.relative_path,
            "original_relative_path": self.original_relative_path,
            "reason": self.reason,
            "detected_at": self.detected_at.isoformat(),
            "authority": self.authority,
            "decision_effect": self.decision_effect,
            "source_retained": self.source_retained,
            "rebuild_from": self.rebuild_from,
        }


__all__ = [
    "InvalidWorldPredictionArchiveError",
    "PREDICTION_ARCHIVE_AUTHORITY",
    "PREDICTION_ARCHIVE_DECISION_EFFECT",
    "PREDICTION_ARCHIVE_QUARANTINE_SCHEMA",
    "PREDICTION_ARCHIVE_SCHEMA",
    "PredictionArchiveState",
    "WorldPredictionArchiveManifest",
    "WorldPredictionArchivePlan",
    "WorldPredictionArchiveQuarantine",
]
