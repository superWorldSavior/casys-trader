"""Prepared and activated universe-mandate projections."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trader.domain.universe import UniverseMandate
from trader.infrastructure.state_db._jsonl_store import (
    append_jsonl_line,
    read_json_object,
    safe_filename_component,
)
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
        return self.active_venue_dir / f"{safe_filename_component(venue or '', empty_error='venue must be non-empty')}.json"

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
        payload = read_json_object(self.prepared_path(candidate_scope_id))
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
            family_postures = (
                dict(prepared.get("family_postures"))
                if isinstance(prepared.get("family_postures"), Mapping)
                else {}
            )
            portfolio_posture = (
                dict(prepared.get("portfolio_posture"))
                if isinstance(prepared.get("portfolio_posture"), Mapping)
                else None
            )
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
            family_postures = {}
            portfolio_posture = None
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
            "family_postures": family_postures,
            "portfolio_posture": portfolio_posture,
        }
        with self._lock:
            previous = read_json_object(self.active_venue_path(venue)) or {}
            previous_symbols = set(previous.get("symbols") or ())
            write_json_atomic(self.active_venue_path(venue), payload)
            for symbol, mandate in symbol_mandates.items():
                write_json_atomic(
                    self.active_symbol_path(symbol),
                    {
                        "mandate_ref": _ref(payload),
                        "symbol_mandate": mandate,
                        "family_postures": family_postures,
                        "portfolio_posture": portfolio_posture,
                    },
                )
            for symbol in previous_symbols - set(symbol_mandates):
                path = self.active_symbol_path(symbol)
                current = read_json_object(path)
                ref = current.get("mandate_ref") if isinstance(current, Mapping) else None
                if isinstance(ref, Mapping) and ref.get("venue") == venue:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            self._append_history(payload)
        return payload

    def active_slice_for_symbol(
        self,
        symbol: str,
        *,
        active_at: datetime | str,
    ) -> dict[str, Any] | None:
        moment = _parse_datetime(active_at)
        if moment is None:
            return None
        payload = read_json_object(self.active_symbol_path(symbol))
        mandate = payload.get("symbol_mandate") if isinstance(payload, Mapping) else None
        ref = payload.get("mandate_ref") if isinstance(payload, Mapping) else None
        if not isinstance(mandate, Mapping) or mandate.get("symbol") != symbol or not isinstance(ref, Mapping):
            return None
        venue = str(ref.get("venue") or "").strip()
        if not venue:
            return None
        try:
            active_venue = read_json_object(self.active_venue_path(venue))
        except ValueError:
            return None
        if not isinstance(active_venue, Mapping) or not _matches_active_venue(ref, active_venue, symbol=symbol):
            return None
        valid_until = active_venue.get("valid_until")
        if valid_until is not None:
            expires_at = _parse_datetime(valid_until)
            if expires_at is None or expires_at <= moment:
                return None
        active_symbols = active_venue.get("symbols")
        active_mandate = active_symbols.get(symbol) if isinstance(active_symbols, Mapping) else None
        if not isinstance(active_mandate, Mapping):
            return None
        return {
            "mandate_ref": _ref(active_venue),
            "symbol_mandate": dict(active_mandate),
            "family_postures": (
                dict(active_venue.get("family_postures"))
                if isinstance(active_venue.get("family_postures"), Mapping)
                else {}
            ),
            "portfolio_posture": (
                dict(active_venue.get("portfolio_posture"))
                if isinstance(active_venue.get("portfolio_posture"), Mapping)
                else None
            ),
        }

    def _append_history(self, payload: Mapping[str, Any]) -> None:
        append_jsonl_line(self.history_path, payload, flock=True)


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


def _matches_active_venue(
    ref: Mapping[str, Any],
    active_venue: Mapping[str, Any] | None,
    *,
    symbol: str,
) -> bool:
    if not isinstance(active_venue, Mapping):
        return False
    for field in ("mandate_id", "candidate_scope_id", "venue"):
        ref_value = str(ref.get(field) or "").strip()
        if not ref_value or ref_value != str(active_venue.get(field) or "").strip():
            return False
    symbols = active_venue.get("symbols")
    active_symbol = symbols.get(symbol) if isinstance(symbols, Mapping) else None
    return isinstance(active_symbol, Mapping) and active_symbol.get("symbol") == symbol


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = ["UniverseMandateStore"]
