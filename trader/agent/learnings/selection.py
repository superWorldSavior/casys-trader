"""Selection pure des learnings bruts a consolider."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_ts(raw: Any) -> datetime | None:
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
    watermark_dt = _parse_ts(watermark)
    selected: list[dict] = []
    for row in raw_rows:
        row_ts = _parse_ts(row.get("ts"))
        if row_ts is None:
            continue
        if watermark_dt is None or row_ts > watermark_dt:
            selected.append(row)
    selected.sort(key=lambda item: _parse_ts(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return selected


def pending_raw_count(raw_rows: list[dict], watermark: str | None) -> int:
    return len(select_new_raw(raw_rows, watermark))
