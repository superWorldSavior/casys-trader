"""File-backed append-only store for macro/news situation briefs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    loose_date_from_as_of,
    read_jsonl_objects,
    safe_filename_component,
)
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    append_jsonl_and_fsync,
    build_availability_receipt,
    default_utc_clock,
    fsync_path,
    receipt_dir,
    unwrap_history_payload,
)


class NewsMacroBriefStore:
    """Read/write `state/news_briefs/YYYY-MM-DD.jsonl` plus latest JSONL caches."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        history_path: str | Path | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        # Kept for constructor compatibility with the old replacement-history store.
        self.history_path = Path(history_path) if history_path is not None else None
        self._ledger = JsonlDayLedger(self.base_dir, validate_date=False)
        self._clock = clock or default_utc_clock

    def path_for_date(self, date: str) -> Path:
        return self._ledger.path_for_date(date)

    def latest_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"latest-{_safe_venue(venue)}.jsonl"

    def receipt_path_for_date(self, date: str) -> Path:
        return receipt_dir(self.base_dir) / f"{date}.jsonl"

    def append(self, brief: NewsMacroBrief, *, date: str | None = None) -> dict[str, str]:
        """Append one canonical brief line, then a sidecar availability receipt."""

        date_key = date or loose_date_from_as_of(
            brief.as_of,
            empty_error="brief.as_of must start with YYYY-MM-DD when date is omitted",
            strip=False,
        )
        payload = brief.to_dict()
        history_path = self.path_for_date(date_key)
        with self._ledger.write_session() as session:
            session.append(payload, date=date_key)
            session.project_jsonl(self.latest_path_for_venue(brief.venue), payload)
        fsync_path(history_path)
        ready_at = self._clock()
        receipt = build_availability_receipt(
            artifact_id=brief.brief_id,
            artifact_ref=brief.ref(date=date_key),
            payload=payload,
            history_ref={
                "path": str(history_path.relative_to(self.base_dir)),
                "scope": date_key,
                "encoding": "jsonl",
            },
            ready_at=ready_at,
        )
        append_jsonl_and_fsync(self.receipt_path_for_date(date_key), receipt)
        return brief.ref(date=date_key)

    def write(self, brief: NewsMacroBrief, *, date: str | None = None) -> None:
        """Compatibility façade: writes are append-only, never replacements."""

        self.append(brief, date=date)

    def read(self, date: str, *, venue: str | None = None) -> NewsMacroBrief | None:
        """Return the latest valid-looking brief in a date JSONL file."""

        for brief in reversed(list(self._iter_date(date))):
            if venue is None or brief.venue == venue:
                return brief
        return None

    def read_latest(
        self,
        venue: str,
        *,
        at: datetime | str | None = None,
    ) -> NewsMacroBrief | None:
        """Read the latest brief for a venue, falling back to date-file scan."""

        latest = self._read_single_line(self.latest_path_for_venue(venue))
        if latest is not None and latest.venue == venue and _is_active(latest, at):
            return latest

        for path in sorted(self.base_dir.glob("*.jsonl"), reverse=True):
            if path.name.startswith("latest-"):
                continue
            for brief in reversed(list(self._iter_path(path))):
                if brief.venue == venue and _is_active(brief, at):
                    return brief
        return None

    def active_ref(
        self,
        date_or_venue: str,
        *,
        venue: str | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, str] | None:
        """Return a ref for the active brief.

        Backward compatibility: ``active_ref("2026-07-09")`` returns the latest
        brief on that date. New code should call ``active_ref("EU", at=now)``.
        """

        if venue is not None:
            brief = self.read(date_or_venue, venue=venue)
            return brief.ref(date=date_or_venue) if brief is not None else None

        if _looks_like_date(date_or_venue):
            brief = self.read(date_or_venue)
            return brief.ref(date=date_or_venue) if brief is not None else None

        brief = self.read_latest(date_or_venue, at=at)
        if brief is None:
            return None
        return brief.ref(
            date=loose_date_from_as_of(
                brief.as_of,
                empty_error="brief.as_of must start with YYYY-MM-DD when date is omitted",
                strip=False,
            )
        )

    def _iter_date(self, date: str) -> list[NewsMacroBrief]:
        return list(self._iter_path(self.path_for_date(date)))

    def _iter_path(self, path: Path) -> list[NewsMacroBrief]:
        briefs: list[NewsMacroBrief] = []
        for payload in read_jsonl_objects(path):
            brief = NewsMacroBrief.from_mapping(unwrap_history_payload(payload))
            if brief is not None:
                briefs.append(brief)
        return briefs

    def _read_single_line(self, path: Path) -> NewsMacroBrief | None:
        briefs = self._iter_path(path)
        return briefs[-1] if briefs else None


def _safe_venue(venue: str) -> str:
    return safe_filename_component(str(venue or "GLOBAL"), empty="GLOBAL")


def _looks_like_date(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def _parse_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_active(brief: NewsMacroBrief, at: datetime | str | None) -> bool:
    moment = _parse_dt(at)
    if moment is None:
        return True
    start = _parse_dt(brief.as_of)
    end = _parse_dt(brief.valid_until)
    if start is not None and moment < start:
        return False
    if end is not None and moment >= end:
        return False
    return True
