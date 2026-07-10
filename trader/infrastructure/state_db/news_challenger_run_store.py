"""Append-only observability store for fresh-news challenger runs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class NewsChallengerRunStore:
    """Persist daily run records and one reconstructible latest cache per venue."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()

    def path_for_date(self, date: str) -> Path:
        return self.base_dir / f"{date}.jsonl"

    def latest_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"latest-{_safe_venue(venue)}.json"

    def append(self, record: Mapping[str, Any]) -> None:
        """Append a canonical run row, then atomically refresh its latest cache."""

        payload = dict(record)
        venue = str(payload.get("venue") or "").strip()
        if not venue:
            raise ValueError("news challenger run requires venue")
        date_key = _date_from_as_of(payload.get("as_of"))
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self._lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            day_path = self.path_for_date(date_key)
            needs_separator = _needs_line_separator(day_path)
            with day_path.open("a", encoding="utf-8") as fh:
                if needs_separator:
                    fh.write("\n")
                fh.write(line + "\n")
            _write_json_atomic(self.latest_path_for_venue(venue), payload)

    def read_latest(self, venue: str) -> dict[str, Any] | None:
        """Read a venue cache, falling back to corruption-tolerant archive scan."""

        cached = _read_json_object(self.latest_path_for_venue(venue))
        if cached is not None and cached.get("venue") == venue:
            return cached

        for path in sorted(self.base_dir.glob("????-??-??.jsonl"), reverse=True):
            rows = _read_jsonl_objects(path)
            for row in reversed(rows):
                if row.get("venue") == venue:
                    return row
        return None


def _date_from_as_of(value: Any) -> str:
    as_of = str(value or "").strip()
    if len(as_of) >= 10 and as_of[4] == "-" and as_of[7] == "-":
        return as_of[:10]
    raise ValueError("news challenger run as_of must start with YYYY-MM-DD")


def _safe_venue(venue: str) -> str:
    safe = "".join(char for char in str(venue) if char.isalnum() or char in ("_", "-"))
    return safe or "UNKNOWN"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(payload), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _needs_line_separator(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows
