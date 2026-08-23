import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from trader.domain.company import CompanyEvidenceItem, CompanyIntelligenceBrief
from trader.infrastructure.state_db.availability_receipt import load_receipts, validate_availability_receipt
from trader.infrastructure.state_db.company_analysis_run_store import CompanyAnalysisRunStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.fundamental_item_store import FundamentalItemStore
from trader.infrastructure.state_db.world_context_reader import WorldContextReader


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


def test_company_append_recovers_clock_failure_after_raw_fsync(tmp_path) -> None:
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("clock failed")
        return datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)

    store = CompanyIntelligenceStore(tmp_path / "company_intelligence", clock=clock)
    brief = _brief()
    with pytest.raises(RuntimeError, match="clock failed"):
        store.append(brief)
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1
    assert not store.receipt_path("SAP.DE").exists()

    ref, written = store.append(brief)
    assert written is False
    assert ref["brief_id"] == brief.brief_id
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1
    receipts = load_receipts(store.receipt_path("SAP.DE"))
    assert len(receipts) == 1
    history_ref = {
        "path": str(store.history_path("SAP.DE").relative_to(store.base_dir)),
        "scope": brief.symbol,
        "encoding": "jsonl",
    }
    assert (
        validate_availability_receipt(
            receipts[0],
            brief.to_dict(),
            expected_artifact_id=brief.brief_id,
            expected_scope=brief.symbol,
            expected_history_path=history_ref["path"],
        )
        is not None
    )
    assert store.read_current("SAP.DE") is not None
    later = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=store,
        clock=lambda: datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc),
    )
    evidence = reader.lookup_company(symbol="SAP.DE", cutoff_at=later)
    assert evidence.status == "complete"
    assert evidence.proven is True


def test_company_append_recovers_crash_after_receipt_before_projection(tmp_path, monkeypatch) -> None:
    from trader.infrastructure.state_db.shadow import write_json_atomic

    store = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc),
    )
    brief = _brief()
    calls = {"n": 0}
    real_write = write_json_atomic

    def boom(path, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("projection failed")
        return real_write(path, payload)

    monkeypatch.setattr("trader.infrastructure.state_db.company_intelligence_store.write_json_atomic", boom)
    with pytest.raises(RuntimeError, match="projection failed"):
        store.append(brief)
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1
    assert len(load_receipts(store.receipt_path("SAP.DE"))) == 1
    assert not store.current_path("SAP.DE").exists()

    ref, written = store.append(brief)
    assert written is False
    assert ref["brief_id"] == brief.brief_id
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1
    assert len(load_receipts(store.receipt_path("SAP.DE"))) == 1
    current = store.read_current("SAP.DE")
    assert current is not None
    assert current.brief_id == brief.brief_id


def test_company_exact_duplicate_retry_does_not_regress_newer_current(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    old = _brief(signature="sig-1", as_of="2026-07-10T01:00:00+00:00")
    store.append(old)
    new = _brief(signature="sig-2", as_of="2026-07-12T01:00:00+00:00")
    store.append(new)
    assert store.read_current("SAP.DE").input_signature == "sig-2"
    _ref, written = store.append(old)
    assert written is False
    assert store.read_current("SAP.DE").input_signature == "sig-2"
    assert len(store.read_history("SAP.DE")) == 2
    assert len(load_receipts(store.receipt_path("SAP.DE"))) == 2


def test_company_append_does_not_receipt_divergent_same_signature(tmp_path) -> None:
    store = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    first = _brief(signature="sig-shared")
    store.append(first)
    divergent_payload = first.to_dict()
    divergent_payload["as_of"] = "2026-07-11T01:00:00+00:00"
    divergent = CompanyIntelligenceBrief.from_mapping(divergent_payload)
    assert divergent is not None
    assert divergent.input_signature == first.input_signature
    _ref, written = store.append(divergent)
    assert written is False
    assert len(store.history_path("SAP.DE").read_text().splitlines()) == 1
    assert store.read_current("SAP.DE").as_of == first.as_of


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


def test_company_analysis_run_store_keeps_success_and_exposes_failure_until_recovery(tmp_path) -> None:
    store = CompanyAnalysisRunStore(tmp_path / "company_analysis_runs")
    success = {
        "run_id": "company-run-success",
        "symbol": "SAP.DE",
        "as_of": "2026-07-10T02:00:00+00:00",
        "status": "success",
        "depth": "screen",
        "input_signature": "sig-1",
        "brief_ref": _brief().ref(),
    }
    failure = {
        "run_id": "company-run-error",
        "symbol": "SAP.DE",
        "as_of": "2026-07-10T02:30:00+00:00",
        "status": "error",
        "depth": "screen",
        "input_signature": "sig-2",
        "error_code": "timeout",
        "error_message": "provider unavailable",
        "retry_attempt": 1,
        "retry_delay_seconds": 1800,
        "next_retry_at": "2026-07-10T03:00:00+00:00",
    }

    store.append(success)
    store.append(failure)

    assert store.read_latest("SAP.DE") == {**success, "latest_failure": failure}

    recovered = {
        **success,
        "run_id": "company-run-recovered",
        "as_of": "2026-07-10T03:01:00+00:00",
        "input_signature": "sig-2",
    }
    store.append(recovered)

    assert store.read_latest("SAP.DE") == recovered
    rows = [json.loads(line) for line in store.path_for_date("2026-07-10").read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["success", "error", "success"]


def test_company_analysis_run_store_deduplicates_repeated_unchanged_observations(tmp_path) -> None:
    store = CompanyAnalysisRunStore(tmp_path / "company_analysis_runs")
    success = {
        "run_id": "company-run-success",
        "symbol": "SAP.DE",
        "as_of": "2026-07-10T02:00:00+00:00",
        "status": "success",
        "depth": "screen",
        "input_signature": "sig-1",
        "brief_ref": _brief().ref(),
    }
    unchanged = {
        **success,
        "run_id": "company-observation-1",
        "as_of": "2026-07-10T03:00:00+00:00",
        "status": "unchanged",
    }

    store.append(success)
    store.append(unchanged)
    store.append({**unchanged, "as_of": "2026-07-10T04:00:00+00:00"})

    assert store.read_latest("SAP.DE") == success
    assert len(store.path_for_date("2026-07-10").read_text(encoding="utf-8").splitlines()) == 1
