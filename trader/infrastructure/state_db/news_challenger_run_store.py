"""Append-only observability store for fresh-news challenger runs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    loose_date_from_as_of,
    read_projection_or_scan,
    safe_filename_component,
)


class NewsChallengerRunStore:
    """Persist daily run records and one reconstructible latest cache per venue."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._ledger = JsonlDayLedger(
            self.base_dir,
            validate_date=False,
            repair_missing_newline=True,
        )

    def path_for_date(self, date: str) -> Path:
        return self._ledger.path_for_date(date)

    def latest_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"latest-{safe_filename_component(venue, empty='UNKNOWN')}.json"

    def append(self, record: Mapping[str, Any]) -> None:
        """Append a canonical run row, then atomically refresh its latest cache."""

        payload = dict(record)
        venue = str(payload.get("venue") or "").strip()
        if not venue:
            raise ValueError("news challenger run requires venue")
        date_key = loose_date_from_as_of(
            payload.get("as_of"),
            empty_error="news challenger run as_of must start with YYYY-MM-DD",
        )

        with self._ledger.write_session() as session:
            session.append(payload, date=date_key)
            session.project_json(self.latest_path_for_venue(venue), payload)

    def read_latest(self, venue: str) -> dict[str, Any] | None:
        """Read a venue cache, falling back to corruption-tolerant archive scan."""

        return read_projection_or_scan(
            self.latest_path_for_venue(venue),
            self.base_dir,
            lambda row: row.get("venue") == venue,
        )
