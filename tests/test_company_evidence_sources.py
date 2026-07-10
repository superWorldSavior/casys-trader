import gzip
import json

from trader.infrastructure.market_sources.company import http as company_http
from trader.infrastructure.market_sources.company import (
    CompositeCompanyEvidenceProvider,
    EsefCompanyEvidenceProvider,
    FinMindCompanyEvidenceProvider,
    LocalCompanyNewsEvidenceProvider,
    SecEdgarCompanyEvidenceProvider,
    YFinanceCompanyEvidenceProvider,
)


AS_OF = "2026-07-10T08:00:00+00:00"


class _HttpResponse:
    def __init__(self, payload: bytes, *, content_encoding: str = "") -> None:
        self._payload = payload
        self.headers = {"Content-Encoding": content_encoding}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._payload


def test_fetch_json_decodes_gzip_response(monkeypatch) -> None:
    body = gzip.compress(json.dumps({"ok": True}).encode("utf-8"))
    monkeypatch.setattr(
        company_http,
        "urlopen",
        lambda request, timeout: _HttpResponse(body, content_encoding="gzip"),
    )

    assert company_http.fetch_json("https://example.test/data.json") == {"ok": True}


class _Series:
    def to_dict(self):
        return {"Total Revenue": 120, "Net Income": 12}


class _Columns:
    def __getitem__(self, index):
        assert index == 0
        return "2026-06-30"


class _ILoc:
    def __getitem__(self, key):
        assert key == (slice(None), 0)
        return _Series()


class _Frame:
    empty = False
    columns = _Columns()
    iloc = _ILoc()


class _Ticker:
    quarterly_income_stmt = _Frame()
    quarterly_balance_sheet = None
    quarterly_cashflow = None
    calendar = {"Earnings Date": ["2026-08-01"]}

    def get_info(self):
        return {
            "longName": "Example Corp",
            "quoteType": "EQUITY",
            "exchange": "NMS",
            "currency": "USD",
            "sector": "Technology",
        }


def test_yfinance_normalizes_profile_financials_and_calendar() -> None:
    provider = YFinanceCompanyEvidenceProvider(ticker_factory=lambda symbol: _Ticker())

    snapshot = provider.collect(symbol="EXM", as_of=AS_OF)

    assert snapshot is not None
    assert snapshot.identity.issuer_name == "Example Corp"
    assert {item.kind for item in snapshot.items} == {
        "earnings_calendar",
        "financial_statement",
        "profile",
    }


def test_sec_edgar_normalizes_latest_10q_fact() -> None:
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "EXM", "title": "Example Corp", "cik_str": 42}}
        return {
            "entityName": "Example Corp",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {"form": "10-Q", "end": "2026-03-31", "filed": "2026-05-01", "val": 120}
                            ]
                        }
                    }
                }
            },
        }

    snapshot = SecEdgarCompanyEvidenceProvider(fetch=fetch, user_agent="Casys test@example.com").collect(
        symbol="EXM", as_of=AS_OF
    )

    assert snapshot is not None
    assert snapshot.identity.external_ids == {"cik": "0000000042"}
    assert snapshot.items[0].payload["metrics"]["revenue"]["val"] == 120
    assert calls[1][1]["headers"]["User-Agent"] == "Casys test@example.com"


def test_finmind_queries_taiwan_financial_statements() -> None:
    captured = {}

    def fetch(url, **kwargs):
        captured.update(kwargs)
        return {
            "data": [
                {"date": "2026-03-31", "type": "Revenue", "value": 100},
                {"date": "2026-03-31", "type": "Income", "value": 10},
            ]
        }

    snapshot = FinMindCompanyEvidenceProvider(
        fetch=fetch,
        issuer_names={"2330.TW": "TSMC"},
    ).collect(symbol="2330.TW", as_of=AS_OF)

    assert snapshot is not None
    assert snapshot.identity.identity_status == "verified"
    assert snapshot.items[0].period_end == "2026-03-31"
    assert captured["params"]["dataset"] == "TaiwanStockFinancialStatements"
    assert captured["params"]["data_id"] == "2330"


def test_esef_uses_entity_filter_and_keeps_filing_metadata() -> None:
    captured = {}

    def fetch(url, **kwargs):
        captured.update(kwargs)
        return {
            "data": [
                {
                    "id": "filing-1",
                    "attributes": {"reporting_date": "2025-12-31"},
                    "links": {"self": "https://example.test/filing-1"},
                }
            ]
        }

    snapshot = EsefCompanyEvidenceProvider(
        entities={"SAP.DE": {"lei": "529900D6BF99LW9R2E68"}},
        fetch=fetch,
        issuer_names={"SAP.DE": "SAP SE"},
    ).collect(symbol="SAP.DE", as_of=AS_OF)

    assert snapshot is not None
    assert snapshot.items[0].payload["filings"][0]["id"] == "filing-1"
    assert captured["params"]["filter[entity]"] == "529900D6BF99LW9R2E68"
    assert captured["params"]["page[size]"] == 5


def test_composite_isolates_provider_errors_and_preserves_remaining_evidence() -> None:
    class Broken:
        def collect(self, *, symbol, as_of):
            raise RuntimeError("offline")

    provider = CompositeCompanyEvidenceProvider(
        [Broken(), YFinanceCompanyEvidenceProvider(ticker_factory=lambda symbol: _Ticker())]
    )

    snapshot = provider.collect(symbol="EXM", as_of=AS_OF)

    assert snapshot.items
    assert snapshot.coverage["provider_errors"] == ["Broken:RuntimeError"]


def test_local_company_news_is_symbol_scoped_and_changes_fingerprint(tmp_path) -> None:
    path = tmp_path / "2026-07-10.jsonl"
    path.write_text(
        '\n'.join(
            (
                '{"uuid":"u1","symbol":"EXM","title":"Guidance raised","publisher":"Reuters"}',
                '{"uuid":"u2","symbol":"OTHER","title":"Must not leak"}',
            )
        )
        + "\n"
    )
    provider = LocalCompanyNewsEvidenceProvider(tmp_path)

    first = provider.collect(symbol="EXM", as_of=AS_OF)
    path.write_text(
        path.read_text()
        + '{"uuid":"u3","symbol":"EXM","title":"New contract","publisher":"Company"}\n'
    )
    second = provider.collect(symbol="EXM", as_of=AS_OF)

    assert first is not None and second is not None
    assert "OTHER" not in str(second.items[0].payload)
    assert first.input_signature != second.input_signature
