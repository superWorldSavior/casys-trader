import json
from concurrent.futures import ThreadPoolExecutor

from trader.domain.company import CompanyEvidenceItem, CompanyIntelligenceBrief
from trader.infrastructure.state_db.company_analysis_run_store import CompanyAnalysisRunStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.fundamental_item_store import FundamentalItemStore


def _brief(
    *,
    signature: str = "sig-1",
    depth: str = "screen",
    as_of: str = "2026-07-10T01:00:00+00:00",
) -> CompanyIntelligenceBrief:
    payload = {
        "symbol": "SAP.DE",
        "as_of": as_of,
        "input_signature": signature,
        "depth": depth,
        "issuer_identity": {"issuer_name": "SAP SE", "identity_status": "verified"},
        "coverage": {"status": "partial"},
        "business": {},
        "financial_snapshot": {},
        "earnings_and_guidance": {},
        "company_thesis": {"status": "untested"},
        "selection_view": {"posture": "neutral", "confidence": "low"},
        "security_readiness": "not_evaluated",
    }
    result = CompanyIntelligenceBrief.from_mapping(payload)
    assert result is not None
    return result


def _item() -> CompanyEvidenceItem:
    result = CompanyEvidenceItem.from_mapping(
        {
            "item_id": "fixture:SAP.DE:2026Q2",
            "symbol": "SAP.DE",
            "provider": "fixture",
            "kind": "financial_statement",
            "source_ref": "fixture:SAP.DE:2026Q2",
            "source_name": "Fixture filing",
            "as_of": "2026-07-09T00:00:00+00:00",
            "payload": {"revenue": 100.0},
        }
    )
    assert result is not None
    return result


def test_fundamental_item_store_is_append_only_and_idempotent(tmp_path) -> None:
    store = FundamentalItemStore(tmp_path / "fundamental_items")

    first_ref, first_written = store.append(_item())
    second_ref, second_written = store.append(_item())

    assert first_ref == second_ref
    assert first_written is True
    assert second_written is False
    assert len(store.read("SAP.DE")) == 1
    assert len(store.path_for_symbol("SAP.DE").read_text().splitlines()) == 1


def test_company_store_keeps_versions_and_current_depths(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")

    first_ref, first_written = store.append(_brief())
    _duplicate_ref, duplicate_written = store.append(_brief())
    deep_ref, deep_written = store.append(_brief(signature="sig-deep", depth="deep"))
    second_ref, second_written = store.append(_brief(signature="sig-2"))

    assert first_written is True
    assert duplicate_written is False
    assert deep_written is True
    assert second_written is True
    assert first_ref["brief_id"] != second_ref["brief_id"]
    assert deep_ref["depth"] == "deep"
    assert store.read_current("SAP.DE", depth="screen").input_signature == "sig-2"
    assert store.read_current("SAP.DE", depth="deep").input_signature == "sig-deep"
    assert len(store.read_history("SAP.DE")) == 3


def test_read_current_preferred_prefers_deep_then_screen(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")

    # screen only -> preferred returns the screen brief
    store.append(_brief(signature="sig-screen", depth="screen"))
    assert store.read_current("SAP.DE", depth="preferred").input_signature == "sig-screen"

    # both exist at the SAME as_of -> preferred returns deep (tie goes to deep)
    store.append(_brief(signature="sig-deep", depth="deep"))
    assert store.read_current("SAP.DE", depth="preferred").input_signature == "sig-deep"


def test_read_current_preferred_prefers_fresher_screen_after_universe_exit(tmp_path) -> None:
    """A symbol that left the universe keeps a stale deep brief; once its screen
    brief refreshes, preferred must serve the fresher screen, not the stale deep."""
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    store.append(_brief(signature="sig-deep-old", depth="deep", as_of="2026-07-10T01:00:00+00:00"))
    store.append(_brief(signature="sig-screen-new", depth="screen", as_of="2026-07-12T01:00:00+00:00"))
    assert store.read_current("SAP.DE", depth="preferred").input_signature == "sig-screen-new"


def test_read_current_preferred_reads_deep_only_symbol_and_none_when_absent(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    store.append(_brief(signature="sig-deep", depth="deep"))
    # a universe member has only a deep brief; a screen-default reader still sees it
    assert store.read_current("SAP.DE", depth="preferred").input_signature == "sig-deep"
    assert store.read_current("MISSING.XX", depth="preferred") is None


def test_company_store_rebuilds_corrupt_current_projection(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    brief = _brief()
    store.append(brief)
    store.current_path("SAP.DE").write_text("{not-json", encoding="utf-8")

    rebuilt = store.read_current("SAP.DE")

    assert rebuilt is not None
    assert rebuilt.brief_id == brief.brief_id
    envelope = json.loads(store.current_path("SAP.DE").read_text(encoding="utf-8"))
    assert envelope["briefs"]["screen"]["brief_id"] == brief.brief_id


def test_company_store_concurrent_duplicate_append_writes_once(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    brief = _brief()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: store.append(brief), range(8)))

    assert sum(1 for _ref, written in results if written) == 1
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1


def test_company_store_bulk_read_preserves_missing_symbols(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    store.append(_brief())

    rows = store.read_current_many(["SAP.DE", "AAPL", "SAP.DE"])

    assert rows["SAP.DE"] is not None
    assert rows["AAPL"] is None


def test_company_analysis_run_store_appends_and_refreshes_latest(tmp_path) -> None:
    store = CompanyAnalysisRunStore(tmp_path / "company_analysis_runs")
    record = {
        "run_id": "company-run-1",
        "symbol": "SAP.DE",
        "as_of": "2026-07-10T02:00:00+00:00",
        "status": "success",
        "brief_ref": _brief().ref(),
    }

    ref = store.append(record)

    assert ref["run_id"] == "company-run-1"
    assert store.read_latest("SAP.DE")["brief_ref"]["symbol"] == "SAP.DE"
    assert len(store.path_for_date("2026-07-10").read_text().splitlines()) == 1
