"""Append-only store for derived cross-venue family boards."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db._jsonl_store import JsonlDayLedger, read_json_object


class GlobalFamilyBoardStore:
    """Persist changed boards and expose one atomic current projection."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._ledger = JsonlDayLedger(self.base_dir, validate_date=False)

    @property
    def current_path(self) -> Path:
        return self.base_dir / "current.json"

    def read_current(self) -> dict[str, Any] | None:
        payload = read_json_object(self.current_path)
        return payload if payload is not None and payload.get("board_id") else None

    def append_if_changed(
        self,
        record: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        payload = _validated_board(record)
        stored, changed = self._ledger.append_if_changed(
            payload,
            date=payload["as_of"][:10],
            latest_path=self.current_path,
            identity_field="board_id",
            read_current=self.read_current,
        )
        return stored, _ref(stored), changed


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
