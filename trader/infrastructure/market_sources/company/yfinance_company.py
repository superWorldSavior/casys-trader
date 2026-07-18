"""Cross-market, explicitly partial company evidence from yfinance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, IssuerIdentity
from trader.market.rotation.wiring import venue_of


class YFinanceCompanyEvidenceProvider:
    def __init__(self, *, ticker_factory: Callable[[str], Any] | None = None) -> None:
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        if symbol.startswith("^") or "=" in symbol:
            return None
        ticker = self._ticker_factory(symbol)
        info = _safe_mapping_call(ticker, "get_info", fallback_attr="info")
        quote_type = str(info.get("quoteType") or "EQUITY").upper()
        instrument_type = "equity" if quote_type in {"EQUITY", "MUTUALFUND"} else quote_type.lower()
        identity = IssuerIdentity(
            issuer_name=str(info.get("longName") or info.get("shortName") or symbol),
            instrument_type=instrument_type,
            exchange=str(info.get("exchange") or "") or None,
            venue=venue_of(symbol),
            currency=str(info.get("currency") or "") or None,
            external_ids={"isin": str(info.get("isin"))} if info.get("isin") else {},
            identity_status="verified" if info.get("longName") or info.get("shortName") else "unverified",
        )
        items: list[CompanyEvidenceItem] = []
        # Only stable, fundamental profile fields. Price-derived valuation fields
        # (marketCap, enterpriseValue, averageVolume, trailingPE, forwardPE,
        # priceToBook) are deliberately excluded: they drift with every intraday
        # tick, which would flip the evidence content_hash and thus the dedup
        # input_signature on every cycle, re-triggering the fundamental analysis
        # in a loop. The micro analyst judges company quality, not stock
        # valuation, so it does not need them.
        profile_payload = {
            key: info.get(key)
            for key in (
                "longName",
                "sector",
                "industry",
                "country",
                "website",
                "longBusinessSummary",
                "sharesOutstanding",
                "floatShares",
                "beta",
            )
            if info.get(key) is not None
        }
        if profile_payload:
            items.append(_item(symbol, as_of, "profile", profile_payload))

        financial_payload: dict[str, Any] = {}
        for target, attribute in (
            ("income_statement", "quarterly_income_stmt"),
            ("balance_sheet", "quarterly_balance_sheet"),
            ("cash_flow", "quarterly_cashflow"),
        ):
            values = _latest_frame_values(getattr(ticker, attribute, None))
            if values:
                financial_payload[target] = values
        if financial_payload:
            items.append(_item(symbol, as_of, "financial_statement", financial_payload))

        calendar_value = _json_safe(getattr(ticker, "calendar", None))
        calendar = calendar_value if isinstance(calendar_value, Mapping) else {}
        if calendar:
            items.append(_item(symbol, as_of, "earnings_calendar", calendar))
        coverage = {
            "provider": "yfinance",
            "status": "partial" if items else "missing",
            "profile": "present" if profile_payload else "missing",
            "financials": "present" if financial_payload else "missing",
            "earnings": "present" if calendar else "missing",
        }
        return CompanyEvidenceSnapshot.build(
            symbol=symbol,
            as_of=as_of,
            identity=identity,
            items=items,
            coverage=coverage,
        )


def _item(symbol: str, as_of: str, kind: str, payload: Mapping[str, Any]) -> CompanyEvidenceItem:
    result = CompanyEvidenceItem.from_mapping(
        {
            "item_id": f"yfinance:{symbol}:{kind}:{as_of[:10]}",
            "symbol": symbol,
            "provider": "yfinance",
            "kind": kind,
            "source_ref": f"yfinance:{symbol}:{kind}:{as_of[:10]}",
            "source_name": f"Yahoo Finance · {kind.replace('_', ' ')}",
            "as_of": as_of,
            "payload": dict(payload),
            "evidence_label": "fact_provider_standardized",
        }
    )
    if result is None:
        raise ValueError("invalid normalized yfinance evidence")
    return result


def _safe_mapping_call(target: Any, method_name: str, *, fallback_attr: str) -> dict[str, Any]:
    try:
        method = getattr(target, method_name, None)
        value = method() if callable(method) else getattr(target, fallback_attr, {})
    except Exception:  # noqa: BLE001 - provider coverage is partial by contract
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _latest_frame_values(frame: Any) -> dict[str, Any]:
    try:
        if frame is None or bool(frame.empty):
            return {}
        series = frame.iloc[:, 0]
        period = frame.columns[0]
        values = series.to_dict()
    except Exception:  # noqa: BLE001 - tolerate yfinance/pandas shape drift
        return {}
    return {
        "period": _json_safe(period),
        "values": {str(key): _json_safe(value) for key, value in values.items() if _json_safe(value) is not None},
    }


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(nested) for nested in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:  # noqa: BLE001 - tolerate dataframe/series API drift
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


__all__ = ["YFinanceCompanyEvidenceProvider"]
