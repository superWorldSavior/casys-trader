"""Bounded first-seen OHLCV journal and derived automatic-shadow artifacts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from trader.application.world_model.dynamics_ports import DynamicsJournalResult
from trader.domain.world_dynamics import ObservedDynamicsBar
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.infrastructure.state_db.shadow import write_json_atomic


_SCHEMA = "world_dynamics_journal.v1"
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dynamics_series_id(series_key: Sequence[str]) -> str:
    """Stable artifact name derived from the domain's exact series identity."""
    return canonical_sha256(list(series_key))


class FilesystemDynamicsJournal:
    """One bounded journal, owned by the single coalescing shadow worker.

    Identical bars keep their first receipt. Later revisions never replace
    admitted evidence. Retention evicts only oldest bars inside pinned scopes.
    """

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], datetime] = _now) -> None:
        self.path = Path(state_dir) / "world_dynamics" / "bars.json"
        self.clock = clock
        self._lock = threading.RLock()

    def _read(self, *, max_series: int, max_bars_per_series: int) -> tuple[ObservedDynamicsBar, ...]:
        if not self.path.exists():
            return ()
        if self.path.stat().st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("dynamics journal exceeds its byte limit")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA:
            raise ValueError("unsupported dynamics journal schema")
        if payload.get("retention") != {"max_series": max_series, "max_bars_per_series": max_bars_per_series}:
            raise ValueError("dynamics journal retention differs from configured bounds")
        records = payload.get("bars")
        if not isinstance(records, list) or len(records) > max_series * max_bars_per_series:
            raise ValueError("dynamics journal record limit exceeded")
        bars = []
        slots = set()
        counts: dict[tuple[str, ...], int] = defaultdict(int)
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("evidence"), dict):
                raise ValueError("invalid dynamics journal record")
            bar = ObservedDynamicsBar.from_dict(record["evidence"])
            if record.get("payload_hash") != bar.payload_hash:
                raise ValueError("dynamics journal evidence hash mismatch")
            slot = (bar.series_key, bar.completed_end_at)
            if slot in slots:
                raise ValueError("duplicate dynamics journal slot")
            slots.add(slot)
            counts[bar.series_key] += 1
            if counts[bar.series_key] > max_bars_per_series or len(counts) > max_series:
                raise ValueError("dynamics journal scope limit exceeded")
            bars.append(bar)
        return tuple(bars)

    def merge(
        self, bars: tuple[ObservedDynamicsBar, ...], *, max_series: int, max_bars_per_series: int,
    ) -> DynamicsJournalResult:
        if type(max_series) is not int or not 1 <= max_series <= 4:
            raise ValueError("max_series must be in [1, 4]")
        if type(max_bars_per_series) is not int or not 2 <= max_bars_per_series <= 256:
            raise ValueError("max_bars_per_series must be in [2, 256]")
        if len(bars) > max_series * max_bars_per_series:
            raise ValueError("capture batch exceeds dynamics journal bounds")
        if any(type(bar) is not ObservedDynamicsBar for bar in bars):
            raise TypeError("journal only accepts real observed dynamics bars")
        with self._lock:
            existing = self._read(max_series=max_series, max_bars_per_series=max_bars_per_series)
            admitted = {bar.series_key for bar in existing}
            slots = {(bar.series_key, bar.completed_end_at): bar for bar in existing}
            new_bars = duplicate_bars = conflicting_bars = 0
            rejected = set()
            record_clock = parse_utc_timestamp(self.clock(), "recorded_at")
            ordered = sorted(bars, key=lambda bar: (bar.series_key, bar.completed_end_at))
            for candidate in ordered:
                if candidate.series_key not in admitted:
                    if len(admitted) >= max_series:
                        rejected.add(candidate.series_key)
                        continue
                    admitted.add(candidate.series_key)
                key = (candidate.series_key, candidate.completed_end_at)
                previous = slots.get(key)
                if previous is not None:
                    if candidate.evidence_id == previous.evidence_id:
                        duplicate_bars += 1
                    else:
                        conflicting_bars += 1
                    continue
                if candidate.first_seen_at > record_clock:
                    raise ValueError("capture clock follows actual journal recording clock")
                slots[key] = replace(candidate, recorded_at=record_clock)
                new_bars += 1
            grouped: dict[tuple[str, ...], list[ObservedDynamicsBar]] = defaultdict(list)
            for bar in slots.values():
                grouped[bar.series_key].append(bar)
            retained = []
            evicted = 0
            for key in sorted(grouped):
                series = sorted(grouped[key], key=lambda bar: bar.completed_end_at)
                evicted += max(0, len(series) - max_bars_per_series)
                retained.extend(series[-max_bars_per_series:])
            if new_bars:
                payload = {
                    "schema_version": _SCHEMA,
                    "retention": {"max_series": max_series, "max_bars_per_series": max_bars_per_series},
                    "bars": [{"evidence": bar.to_dict(), "payload_hash": bar.payload_hash} for bar in retained],
                }
                if len(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")) > _MAX_JOURNAL_BYTES:
                    raise ValueError("dynamics journal serialization exceeds its byte limit")
                write_json_atomic(self.path, payload, durable=True)
            return DynamicsJournalResult(
                bars=tuple(retained), accepted_series=len(grouped), new_bars=new_bars,
                duplicate_bars=duplicate_bars, conflicting_bars=conflicting_bars,
                evicted_bars=evicted, rejected_series=len(rejected),
            )


class FilesystemDynamicsArtifacts:
    """Latest derived reports only; real bar evidence stays in the journal."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.status_path = self.state_dir / "world_dynamics_status.json"
        self.reports_dir = self.state_dir / "world_dynamics" / "reports"

    def write_report(self, series_key: Sequence[str], payload: Mapping[str, object]) -> str:
        path = self.reports_dir / f"{dynamics_series_id(series_key)}.json"
        write_json_atomic(path, dict(payload))
        return str(path.resolve())

    def write_status(self, payload: Mapping[str, object]) -> None:
        write_json_atomic(self.status_path, dict(payload))
