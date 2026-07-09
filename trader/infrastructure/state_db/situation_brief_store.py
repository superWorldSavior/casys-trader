"""File-backed store for macro/news situation briefs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.situation import NewsMacroBrief


class NewsMacroBriefStore:
    """Read/write `state/news_briefs/YYYY-MM-DD.json` with replacement history."""

    def __init__(self, base_dir: str | Path, *, history_path: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.history_path = (
            Path(history_path)
            if history_path is not None
            else self.base_dir.parent / "news_briefs-history.jsonl"
        )

    def path_for_date(self, date: str) -> Path:
        return self.base_dir / f"{date}.json"

    def read(self, date: str) -> NewsMacroBrief | None:
        path = self.path_for_date(date)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        return NewsMacroBrief.from_mapping(payload)

    def write(self, brief: NewsMacroBrief, *, date: str | None = None) -> None:
        date_key = date or _date_from_as_of(brief.as_of)
        payload = brief.to_dict()
        path = self.path_for_date(date_key)
        self._archive_replaced(path, replaced_by=brief.ref(date=date_key))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def active_ref(self, date: str) -> dict[str, str] | None:
        brief = self.read(date)
        if brief is None:
            return None
        return brief.ref(date=date)

    def _archive_replaced(self, path: Path, *, replaced_by: dict[str, str]) -> None:
        if not path.exists():
            return
        try:
            previous: Any = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        entry = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "replaced_by": replaced_by,
            "path": str(path.name),
            "payload": previous,
        }
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _date_from_as_of(as_of: str) -> str:
    if len(as_of) >= 10 and as_of[4] == "-" and as_of[7] == "-":
        return as_of[:10]
    raise ValueError("brief.as_of must start with YYYY-MM-DD when date is omitted")
