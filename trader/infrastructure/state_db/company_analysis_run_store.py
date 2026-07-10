"""Append-only analysis-run ledger with a latest projection per symbol."""

from __future__ import annotations

import fcntl
import json
import threading
from datetime import date as date_type
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key
from trader.infrastructure.state_db.shadow import write_json_atomic


class CompanyAnalysisRunStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.latest_dir = self.base_dir / "latest"
        self._lock = threading.RLock()

    def path_for_date(self, date: str) -> Path:
        return self.base_dir / f"{_valid_date(date)}.jsonl"

    def latest_path(self, symbol: str) -> Path:
        return self.latest_dir / f"{symbol_storage_key(symbol)}.json"

    def append(self, record: Mapping[str, Any]) -> dict[str, str]:
        payload = dict(record)
        symbol = _required(payload.get("symbol"), "symbol")
        as_of = _required(payload.get("as_of"), "as_of")
        status = _required(payload.get("status"), "status")
        payload.update({"symbol": symbol, "as_of": as_of, "status": status})
        date_key = _valid_date(as_of[:10])
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        path = self.path_for_date(date_key)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            write_json_atomic(self.latest_path(symbol), payload)
        result = {"symbol": symbol, "as_of": as_of, "status": status}
        run_id = str(payload.get("run_id") or "").strip()
        if run_id:
            result["run_id"] = run_id
        return result

    def read_latest(self, symbol: str) -> dict[str, Any] | None:
        try:
            payload: Any = json.loads(self.latest_path(symbol).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("symbol") == symbol else None


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _valid_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("date must be YYYY-MM-DD")
    return text


__all__ = ["CompanyAnalysisRunStore"]
