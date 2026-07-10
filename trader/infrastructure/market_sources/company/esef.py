"""ESEF filing metadata from the public filings.xbrl.org JSON API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from trader.domain.company import (
    CompanyEvidenceItem,
    CompanyEvidenceSnapshot,
    IssuerIdentity,
    build_company_input_signature,
)
from trader.infrastructure.market_sources.company.http import fetch_json
from trader.market.rotation.wiring import venue_of

ESEF_FILINGS_URL = "https://filings.xbrl.org/api/filings"


class EsefCompanyEvidenceProvider:
    def __init__(
        self,
        *,
        entities: Mapping[str, Mapping[str, Any]] | None = None,
        fetch: Callable[..., dict[str, Any]] = fetch_json,
        issuer_names: Mapping[str, str] | None = None,
    ) -> None:
        self._entities = {str(symbol): dict(value) for symbol, value in (entities or {}).items()}
        self._fetch = fetch
        self._issuer_names = {str(key): str(value) for key, value in (issuer_names or {}).items()}

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        entity = self._entities.get(symbol)
        if entity is None:
            return None
        entity_id = str(entity.get("filings_xbrl_entity_id") or entity.get("lei") or "").strip()
        if not entity_id:
            return None
        payload = self._fetch(
            ESEF_FILINGS_URL,
            params={
                "filter[entity]": entity_id,
                "include": "entity",
                "sort": "-processed",
                "page[size]": 5,
            },
            timeout_s=30,
        )
        data = payload.get("data")
        filings = [dict(row) for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []
        normalized = {
            "entity_id": entity_id,
            "filings": [
                {
                    "id": filing.get("id"),
                    "attributes": dict(filing.get("attributes") or {}),
                    "links": dict(filing.get("links") or {}),
                }
                for filing in filings
            ],
        }
        items: tuple[CompanyEvidenceItem, ...] = ()
        if filings:
            digest = build_company_input_signature(normalized)
            item = CompanyEvidenceItem.from_mapping(
                {
                    "item_id": f"esef:{entity_id}:filings:{digest[:16]}",
                    "symbol": symbol,
                    "provider": "filings_xbrl_org",
                    "kind": "filing",
                    "source_ref": f"esef:{entity_id}:filings:{digest[:16]}",
                    "source_name": "filings.xbrl.org · ESEF filings",
                    "source_url": ESEF_FILINGS_URL,
                    "as_of": as_of,
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
                venue=venue_of(symbol),
                external_ids={"lei": entity_id},
                identity_status="verified",
            ),
            items=items,
            coverage={
                "provider": "filings_xbrl_org",
                "status": "partial" if items else "missing",
                "financials": "filing_metadata_only" if items else "missing",
            },
        )


__all__ = ["EsefCompanyEvidenceProvider"]
