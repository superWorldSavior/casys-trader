"""Append-only JSONL history and store-attested receipts for ABOUT artifacts.

Each row is ``{"envelope": ..., "signal": ...}`` per the
``trader.domain.world_knowledge`` row contract: a ``KnowledgeArtifact``
envelope plus the driver bundle frozen at write time. History never rewrites:
same artifact id with same content is a no-op returning the existing receipt;
same id with different content is a conflict. Point-in-time eligibility is
not decided here: readers join attested rows and gate on clocks themselves.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.world_availability import (
    PersistedWorldRef,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    world_subject_content_sha256,
)
from trader.domain.world_company import DriverCompanyBundle
from trader.domain.world_context import KnowledgeArtifact
from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_graph import KnowledgeArtifactRef
from trader.domain.world_knowledge import (
    KNOWLEDGE_ARTIFACT_SUBJECT_KIND,
    knowledge_artifact_content_sha256,
    knowledge_artifact_row,
    knowledge_artifact_scope,
    require_about_signal_kind,
    require_ready_at,
)
from trader.domain.world_news import DriverNewsBundle
from trader.infrastructure.state_db._jsonl_store import read_jsonl_objects
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    WorldAvailabilityJsonlReceiptStore,
    default_utc_clock,
)
from trader.infrastructure.state_db.shadow import write_json_atomic

WORLD_KNOWLEDGE_STORE_ID = "world-knowledge-jsonl.v1"
_STATUS_SCHEMA = "world_knowledge_status.v1"


def _utc(value: datetime, *, field_name: str = "clock") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_day(value: datetime) -> str:
    return _utc(value, field_name="timestamp").date().isoformat()


class WorldKnowledgeStore:
    """JSONL adapter for knowledge-artifact appends backing ABOUT overlays."""

    def __init__(self, root: str | Path, *, clock: UtcClock | None = None) -> None:
        self._root = Path(root)
        self._clock = clock or default_utc_clock
        self._thread_lock = threading.RLock()
        self._receipts = WorldAvailabilityJsonlReceiptStore(
            self._root,
            store_id=WORLD_KNOWLEDGE_STORE_ID,
            clock=self._clock,
        )

    def append_artifact(
        self,
        envelope: KnowledgeArtifact,
        signal: DriverNewsBundle | DriverCompanyBundle,
    ) -> PersistedWorldRef[KnowledgeArtifactRef]:
        if not isinstance(envelope, KnowledgeArtifact):
            raise TypeError("envelope must be KnowledgeArtifact")
        if not isinstance(signal, (DriverNewsBundle, DriverCompanyBundle)):
            raise TypeError("signal must be DriverNewsBundle or DriverCompanyBundle")
        kind = require_about_signal_kind(envelope.kind)
        if kind == "news_macro" and not isinstance(signal, DriverNewsBundle):
            raise TypeError("news_macro artifacts require a DriverNewsBundle signal")
        if kind == "company_intelligence" and not isinstance(signal, DriverCompanyBundle):
            raise TypeError("company_intelligence artifacts require a DriverCompanyBundle signal")
        ready_at = require_ready_at(envelope.ready_at)
        row = knowledge_artifact_row(envelope, signal.to_dict())
        row_hash = knowledge_artifact_content_sha256(row)
        if envelope.content_sha256 != row_hash:
            raise ValueError("envelope content_sha256 does not match the artifact row")
        ref = KnowledgeArtifactRef(artifact_id=envelope.artifact_id, content_sha256=row_hash)
        stored = {"envelope": {k: v for k, v in row["envelope"].items() if k != "content_sha256"}}
        stored["signal"] = row["signal"]
        with self._exclusive_lock():
            existing_row = self._unique_history_row(ref.artifact_id)
            if existing_row is not None:
                if knowledge_artifact_content_sha256(existing_row) != row_hash:
                    raise ValueError("conflict: artifact id already stored with different content")
                payload = existing_row
                existing_envelope = existing_row.get("envelope")
                if not isinstance(existing_envelope, Mapping):
                    raise ValueError("stored artifact row envelope is corrupt")
                day = _utc_day(parse_utc_timestamp(existing_envelope.get("ready_at"), "ready_at"))
            else:
                payload = stored
                day = _utc_day(ready_at)
            receipt = self._persist_subject(
                subject_id=ref.artifact_id,
                payload=payload,
                scope=knowledge_artifact_scope(envelope),
                history_path=f"artifacts/{day}.jsonl",
                receipt_path=f"artifacts/availability_receipts/{day}.jsonl",
            )
            self._write_status()
            return PersistedWorldRef(identity=ref, receipt=receipt)

    def load_corpus(self) -> dict[str, Any]:
        """Attested joins keyed by artifact id. Read-only; see the corpus loader."""

        # Local import: the corpus module imports this store's id (cycle).
        from trader.infrastructure.state_db.world_knowledge_corpus import load_knowledge_corpus

        return dict(load_knowledge_corpus(self._root))

    def _unique_history_row(self, artifact_id: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for row in self._iter_history_rows():
            envelope = row.get("envelope")
            if not isinstance(envelope, Mapping):
                continue
            if str(envelope.get("artifact_id") or "") != artifact_id:
                continue
            if found is not None:
                raise ValueError("multiple history rows for the same artifact_id")
            found = dict(row)
        return found

    def _iter_history_rows(self) -> Iterator[dict[str, Any]]:
        base = self._root / "artifacts"
        if not base.exists():
            return
        for path in sorted(base.glob("????-??-??.jsonl")):
            if not path.is_file():
                continue
            for row in read_jsonl_objects(path):
                if isinstance(row, dict):
                    yield row

    def _persist_subject(
        self,
        *,
        subject_id: str,
        payload: Mapping[str, Any],
        scope: str,
        history_path: str,
        receipt_path: str,
    ) -> WorldAvailabilityReceipt:
        subject = WorldAvailabilitySubjectRef(
            kind=KNOWLEDGE_ARTIFACT_SUBJECT_KIND,
            subject_id=subject_id,
            content_sha256=world_subject_content_sha256(payload),
        )
        persisted = self._receipts.append(
            subject,
            scope=scope,
            payload=payload,
            history_path=history_path,
            receipt_path=receipt_path,
        )
        return persisted.receipt

    def _write_status(self) -> None:
        write_json_atomic(
            self._root / "status.json",
            {"schema_version": _STATUS_SCHEMA, "artifacts": sum(1 for _row in self._iter_history_rows())},
        )

    @contextmanager
    def _exclusive_lock(self):  # type: ignore[no-untyped-def]
        with self._thread_lock:
            self._root.mkdir(parents=True, exist_ok=True)
            lock_path = self._root / ".world-knowledge-store.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "WORLD_KNOWLEDGE_STORE_ID",
    "WorldKnowledgeStore",
]
