"""Append-only company-analysis attempts with usable latest projections.

The latest projection is deliberately a *last usable brief* projection, not a
last event pointer: a transient error is exposed beneath ``latest_failure``
without hiding the report that was good immediately before it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    calendar_date_from_as_of,
    read_json_object,
    required_text,
)
from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key


class CompanyAnalysisRunStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.latest_dir = self.base_dir / "latest"
        self._ledger = JsonlDayLedger(self.base_dir, use_fcntl=True)

    def path_for_date(self, date: str) -> Path:
        return self._ledger.path_for_date(date)

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
        symbol = required_text(payload.get("symbol"), field="symbol")
        as_of = required_text(payload.get("as_of"), field="as_of")
        status = required_text(payload.get("status"), field="status")
        payload.update({"symbol": symbol, "as_of": as_of, "status": status})
        date_key = calendar_date_from_as_of(as_of, empty_error="date must be YYYY-MM-DD")
        result = {"symbol": symbol, "as_of": as_of, "status": status}
        run_id = str(payload.get("run_id") or "").strip()
        if run_id:
            result["run_id"] = run_id

        with self._ledger.write_session(date_key) as session:
            latest_path = self.latest_path(symbol)
            latest = read_json_object(latest_path)
            if _is_redundant_unchanged(payload, latest):
                return result
            session.append(payload, date=date_key)
            if _is_failure(payload) and _is_usable_success(latest):
                # Preserve the report object byte-for-byte in shape and
                # replace only the warning with the newest failed attempt.
                session.project_json(latest_path, {**latest, "latest_failure": payload})
            elif status == "unchanged" and _is_usable_success(latest):
                # A material observation remains in append-only history,
                # but never displaces the last actual report nor clears an
                # unresolved failure marker.
                pass
            else:
                # A success is a genuine recovery and clears any former
                # latest_failure. If no usable report existed yet, retain
                # the first event so the failure/observation is visible.
                session.project_json(latest_path, payload)
        return result

    def read_latest(self, symbol: str) -> dict[str, Any] | None:
        payload = read_json_object(self.latest_path(symbol))
        return payload if payload is not None and payload.get("symbol") == symbol else None


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


__all__ = ["CompanyAnalysisRunStore"]
