"""Bounded JSONL store for raw runtime learnings."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["LearningsStore", "RawLearningsStore"]


class RawLearningsStore:
    """Machine-owned runtime learnings buffer.

    The daemon appends short notes emitted by the agent at each wake. Recent
    rows are reinjected into the next wake context, while evicted rows are kept
    in an append-only archive for later recall/RAG ingestion.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 200,
        archive_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.max_entries = max_entries
        self.archive_path = (
            Path(archive_path)
            if archive_path is not None
            else self.path.parent / "archive" / f"{self.path.stem}-evicted.jsonl"
        )

    def _read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def append(self, *, symbol: str, note: str, now: datetime | None = None, **extra: object) -> bool:
        """Append one note, returning False for empty, disabled, or duplicate entries."""
        note = note.strip()
        if not note or self.max_entries <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        row = {"ts": now.isoformat(), "symbol": symbol, "note": note, **extra}
        rows = self._read_rows()
        decision_id = row.get("decision_id")
        if isinstance(decision_id, str) and decision_id and any(
            existing.get("decision_id") == decision_id for existing in rows
        ):
            return False
        rows.append(row)
        evicted = rows[: -self.max_entries] if len(rows) > self.max_entries else []
        rows = rows[-self.max_entries :]
        if evicted:
            self._archive_evicted(evicted, now=now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in rows:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        return True

    def _archive_evicted(self, evicted: list[dict], *, now: datetime) -> None:
        """Best-effort append-only archive for rows evicted from the live buffer."""
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as f:
                for item in evicted:
                    f.write(json.dumps({**item, "evicted_at": now.isoformat()}, ensure_ascii=False) + "\n")
        except OSError as exc:
            # The archive must never block writing the live buffer.
            log.warning("archive evicted learnings not written %s (%s)", self.archive_path, exc)

    def recent(self, limit: int = 10) -> list[dict]:
        """Return the latest entries in chronological order."""
        if limit <= 0:
            return []
        return self._read_rows()[-limit:]

    def all(self) -> list[dict]:
        """Return all live-buffer entries in chronological order."""
        return self._read_rows()


LearningsStore = RawLearningsStore
