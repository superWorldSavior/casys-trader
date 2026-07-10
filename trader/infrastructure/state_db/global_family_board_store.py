"""Append-only store for derived cross-venue family boards."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db.shadow import write_json_atomic


class GlobalFamilyBoardStore:
    """Persist changed boards and expose one atomic current projection."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._write_lock = threading.RLock()

    @property
    def current_path(self) -> Path:
        return self.base_dir / "current.json"

    def read_current(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("board_id") else None

    def append_if_changed(
        self,
        record: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        payload = _validated_board(record)
        with self._write_lock:
            current = self.read_current()
            if current is not None and current.get("board_id") == payload["board_id"]:
                return current, _ref(current), False
            self.base_dir.mkdir(parents=True, exist_ok=True)
            day = payload["as_of"][:10]
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with (self.base_dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            write_json_atomic(self.current_path, payload)
        return payload, _ref(payload), True


def _validated_board(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("global family board must be a mapping")
    payload = dict(record)
    for field in ("board_id", "as_of", "status", "role"):
        value = str(payload.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field} must be a non-empty string")
        payload[field] = value
    if len(payload["as_of"]) < 10:
        raise ValueError("as_of must start with YYYY-MM-DD")
    return payload


def _ref(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "board_id": record.get("board_id"),
        "as_of": record.get("as_of"),
        "status": record.get("status"),
    }
