"""Append-only company-intelligence history with atomic current projections."""

from __future__ import annotations

import fcntl
import json
import threading
from pathlib import Path
from typing import Any, Iterable

from trader.domain.company import CompanyIntelligenceBrief
from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key
from trader.infrastructure.state_db.shadow import write_json_atomic


class CompanyIntelligenceStore:
    """Canonical per-symbol history plus reconstructible current projection."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.history_dir = self.base_dir / "history"
        self.current_dir = self.base_dir / "current"
        self._lock = threading.RLock()

    def history_path(self, symbol: str) -> Path:
        return self.history_dir / f"{symbol_storage_key(symbol)}.jsonl"

    def current_path(self, symbol: str) -> Path:
        return self.current_dir / f"{symbol_storage_key(symbol)}.json"

    def append(self, brief: CompanyIntelligenceBrief) -> tuple[dict[str, str], bool]:
        path = self.history_path(brief.symbol)
        payload = brief.to_dict()
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                history = _read_briefs(handle.read().splitlines(), symbol=brief.symbol)
                duplicate = any(
                    candidate.input_signature == brief.input_signature and candidate.depth == brief.depth
                    for candidate in history
                )
                if not duplicate:
                    handle.seek(0, 2)
                    handle.write(line + "\n")
                    handle.flush()
                    projection = self._read_current_envelope(brief.symbol)
                    briefs = dict(projection.get("briefs") or {})
                    briefs[brief.depth] = payload
                    write_json_atomic(
                        self.current_path(brief.symbol),
                        {"schema_version": 1, "symbol": brief.symbol, "briefs": briefs},
                    )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return brief.ref(), not duplicate

    def read_current(
        self,
        symbol: str,
        *,
        depth: str = "screen",
    ) -> CompanyIntelligenceBrief | None:
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
        try:
            payload: Any = json.loads(self.current_path(symbol).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "symbol": symbol, "briefs": {}}
        if not isinstance(payload, dict) or payload.get("symbol") != symbol:
            return {"schema_version": 1, "symbol": symbol, "briefs": {}}
        briefs = payload.get("briefs")
        if not isinstance(briefs, dict):
            payload["briefs"] = {}
        return payload


def _read_briefs(lines: Iterable[str], *, symbol: str) -> list[CompanyIntelligenceBrief]:
    result: list[CompanyIntelligenceBrief] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        brief = CompanyIntelligenceBrief.from_mapping(payload)
        if brief is not None and brief.symbol == symbol:
            result.append(brief)
    return result


__all__ = ["CompanyIntelligenceStore"]
