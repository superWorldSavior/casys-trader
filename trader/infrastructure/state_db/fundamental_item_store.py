"""Append-only per-symbol store for normalized company evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Iterable

from trader.domain.company import CompanyEvidenceItem


def symbol_storage_key(symbol: str) -> str:
    normalized = str(symbol or "").strip()
    if not normalized:
        raise ValueError("symbol must be a non-empty string")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class FundamentalItemStore:
    """Canonical `state/fundamental_items/<sha256(symbol)>.jsonl` store."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def path_for_symbol(self, symbol: str) -> Path:
        return self.base_dir / f"{symbol_storage_key(symbol)}.jsonl"

    def append(self, item: CompanyEvidenceItem) -> tuple[dict[str, str], bool]:
        path = self.path_for_symbol(item.symbol)
        payload = item.to_dict()
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = _read_items(handle.read().splitlines(), symbol=item.symbol)
                duplicate = any(
                    candidate.item_id == item.item_id and candidate.content_hash == item.content_hash
                    for candidate in existing
                )
                if not duplicate:
                    handle.seek(0, 2)
                    handle.write(line + "\n")
                    handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return (
            {
                "symbol": item.symbol,
                "item_id": item.item_id,
                "content_hash": item.content_hash,
                "source_ref": item.source_ref,
            },
            not duplicate,
        )

    def append_many(self, items: Iterable[CompanyEvidenceItem]) -> dict[str, int]:
        appended = 0
        skipped = 0
        for item in items:
            _ref, written = self.append(item)
            if written:
                appended += 1
            else:
                skipped += 1
        return {"appended": appended, "skipped": skipped}

    def read(self, symbol: str) -> list[CompanyEvidenceItem]:
        path = self.path_for_symbol(symbol)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return _read_items(lines, symbol=symbol)


def _read_items(lines: Iterable[str], *, symbol: str) -> list[CompanyEvidenceItem]:
    result: list[CompanyEvidenceItem] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        item = CompanyEvidenceItem.from_mapping(payload)
        if item is not None and item.symbol == symbol:
            result.append(item)
    return result


__all__ = ["FundamentalItemStore", "symbol_storage_key"]
