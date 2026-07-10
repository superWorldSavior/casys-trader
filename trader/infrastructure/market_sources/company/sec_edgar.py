"""Official SEC EDGAR company-facts evidence for US equities."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from trader.domain.company import (
    CompanyEvidenceItem,
    CompanyEvidenceSnapshot,
    IssuerIdentity,
    build_company_input_signature,
)
from trader.infrastructure.market_sources.company.http import fetch_json

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_CONCEPTS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "diluted_eps": ("EarningsPerShareDiluted",),
}


class SecEdgarCompanyEvidenceProvider:
    def __init__(
        self,
        *,
        fetch: Callable[..., dict[str, Any]] = fetch_json,
        user_agent: str | None = None,
    ) -> None:
        self._fetch = fetch
        self._user_agent = user_agent or os.getenv("CASYS_SEC_USER_AGENT")
        self._ticker_index: dict[str, dict[str, Any]] | None = None

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        if not _is_us_equity_symbol(symbol):
            return None
        if not self._user_agent:
            raise RuntimeError("CASYS_SEC_USER_AGENT is required for SEC EDGAR")
        company = self._company_for_ticker(symbol)
        if company is None:
            return None
        cik = f"{int(company['cik_str']):010d}"
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        raw = self._fetch(SEC_COMPANY_FACTS_URL.format(cik=cik), headers=headers, timeout_s=30)
        metrics = _latest_company_facts(raw)
        normalized = {
            "entity_name": raw.get("entityName") or company.get("title"),
            "cik": cik,
            "metrics": metrics,
        }
        if not metrics:
            return CompanyEvidenceSnapshot.build(
                symbol=symbol,
                as_of=as_of,
                identity=IssuerIdentity(
                    str(normalized["entity_name"] or symbol),
                    venue="US",
                    external_ids={"cik": cik},
                    identity_status="verified",
                ),
                items=(),
                coverage={"provider": "sec_edgar", "status": "partial", "financials": "missing"},
            )
        digest = build_company_input_signature(normalized)
        item = CompanyEvidenceItem.from_mapping(
            {
                "item_id": f"sec:{cik}:companyfacts:{digest[:16]}",
                "symbol": symbol,
                "provider": "sec_edgar",
                "kind": "financial_statement",
                "source_ref": f"sec:{cik}:companyfacts:{digest[:16]}",
                "source_name": "SEC EDGAR · Company Facts",
                "source_url": SEC_COMPANY_FACTS_URL.format(cik=cik),
                "as_of": as_of,
                "payload": normalized,
                "evidence_label": "fact_source_reported",
            }
        )
        if item is None:
            raise ValueError("invalid normalized SEC evidence")
        return CompanyEvidenceSnapshot.build(
            symbol=symbol,
            as_of=as_of,
            identity=IssuerIdentity(
                str(normalized["entity_name"] or symbol),
                venue="US",
                external_ids={"cik": cik},
                identity_status="verified",
            ),
            items=(item,),
            coverage={"provider": "sec_edgar", "status": "partial", "financials": "present"},
        )

    def _company_for_ticker(self, symbol: str) -> dict[str, Any] | None:
        if self._ticker_index is None:
            headers = {"User-Agent": self._user_agent or ""}
            payload = self._fetch(SEC_TICKERS_URL, headers=headers, timeout_s=30)
            self._ticker_index = {
                str(item.get("ticker") or "").upper(): dict(item)
                for item in payload.values()
                if isinstance(item, Mapping) and item.get("ticker")
            }
        return self._ticker_index.get(symbol.upper())


def _latest_company_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    us_gaap = ((raw.get("facts") or {}).get("us-gaap") or {}) if isinstance(raw.get("facts"), Mapping) else {}
    result: dict[str, Any] = {}
    for output_name, candidates in _CONCEPTS.items():
        for concept in candidates:
            concept_payload = us_gaap.get(concept) if isinstance(us_gaap, Mapping) else None
            latest = _latest_fact(concept_payload)
            if latest is not None:
                result[output_name] = {"concept": concept, **latest}
                break
    return result


def _latest_fact(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    units = raw.get("units")
    if not isinstance(units, Mapping):
        return None
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for unit, values in units.items():
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping) or value.get("form") not in {"10-K", "10-Q"}:
                continue
            row = dict(value)
            rows.append((str(row.get("end") or ""), str(row.get("filed") or ""), {"unit": str(unit), **row}))
    if not rows:
        return None
    return max(rows, key=lambda item: (item[0], item[1]))[2]


def _is_us_equity_symbol(symbol: str) -> bool:
    return bool(symbol and "." not in symbol and not symbol.startswith("^") and "=" not in symbol)


__all__ = ["SecEdgarCompanyEvidenceProvider"]
