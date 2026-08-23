"""Append-only company-intelligence history with atomic current projections."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from trader.domain.company import CompanyIntelligenceBrief
from trader.infrastructure.state_db._jsonl_store import jsonl_dumps, read_json_object
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    append_jsonl_and_fsync,
    build_availability_receipt,
    default_utc_clock,
    load_receipts,
    payload_sha256,
    receipt_dir,
    unwrap_history_payload,
    validate_availability_receipt,
)
from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key
from trader.infrastructure.state_db.shadow import write_json_atomic


def _brief_as_of_ts(brief: CompanyIntelligenceBrief) -> float:
    """Sortable timestamp of a brief's as_of; -inf when unparseable/absent."""
    try:
        parsed = datetime.fromisoformat(str(brief.as_of).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class CompanyIntelligenceStore:
    """Canonical per-symbol history plus reconstructible current projection."""

    def __init__(self, base_dir: str | Path, *, clock: UtcClock | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.history_dir = self.base_dir / "history"
        self.current_dir = self.base_dir / "current"
        self._lock = threading.RLock()
        self._clock = clock or default_utc_clock

    def history_path(self, symbol: str) -> Path:
        return self.history_dir / f"{symbol_storage_key(symbol)}.jsonl"

    def current_path(self, symbol: str) -> Path:
        return self.current_dir / f"{symbol_storage_key(symbol)}.json"

    def receipt_path(self, symbol: str) -> Path:
        return receipt_dir(self.base_dir) / f"{symbol_storage_key(symbol)}.jsonl"

    def append(self, brief: CompanyIntelligenceBrief) -> tuple[dict[str, str], bool]:
        path = self.history_path(brief.symbol)
        payload = brief.to_dict()
        line = jsonl_dumps(payload)
        history_ref = {
            "path": str(path.relative_to(self.base_dir)),
            "scope": brief.symbol,
            "encoding": "jsonl",
        }
        self.history_dir.mkdir(parents=True, exist_ok=True)
        appended = False
        with self._lock, path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw_lines = handle.read().splitlines()
                history = _read_briefs(raw_lines, symbol=brief.symbol)
                exact_payload = _history_contains_payload(raw_lines, payload)
                same_signature = any(
                    candidate.input_signature == brief.input_signature and candidate.depth == brief.depth
                    for candidate in history
                )
                if exact_payload:
                    if not self._has_valid_receipt(brief, payload, history_ref=history_ref):
                        handle.flush()
                        os.fsync(handle.fileno())
                        self._stamp_receipt(brief, payload, history_ref=history_ref)
                    self._write_current_from_history(brief.symbol, history, depth=brief.depth)
                elif same_signature:
                    pass
                else:
                    handle.seek(0, 2)
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    self._stamp_receipt(brief, payload, history_ref=history_ref)
                    self._write_current_from_history(brief.symbol, [*history, brief], depth=brief.depth)
                    appended = True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return brief.ref(), appended

    def _stamp_receipt(
        self,
        brief: CompanyIntelligenceBrief,
        payload: dict[str, Any],
        *,
        history_ref: dict[str, str],
    ) -> None:
        ready_at = self._clock()
        receipt = build_availability_receipt(
            artifact_id=brief.brief_id,
            artifact_ref=brief.ref(),
            payload=payload,
            history_ref=history_ref,
            ready_at=ready_at,
        )
        append_jsonl_and_fsync(self.receipt_path(brief.symbol), receipt)

    def _write_current_from_history(
        self,
        symbol: str,
        history: list[CompanyIntelligenceBrief],
        *,
        depth: str,
    ) -> None:
        matching = [item for item in history if item.depth == depth]
        if not matching:
            return
        latest = matching[-1]
        projection = self._read_current_envelope(symbol)
        briefs = dict(projection.get("briefs") or {})
        briefs[depth] = latest.to_dict()
        write_json_atomic(
            self.current_path(symbol),
            {"schema_version": 1, "symbol": symbol, "briefs": briefs},
        )

    def _has_valid_receipt(
        self,
        brief: CompanyIntelligenceBrief,
        payload: Mapping[str, Any],
        *,
        history_ref: Mapping[str, str],
    ) -> bool:
        for receipt in load_receipts(self.receipt_path(brief.symbol)):
            if (
                validate_availability_receipt(
                    receipt,
                    payload,
                    expected_artifact_id=brief.brief_id,
                    expected_scope=str(history_ref.get("scope") or brief.symbol),
                    expected_history_path=str(history_ref.get("path") or ""),
                )
                is not None
            ):
                return True
        return False

    def read_current(
        self,
        symbol: str,
        *,
        depth: str = "screen",
    ) -> CompanyIntelligenceBrief | None:
        # "preferred" resolves the one brief a symbol should have: normally a
        # symbol is analyzed at a single depth (universe -> deep, candidate ->
        # screen). When both coexist (a symbol entering or LEAVING the universe),
        # serve the most recent, deep on a tie — so a symbol that left the
        # universe stops serving its now-stale deep brief once its screen brief
        # refreshes. Mirrors the cockpit's _pick_brief ordering.
        if depth == "preferred":
            deep = self.read_current(symbol, depth="deep")
            screen = self.read_current(symbol, depth="screen")
            if deep is None:
                return screen
            if screen is None:
                return deep
            return screen if _brief_as_of_ts(screen) > _brief_as_of_ts(deep) else deep
        envelope = self._read_current_envelope(symbol)
        raw = (envelope.get("briefs") or {}).get(depth)
        if not isinstance(raw, dict):
            return self.rebuild_current(symbol, depth=depth)
        brief = CompanyIntelligenceBrief.from_mapping(raw)
        if brief is None or brief.symbol != symbol or brief.depth != depth:
            return self.rebuild_current(symbol, depth=depth)
        return brief

    def read_current_many(
        self,
        symbols: Iterable[str],
        *,
        depth: str = "screen",
    ) -> dict[str, CompanyIntelligenceBrief | None]:
        result: dict[str, CompanyIntelligenceBrief | None] = {}
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").strip()
            if symbol and symbol not in result:
                result[symbol] = self.read_current(symbol, depth=depth)
        return result

    def read_history(self, symbol: str) -> list[CompanyIntelligenceBrief]:
        try:
            lines = self.history_path(symbol).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return _read_briefs(lines, symbol=symbol)

    def rebuild_current(
        self,
        symbol: str,
        *,
        depth: str = "screen",
    ) -> CompanyIntelligenceBrief | None:
        history = [brief for brief in self.read_history(symbol) if brief.depth == depth]
        if not history:
            return None
        latest = history[-1]
        with self._lock:
            envelope = self._read_current_envelope(symbol)
            briefs = dict(envelope.get("briefs") or {})
            briefs[depth] = latest.to_dict()
            write_json_atomic(
                self.current_path(symbol),
                {"schema_version": 1, "symbol": symbol, "briefs": briefs},
            )
        return latest

    def _read_current_envelope(self, symbol: str) -> dict[str, Any]:
        payload = read_json_object(self.current_path(symbol))
        if payload is None or payload.get("symbol") != symbol:
            return {"schema_version": 1, "symbol": symbol, "briefs": {}}
        if not isinstance(payload.get("briefs"), dict):
            payload["briefs"] = {}
        return payload


def _history_contains_payload(lines: Iterable[str], payload: Mapping[str, Any]) -> bool:
    try:
        expected = payload_sha256(payload)
    except (TypeError, ValueError):
        return False
    for line in lines:
        try:
            row: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            if payload_sha256(dict(unwrap_history_payload(row))) == expected:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _read_briefs(lines: Iterable[str], *, symbol: str) -> list[CompanyIntelligenceBrief]:
    result: list[CompanyIntelligenceBrief] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        brief = CompanyIntelligenceBrief.from_mapping(unwrap_history_payload(payload))
        if brief is not None and brief.symbol == symbol:
            result.append(brief)
    return result


__all__ = ["CompanyIntelligenceStore"]
