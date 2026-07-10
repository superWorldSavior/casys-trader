"""FinMind/MOPS normalized financial statements for Taiwan equities."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any

from trader.domain.company import (
    CompanyEvidenceItem,
    CompanyEvidenceSnapshot,
    IssuerIdentity,
    build_company_input_signature,
)
from trader.infrastructure.market_sources.company.http import fetch_json

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


class FinMindCompanyEvidenceProvider:
    def __init__(
        self,
        *,
        fetch: Callable[..., dict[str, Any]] = fetch_json,
        token: str | None = None,
        issuer_names: Mapping[str, str] | None = None,
    ) -> None:
        self._fetch = fetch
        self._token = token or os.getenv("FINMIND_TOKEN")
        self._issuer_names = {str(key): str(value) for key, value in (issuer_names or {}).items()}

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        if not symbol.endswith((".TW", ".TWO")):
            return None
        data_id = symbol.split(".", 1)[0]
        at_date = _as_date(as_of)
        params = {
            "dataset": "TaiwanStockFinancialStatements",
            "data_id": data_id,
            "start_date": (at_date - timedelta(days=800)).isoformat(),
            "end_date": at_date.isoformat(),
            "token": self._token,
        }
        payload = self._fetch(FINMIND_URL, params=params, timeout_s=30)
        raw_rows = payload.get("data")
        rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)] if isinstance(raw_rows, list) else []
        latest_date = max((str(row.get("date") or "") for row in rows), default="")
        latest_rows = [row for row in rows if str(row.get("date") or "") == latest_date]
        normalized = {
            "data_id": data_id,
            "period_end": latest_date,
            "statements": latest_rows[:120],
        }
        items: tuple[CompanyEvidenceItem, ...] = ()
        if latest_rows:
            digest = build_company_input_signature(normalized)
            item = CompanyEvidenceItem.from_mapping(
                {
                    "item_id": f"finmind:{data_id}:financials:{latest_date}:{digest[:12]}",
                    "symbol": symbol,
                    "provider": "finmind_mops",
                    "kind": "financial_statement",
                    "source_ref": f"finmind:{data_id}:financials:{latest_date}:{digest[:12]}",
                    "source_name": "FinMind · MOPS/TWSE financial statements",
                    "source_url": FINMIND_URL,
                    "as_of": as_of,
                    "period_end": latest_date,
                    "currency": "TWD",
                    "payload": normalized,
                    "evidence_label": "fact_source_reported",
                }
            )
            if item is not None:
                items = (item,)
        return CompanyEvidenceSnapshot.build(
            symbol=symbol,
            as_of=as_of,
            identity=IssuerIdentity(
                self._issuer_names.get(symbol, symbol),
                exchange="TWSE" if symbol.endswith(".TW") else "TPEx",
                venue="TW",
                currency="TWD",
                external_ids={"mops_code": data_id},
                identity_status="verified" if symbol in self._issuer_names else "unverified",
            ),
            items=items,
            coverage={
                "provider": "finmind_mops",
                "status": "partial" if items else "missing",
                "financials": "present" if items else "missing",
            },
        )


def _as_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return date.today()


__all__ = ["FinMindCompanyEvidenceProvider"]
