"""Normalized recent company news already collected by the local runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trader.domain.company import (
    CompanyEvidenceItem,
    CompanyEvidenceSnapshot,
    IssuerIdentity,
    build_company_input_signature,
)
from trader.market.rotation.wiring import venue_of


class LocalCompanyNewsEvidenceProvider:
    def __init__(self, base_dir: str | Path, *, lookback_days: int = 7, max_items: int = 20) -> None:
        self.base_dir = Path(base_dir)
        self.lookback_days = max(0, int(lookback_days))
        self.max_items = max(1, int(max_items))

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        day = _date(as_of)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for offset in range(self.lookback_days + 1):
            path = self.base_dir / f"{(day - timedelta(days=offset)).isoformat()}.jsonl"
            for row in reversed(_read_rows(path)):
                if str(row.get("symbol") or "").strip() != symbol:
                    continue
                uid = str(row.get("uuid") or row.get("source_ref") or "").strip()
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                rows.append(
                    {
                        key: row.get(key)
                        for key in (
                            "uuid",
                            "title",
                            "publisher",
                            "provider",
                            "published_at",
                            "link",
                            "summary",
                        )
                        if row.get(key) is not None
                    }
                )
                if len(rows) >= self.max_items:
                    break
            if len(rows) >= self.max_items:
                break
        if not rows:
            return None
        payload = {"symbol": symbol, "items": rows}
        digest = build_company_input_signature(payload)
        item = CompanyEvidenceItem.from_mapping(
            {
                "item_id": f"local-news:{symbol}:{digest[:16]}",
                "symbol": symbol,
                "provider": "local_company_news",
                "kind": "company_news",
                "source_ref": f"local-news:{symbol}:{digest[:16]}",
                "source_name": "Local company-news archive",
                "as_of": as_of,
                "payload": payload,
                "evidence_label": "fact_provider_standardized",
            }
        )
        if item is None:
            return None
        return CompanyEvidenceSnapshot.build(
            symbol=symbol,
            as_of=as_of,
            identity=IssuerIdentity(symbol, venue=venue_of(symbol), identity_status="unverified"),
            items=(item,),
            coverage={"provider": "local_company_news", "status": "partial", "news": "present"},
        )


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            rows.append(dict(payload))
    return rows


def _date(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return date.today()


__all__ = ["LocalCompanyNewsEvidenceProvider"]
