"""JSON stores and normalization helpers for consolidated learnings."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("trader.agent.learnings.consolidator")

DEFAULT_MAX_GLOBAL = 10
DEFAULT_MAX_BY_SYMBOL = 5


def empty_consolidated() -> dict:
    return {"watermark": None, "global": [], "by_symbol": {}}


def _normalize_entry(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    note = str(item.get("note") or "").strip()
    if not note:
        return None
    entry = {"note": note}
    robustness = str(item.get("robustness") or "").strip()
    if robustness:
        entry["robustness"] = robustness
    return entry


def _normalize_entries(items: Any, *, limit: int) -> list[dict]:
    if not isinstance(items, list):
        return []
    entries: list[dict] = []
    for item in items:
        entry = _normalize_entry(item)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def normalize_consolidated(payload: Any, *, watermark: str | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    by_symbol_raw = payload.get("by_symbol", {})
    if not isinstance(by_symbol_raw, dict):
        return None

    by_symbol: dict[str, list[dict]] = {}
    for raw_symbol, items in by_symbol_raw.items():
        symbol = str(raw_symbol).strip()
        if not symbol:
            continue
        entries = _normalize_entries(items, limit=DEFAULT_MAX_BY_SYMBOL)
        if entries:
            by_symbol[symbol] = entries

    return {
        "watermark": watermark,
        "global": _normalize_entries(payload.get("global", []), limit=DEFAULT_MAX_GLOBAL),
        "by_symbol": by_symbol,
    }


class ConsolidatedLearningsStore:
    def __init__(self, path: str | Path, *, history_path: str | Path | None = None):
        self.path = Path(path)
        self.history_path = (
            Path(history_path)
            if history_path is not None
            else self.path.parent / "archive" / f"{self.path.stem}-history.jsonl"
        )

    def read(self) -> dict:
        if not self.path.exists():
            return empty_consolidated()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty_consolidated()
        normalized = normalize_consolidated(payload, watermark=payload.get("watermark"))
        return normalized or empty_consolidated()

    def write(self, payload: dict, *, watermark: str | None) -> None:
        normalized = normalize_consolidated(payload, watermark=watermark)
        if normalized is None:
            raise ValueError("invalid consolidated learnings payload")
        self._archive_replaced(replaced_by_watermark=normalized.get("watermark"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _archive_replaced(self, *, replaced_by_watermark: str | None) -> None:
        if not self.path.exists():
            return
        try:
            previous = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        entry = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "replaced_by_watermark": replaced_by_watermark,
            "payload": previous,
        }
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("historisation consolide non ecrite %s (%s)", self.history_path, exc)


class ConsolidationStatusStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_failure(
        self,
        *,
        consolidated_watermark: str | None,
        raw_watermark: str | None,
        new_raw_count: int,
        error: dict,
    ) -> None:
        payload = {
            "last_failure": {
                "consolidated_watermark": consolidated_watermark,
                "raw_watermark": raw_watermark,
                "new_raw_count": new_raw_count,
                "error_code": str(error.get("error_code") or "unknown"),
                "error_message": str(error.get("error_message") or "")[:500],
                "provider": error.get("provider"),
                "model": error.get("model"),
            }
        }
        optional_fields = (
            "requested_model",
            "output_preview",
            "output_tail",
            "output_length",
        )
        for field in optional_fields:
            if field in error:
                payload["last_failure"][field] = error[field]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
