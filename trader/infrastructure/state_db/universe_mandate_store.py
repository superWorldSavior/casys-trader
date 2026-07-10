"""Prepared and activated universe-mandate projections."""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from trader.domain.universe import UniverseMandate
from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key
from trader.infrastructure.state_db.shadow import write_json_atomic


class UniverseMandateStore:
    """Keep prepared mandates private until an explicit venue activation."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.prepared_dir = self.base_dir / "prepared"
        self.active_venue_dir = self.base_dir / "active" / "venues"
        self.active_symbol_dir = self.base_dir / "active" / "symbols"
        self.history_path = self.base_dir / "history.jsonl"
        self._lock = threading.RLock()

    def prepared_path(self, candidate_scope_id: str) -> Path:
        return self.prepared_dir / f"{_digest(candidate_scope_id)}.json"

    def active_venue_path(self, venue: str) -> Path:
        return self.active_venue_dir / f"{_safe(venue)}.json"

    def active_symbol_path(self, symbol: str) -> Path:
        return self.active_symbol_dir / f"{symbol_storage_key(symbol)}.json"

    def write_prepared(self, mandate: UniverseMandate) -> dict[str, str]:
        if mandate.status != "prepared":
            raise ValueError("only a prepared mandate can enter the prepared projection")
        payload = mandate.to_dict()
        with self._lock:
            write_json_atomic(self.prepared_path(mandate.candidate_scope_id), payload)
            self._append_history(payload)
        return _ref(payload)

    def read_prepared(self, candidate_scope_id: str) -> dict[str, Any] | None:
        payload = _read_mapping(self.prepared_path(candidate_scope_id))
        if payload is None or payload.get("candidate_scope_id") != candidate_scope_id:
            return None
        return payload

    def activate(
        self,
        *,
        venue: str,
        candidate_scope_id: str,
        as_of: str,
        selected_symbols: Iterable[str],
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        symbols = tuple(dict.fromkeys(str(symbol).strip() for symbol in selected_symbols if str(symbol).strip()))
        prepared = self.read_prepared(candidate_scope_id) if not fallback_reason else None
        if prepared is not None:
            available = prepared.get("symbols") if isinstance(prepared.get("symbols"), Mapping) else {}
            symbol_mandates = {
                symbol: dict(available[symbol])
                for symbol in symbols
                if isinstance(available.get(symbol), Mapping)
            }
            status = "active"
            mandate_id = str(prepared.get("mandate_id") or "")
            agent_run_id = str(prepared.get("agent_run_id") or "")
            valid_until = prepared.get("valid_until")
        else:
            symbol_mandates = {
                symbol: {
                    "symbol": symbol,
                    "why_selected": "",
                    "role": "fallback_selection",
                    "posture": "unmandated",
                    "allowed_sides": [],
                    "family_context": {},
                    "company_context": {},
                    "company_brief_ref": {},
                    "confidence": "none",
                }
                for symbol in symbols
            }
            status = "fallback"
            mandate_id = f"universe-mandate:fallback:{_digest(f'{venue}:{candidate_scope_id}:{as_of}') }"
            agent_run_id = ""
            valid_until = None
        payload = {
            "schema_version": 1,
            "mandate_id": mandate_id,
            "candidate_scope_id": candidate_scope_id,
            "venue": venue,
            "agent_run_id": agent_run_id,
            "as_of": as_of,
            "valid_until": valid_until,
            "status": status,
            "symbols": symbol_mandates,
            "fallback_reason": fallback_reason,
        }
        with self._lock:
            previous = _read_mapping(self.active_venue_path(venue)) or {}
            previous_symbols = set(previous.get("symbols") or ())
            write_json_atomic(self.active_venue_path(venue), payload)
            for symbol, mandate in symbol_mandates.items():
                write_json_atomic(
                    self.active_symbol_path(symbol),
                    {
                        "mandate_ref": _ref(payload),
                        "symbol_mandate": mandate,
                    },
                )
            for symbol in previous_symbols - set(symbol_mandates):
                path = self.active_symbol_path(symbol)
                current = _read_mapping(path)
                ref = current.get("mandate_ref") if isinstance(current, Mapping) else None
                if isinstance(ref, Mapping) and ref.get("venue") == venue:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            self._append_history(payload)
        return payload

    def active_slice_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        payload = _read_mapping(self.active_symbol_path(symbol))
        mandate = payload.get("symbol_mandate") if isinstance(payload, Mapping) else None
        ref = payload.get("mandate_ref") if isinstance(payload, Mapping) else None
        if not isinstance(mandate, Mapping) or mandate.get("symbol") != symbol or not isinstance(ref, Mapping):
            return None
        return {"mandate_ref": dict(ref), "symbol_mandate": dict(mandate)}

    def _append_history(self, payload: Mapping[str, Any]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ref(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "mandate_id": str(payload.get("mandate_id") or ""),
        "candidate_scope_id": str(payload.get("candidate_scope_id") or ""),
        "venue": str(payload.get("venue") or ""),
        "agent_run_id": str(payload.get("agent_run_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
        "status": str(payload.get("status") or ""),
    }


def _digest(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("value must be non-empty")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe(value: str) -> str:
    text = "".join(char for char in str(value or "") if char.isalnum() or char in {"-", "_"})
    if not text:
        raise ValueError("venue must be non-empty")
    return text


def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["UniverseMandateStore"]
