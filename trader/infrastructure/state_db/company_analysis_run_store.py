"""Append-only company-analysis attempts with usable latest projections.

The latest projection is deliberately a *last usable brief* projection, not a
last event pointer: a transient error is exposed beneath ``latest_failure``
without hiding the report that was good immediately before it.
"""

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
        """Append a material attempt and refresh its symbol projection safely.

        Errors never replace a usable ``success``/legacy ``unchanged`` report;
        they are retained as ``latest_failure``. A later real success replaces
        the projection and clears that marker. Repeated unchanged scanner
        observations with the same report input are ignored: they add no
        operator signal and previously produced a high-volume JSONL storm.
        """

        payload = dict(record)
        symbol = _required(payload.get("symbol"), "symbol")
        as_of = _required(payload.get("as_of"), "as_of")
        status = _required(payload.get("status"), "status")
        payload.update({"symbol": symbol, "as_of": as_of, "status": status})
        date_key = _valid_date(as_of[:10])
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        path = self.path_for_date(date_key)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        result = {"symbol": symbol, "as_of": as_of, "status": status}
        run_id = str(payload.get("run_id") or "").strip()
        if run_id:
            result["run_id"] = run_id

        with self._lock, path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                latest_path = self.latest_path(symbol)
                latest = _read_json_object(latest_path)
                if _is_redundant_unchanged(payload, latest):
                    return result
                handle.write(line + "\n")
                handle.flush()

                if _is_failure(payload) and _is_usable_success(latest):
                    # Preserve the report object byte-for-byte in shape and
                    # replace only the warning with the newest failed attempt.
                    write_json_atomic(latest_path, {**latest, "latest_failure": payload})
                elif status == "unchanged" and _is_usable_success(latest):
                    # A material observation remains in append-only history,
                    # but never displaces the last actual report nor clears an
                    # unresolved failure marker.
                    pass
                else:
                    # A success is a genuine recovery and clears any former
                    # latest_failure. If no usable report existed yet, retain
                    # the first event so the failure/observation is visible.
                    write_json_atomic(latest_path, payload)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return result

    def read_latest(self, symbol: str) -> dict[str, Any] | None:
        try:
            payload: Any = json.loads(self.latest_path(symbol).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("symbol") == symbol else None


def _is_usable_success(value: Mapping[str, Any] | None) -> bool:
    return bool(value) and str(value.get("status") or "").strip() in {"success", "unchanged"}


def _is_failure(value: Mapping[str, Any]) -> bool:
    return str(value.get("status") or "").strip() in {"error", "invalid"}


def _is_redundant_unchanged(
    payload: Mapping[str, Any], latest: Mapping[str, Any] | None
) -> bool:
    """Ignore only same-symbol, same-input unchanged observations.

    ``as_of`` and trigger naturally vary at each daemon tick, so neither is a
    material differentiator. Require a real signature to avoid deduplicating
    malformed or legacy records that carry no provenance.
    """

    if str(payload.get("status") or "").strip() != "unchanged" or not latest:
        return False
    signature = str(payload.get("input_signature") or "").strip()
    if not signature:
        return False
    return all(
        latest.get(field) == payload.get(field)
        for field in ("symbol", "input_signature", "depth", "brief_ref")
    )


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
