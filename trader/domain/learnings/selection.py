"""Sélection des notes brutes strictement après un watermark de consolidation.

In : lignes brutes avec ``ts`` ISO, watermark optionnel. Out : lignes plus
récentes que le watermark, triées. Les ``ts`` illisibles sont ignorés.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_ts(raw: Any) -> datetime | None:
    """ISO → datetime UTC ; None si vide ou invalide. Naive traité comme UTC."""

    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def select_new_raw(raw_rows: list[dict], watermark: str | None) -> list[dict]:
    """Lignes dont ``ts`` > watermark (toutes si watermark None), plus anciennes d'abord."""

    watermark_dt = parse_ts(watermark)
    selected: list[dict] = []
    for row in raw_rows:
        row_ts = parse_ts(row.get("ts"))
        if row_ts is None:
            continue
        if watermark_dt is None or row_ts > watermark_dt:
            selected.append(row)
    selected.sort(key=lambda item: parse_ts(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return selected


def pending_raw_count(raw_rows: list[dict], watermark: str | None) -> int:
    """Nombre de lignes que ``select_new_raw`` retiendrait."""

    return len(select_new_raw(raw_rows, watermark))
