import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest

from trader.domain.situation import NewsMacroBrief
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.universe import candidate_scope_id
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore
from trader.runtime import news_macro_runtime


@pytest.fixture(autouse=True)
def _news_macro_disabled_by_default(monkeypatch):
    """Override the suite default only for explicit news-macro runtime tests.

    Global pass is disabled here so existing tests that assert exact ``skipped``
    lists keep passing; individual tests that exercise the GLOBAL pass call
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1") themselves.
    """

    monkeypatch.setenv("CASYS_NEWS_MACRO_ANALYST_ENABLED", "1")
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "0")


class FakeAnalyst:
    def __init__(self) -> None:
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        brief = NewsMacroBrief.from_mapping(
            {
                "brief_id": f"{request.as_of}|{request.venue}",
                "venue": request.venue,
                "as_of": request.as_of,
                "valid_until": request.valid_until,
                "input_refs": request.input_refs,
                "zones": {
                    request.venue: [
                        {
                            "point": "Risk-off pressure broadens",
                            "sources": ["u-eu"],
                            "symbols": list(request.candidate_symbols),
                            "direction": "risk_off",
                        }
                    ]
                },
            }
        )
        assert brief is not None
        return brief


class BrokenAnalyst:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        raise RuntimeError("llm down")


def test_news_macro_runner_is_non_blocking_single_flight_and_stoppable(caplog) -> None:
    caplog.set_level(logging.INFO, logger="casys-trader")
    started = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def tick_fn(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=2.0)
        return {"triggered": [], "skipped": [], "errors": []}

    runner = news_macro_runtime.NewsMacroAnalysisRunner(tick_fn=tick_fn, stop_timeout_s=0.1)

    first = runner.trigger(state_dir="state", venues=("EU",))
    assert first["triggered"] is True
    assert started.wait(timeout=1.0)
    assert runner.trigger(state_dir="state", venues=("EU",)) == {
        "triggered": False,
        "reason": "queued_latest",
    }

    release.set()
    first["_thread"].join(timeout=1.0)
    assert len(calls) == 2
    assert calls[0]["state_dir"] == "state"
    assert callable(calls[0]["stop_requested"])
    assert "news macro trigger coalesced pending_replaced=False venues=('EU',)" in caplog.text

    runner.stop()
    assert runner.trigger(state_dir="state") == {"triggered": False, "reason": "stopping"}


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_candidate_scope(
    state_dir,
    *,
    venue="EU",
    candidates=None,
    baseline=None,
    as_of="2026-07-09T08:00:00+00:00",
    scope_phase=None,
) -> str:
    if candidates is None:
        candidates = [
            {"symbol": "AIR.PA", "attractiveness": 0.8, "bias": "long"},
        ]
    if baseline is None:
        baseline = [item["symbol"] for item in candidates[:25]]
    scope_id = candidate_scope_id(venue, candidates, baseline, as_of)
    record = {
        "schema_version": 1,
        "candidate_scope_id": scope_id,
        "candidate_run_ids": [f"candidate-{venue}"],
        "venue": venue,
        "as_of": as_of,
        "candidates": candidates,
        "default_hotlist": baseline,
        "sticky_context_at_close": [],
    }
    if scope_phase is not None:
        record["scope_phase"] = scope_phase
    CandidateScopeStore(state_dir / "candidate_scopes").append(record)
    (state_dir / "venue_state.json").write_text(
        json.dumps(
            {
                "venues": {
                    venue: {
                        **record,
                        "last_close_at": as_of,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return scope_id


def _write_venue_state(state_dir) -> None:
    _write_candidate_scope(state_dir)


def _write_company_brief(state_dir, *, signature: str) -> None:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AIR.PA",
            "as_of": "2026-07-09T08:30:00+00:00",
            "input_signature": signature,
            "depth": "screen",
            "issuer_identity": {"issuer_name": "Airbus", "identity_status": "verified"},
            "coverage": {"status": "partial"},
            "business": {
                "summary": "Commercial aerospace leader.",
                "source_refs": [f"company:{signature}"],
                "freshness": {"freshness": "fresh"},
            },
            "company_thesis": {"status": "intact", "summary": f"Thesis {signature}"},
            "selection_view": {"posture": "neutral", "confidence": "medium"},
            "security_readiness": "conditional",
            "source_refs": [f"company:{signature}"],
        }
    )
    assert brief is not None
    CompanyIntelligenceStore(state_dir / "company_intelligence").append(brief)


def test_tick_news_macro_analysis_writes_jsonl_and_indexes_memory(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [
            {"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"},
            {"uuid": "u-us", "symbol": "SPY", "title": "US market note"},
        ],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    second = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    day_file = state_dir / "news_briefs" / "2026-07-09.jsonl"
    latest_file = state_dir / "news_briefs" / "latest-EU.jsonl"
    rows = [json.loads(line) for line in day_file.read_text(encoding="utf-8").splitlines()]

    assert len(analyst.requests) == 1
    assert analyst.requests[0].venue == "EU"
    assert analyst.requests[0].candidate_symbols == ("AIR.PA",)
    assert [item["uuid"] for item in analyst.requests[0].news_items] == ["u-eu"]
    assert result["triggered"][0]["venue"] == "EU"
    assert second["skipped"] == [
        {"venue": "EU", "reason": "active_brief_same_scope_and_inputs"}
    ]
    assert rows[0]["venue"] == "EU"
    assert analyst.requests[0].input_refs["candidate_scope_id"].startswith(
        "candidate_scope:v1:EU:"
    )
    assert latest_file.exists()
    assert SituationMemoryStore(state_dir / "situation_memory.db").count() == 1


def test_tick_news_macro_analysis_waits_for_preopen_child_scope(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_candidate_scope(state_dir, scope_phase="close")
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc),
        analyst=analyst,
        venues=("EU",),
    )

    assert analyst.requests == []
    assert result["triggered"] == []
    assert result["skipped"] == [
        {"venue": "EU", "reason": "awaiting_preopen_scope"}
    ]


def test_tick_news_macro_analysis_refreshes_active_brief_when_candidate_scope_changes(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [
            {"uuid": "u-air", "symbol": "AIR.PA", "title": "Airbus demand warning"},
            {"uuid": "u-sap", "symbol": "SAP.DE", "title": "SAP raises guidance"},
        ],
    )
    analyst = FakeAnalyst()

    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    first_scope = analyst.requests[0].input_refs["candidate_scope_id"]
    _write_candidate_scope(
        state_dir,
        candidates=[
            {"symbol": "AIR.PA", "attractiveness": 0.8, "bias": "long"},
            {"symbol": "SAP.DE", "attractiveness": 0.7, "bias": "long"},
        ],
        as_of="2026-07-09T15:30:00+00:00",
    )

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert result["triggered"][0]["venue"] == "EU"
    assert len(analyst.requests) == 2
    assert analyst.requests[1].candidate_symbols == ("AIR.PA", "SAP.DE")
    assert analyst.requests[1].input_refs["candidate_scope_id"] != first_scope


def test_tick_news_macro_analysis_refreshes_changed_inputs_after_cooldown(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-air-1", "symbol": "AIR.PA", "title": "Initial Airbus note"}],
    )
    analyst = FakeAnalyst()

    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [
            {"uuid": "u-air-1", "symbol": "AIR.PA", "title": "Initial Airbus note"},
            {"uuid": "u-air-2", "symbol": "AIR.PA", "title": "Overnight guidance cut"},
        ],
    )

    cooldown = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(hours=1),
        analyst=analyst,
        venues=("EU",),
    )
    refreshed = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(hours=5),
        analyst=analyst,
        venues=("EU",),
    )

    assert cooldown["skipped"] == [
        {"venue": "EU", "reason": "active_brief_changed_inputs_cooldown"}
    ]
    assert refreshed["triggered"][0]["venue"] == "EU"
    assert len(analyst.requests) == 2
    assert {item["uuid"] for item in analyst.requests[1].news_items} == {
        "u-air-1",
        "u-air-2",
    }


def test_new_company_brief_waits_for_regional_refresh_and_keeps_provenance(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-air-1", "symbol": "AIR.PA", "title": "Airbus note"}],
    )
    _write_company_brief(state_dir, signature="sig-1")
    analyst = FakeAnalyst()
    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    _write_company_brief(state_dir, signature="sig-2")

    deferred = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(hours=1),
        analyst=analyst,
        venues=("EU",),
    )
    refreshed = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(hours=5),
        analyst=analyst,
        venues=("EU",),
    )

    assert deferred["skipped"] == [
        {"venue": "EU", "reason": "active_brief_changed_inputs_cooldown"}
    ]
    assert refreshed["triggered"][0]["venue"] == "EU"
    assert len(analyst.requests) == 2
    assert analyst.requests[1].company_anchors["AIR.PA"]["brief_ref"]["input_signature"] == "sig-2"
    assert analyst.requests[1].input_refs["company_brief_refs"] == {
        "AIR.PA": analyst.requests[1].company_anchors["AIR.PA"]["brief_ref"]
    }


def test_force_can_refresh_new_company_brief_before_regional_cooldown(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-air-1", "symbol": "AIR.PA", "title": "Airbus note"}],
    )
    _write_company_brief(state_dir, signature="sig-1")
    analyst = FakeAnalyst()
    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    _write_company_brief(state_dir, signature="sig-2")

    refreshed = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(hours=1),
        analyst=analyst,
        venues=("EU",),
        force=True,
    )

    assert refreshed["triggered"][0]["venue"] == "EU"
    assert len(analyst.requests) == 2
    assert analyst.requests[1].company_anchors["AIR.PA"]["brief_ref"]["input_signature"] == "sig-2"


def test_tick_news_macro_analysis_reads_scope_store_when_venue_state_is_absent(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    (state_dir / "venue_state.json").unlink()
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-air", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert result["triggered"][0]["venue"] == "EU"
    assert analyst.requests[0].candidate_symbols == ("AIR.PA",)


def test_tick_news_macro_analysis_includes_macro_series_global_news_and_family_context(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [{"uuid": "u-air", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [
            {
                "uuid": "g-1",
                "title": "European defense spending debate intensifies",
                "publisher": "TestWire",
                "published_at": "2026-07-10T07:00:00+00:00",
                "regions": ["EU"],
                "families": ["eu_industrials"],
            }
        ],
    )
    _write_jsonl(
        state_dir / "macro_series" / "ecb_deposit_rate.jsonl",
        [
            {
                "ts_collected": "2026-07-10T06:00:00+00:00",
                "series_id": "ECB/FM/B.U2.EUR.4F.KR.DFR.LEV",
                "period": "2026-07-09",
                "value": 2.25,
            }
        ],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert result["triggered"][0]["venue"] == "EU"
    request = analyst.requests[0]
    assert [item["uuid"] for item in request.global_news_items] == ["g-1"]
    assert request.macro_series == (
        {
            "label": "ecb_deposit_rate",
            "series_id": "ECB/FM/B.U2.EUR.4F.KR.DFR.LEV",
            "period": "2026-07-09",
            "value": 2.25,
            "ts_collected": "2026-07-10T06:00:00+00:00",
        },
    )
    assert "eu_industrials" in request.family_context
    assert request.family_context["eu_industrials"]["candidate_symbols"] == ["AIR.PA"]
    assert request.family_context["eu_industrials"]["news_item_uuids"] == ["u-air"]
    assert request.input_refs["global_news_uuids"] == ["g-1"]
    assert request.input_refs["macro_series_labels"] == ["ecb_deposit_rate"]
    assert request.input_refs["families"] == ["eu_industrials"]
    assert request.input_refs["coverage"] == {
        "status": "partial",
        "candidate_count": 1,
        "candidates_with_news": 1,
        "news_items_injected": 1,
        "news_item_cap": 80,
        "news_cap_reached": False,
        "global_news_items_injected": 1,
        "global_headlines_status": "present",
        "macro_calendar_status": "fallback_only",
        "macro_series_count": 1,
        "macro_series_stale_labels": [],
    }


def test_tick_news_macro_analysis_can_digest_macro_context_without_symbol_news(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [{"uuid": "global-1", "title": "US yields jump", "regions": ["GLOBAL"]}],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert result["triggered"][0]["venue"] == "EU"
    assert analyst.requests[0].news_items == ()
    assert [item["uuid"] for item in analyst.requests[0].global_news_items] == ["global-1"]


def test_tick_news_macro_analysis_marks_missing_global_and_stale_macro_as_partial(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "macro_series" / "cpi_us_all_items.jsonl",
        [{"series_id": "BLS/cu/CUSR0000SA0", "period": "2025-01", "value": 319.1}],
    )
    analyst = FakeAnalyst()

    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    coverage = analyst.requests[0].input_refs["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["candidate_count"] == 1
    assert coverage["candidates_with_news"] == 0
    assert coverage["global_headlines_status"] == "missing"
    assert coverage["macro_calendar_status"] == "fallback_only"
    assert coverage["macro_series_stale_labels"] == ["cpi_us_all_items"]


def test_tick_news_macro_analysis_caps_symbol_news_per_venue(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [{"uuid": f"u-air-{idx}", "symbol": "AIR.PA", "title": f"Airbus item {idx}"} for idx in range(100)],
    )
    analyst = FakeAnalyst()

    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert len(analyst.requests[0].news_items) == 80
    assert analyst.requests[0].input_refs["news_item_count"] == 80


def test_tick_news_macro_analysis_keeps_all_candidates_and_attributed_challenger_evidence(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    radar_candidates = [
        {"symbol": f"EU{idx}.PA", "attractiveness": 1.0 - idx / 100, "bias": "long"}
        for idx in range(60)
    ]
    challenger = {
        "symbol": "SAP.DE",
        "attractiveness": 0.0,
        "bias": "neutral",
        "candidate_source": "fresh_news",
        "fresh_news": {
            "score": 92,
            "latest_published_at": "2026-07-10T08:00:00+00:00",
            "event_types": ["earnings_guidance"],
            "evidence": [
                {
                    "uuid": "u-sap",
                    "symbol": "SPY",
                    "fetched_for": "SPY",
                    "title": "SAP raises full-year cloud guidance",
                    "publisher": "Reuters",
                    "published_at": "2026-07-10T08:00:00+00:00",
                    "attribution": {"method": "legal_name", "matched": "sap"},
                    "event_type": "earnings_guidance",
                    "score": 92,
                },
                {
                    "uuid": "u-sap",
                    "title": "duplicate evidence must be removed",
                    "publisher": "Reuters",
                },
            ],
        },
    }
    _write_candidate_scope(
        state_dir,
        candidates=[*radar_candidates, challenger],
        as_of="2026-07-10T08:00:00+00:00",
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": "u-sap",
                "symbol": "SPY",
                "title": "raw fetch attribution must not win",
                "publisher": "RawWire",
            }
        ],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )

    assert result["triggered"][0]["venue"] == "EU"
    request = analyst.requests[0]
    assert len(request.candidate_symbols) == 61
    assert request.candidate_symbols[-1] == "SAP.DE"
    assert [item["uuid"] for item in request.news_items] == ["u-sap"]
    evidence = request.news_items[0]
    assert evidence["symbol"] == "SAP.DE"
    assert evidence["publisher"] == "Reuters"
    assert evidence["candidate_source"] == "fresh_news"
    assert evidence["attribution"]["method"] == "legal_name"


def test_tick_news_macro_analysis_large_raw_volume_does_not_hide_later_venue_news(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_candidate_scope(
        state_dir,
        venue="US",
        candidates=[{"symbol": "SPY", "attractiveness": 0.8, "bias": "long"}],
        as_of="2026-07-10T08:00:00+00:00",
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            *[
                {"uuid": f"u-air-{idx}", "symbol": "AIR.PA", "title": f"Airbus item {idx}"}
                for idx in range(2_100)
            ],
            {"uuid": "u-spy", "symbol": "SPY", "title": "SPY macro note"},
        ],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("US",),
    )

    assert result["triggered"][0]["venue"] == "US"
    assert [item["uuid"] for item in analyst.requests[0].news_items] == ["u-spy"]


def test_tick_news_macro_analysis_backs_off_changed_material_inputs_in_same_scope(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="casys-trader")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    analyst = BrokenAnalyst()

    first = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [
            {"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"},
            {
                "uuid": "u-eu-2",
                "symbol": "AIR.PA",
                "title": "Airbus guidance update",
                "fetched_at": "2026-07-09T09:03:00+00:00",
            },
        ],
    )
    second = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=4),
        analyst=analyst,
        venues=("EU",),
    )

    assert first["errors"][0]["code"] == "RuntimeError"
    failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="EU")
    assert failure is not None
    assert failure["retry_attempt"] == 1
    assert failure["retry_delay_seconds"] == 30 * 60
    assert failure["next_retry_at"] == (now + timedelta(minutes=30)).isoformat()
    assert failure["error"] == {"code": "RuntimeError", "message": "llm down"}
    assert second["skipped"] == [
        {
            "venue": "EU",
            "reason": "failure_backoff",
            "attempt": 1,
            "delay_seconds": 30 * 60,
            "next_retry_at": (now + timedelta(minutes=30)).isoformat(),
            "error": "RuntimeError",
        }
    ]
    assert analyst.calls == 1
    assert "news macro retry scheduled venue=EU" in caplog.text
    assert "news macro retry deferred venue=EU" in caplog.text

    retry = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=30),
        analyst=analyst,
        venues=("EU",),
    )

    assert retry["errors"][0]["attempt"] == 2
    assert analyst.calls == 2
    second_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="EU")
    assert second_failure is not None
    assert second_failure["retry_attempt"] == 2
    assert second_failure["retry_delay_seconds"] == 60 * 60
    assert second_failure["next_retry_at"] == (now + timedelta(minutes=90)).isoformat()


def test_news_macro_failure_delay_ladder_is_bounded() -> None:
    assert [news_macro_runtime._failure_delay_seconds(attempt) for attempt in range(1, 7)] == [
        30 * 60,
        60 * 60,
        120 * 60,
        240 * 60,
        360 * 60,
        360 * 60,
    ]


def test_tick_news_macro_analysis_new_regional_scope_resets_retry_lineage(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    analyst = BrokenAnalyst()

    news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
    )
    first_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="EU")
    assert first_failure is not None
    _write_candidate_scope(
        state_dir,
        as_of="2026-07-09T15:30:00+00:00",
    )

    reset = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=4),
        analyst=analyst,
        venues=("EU",),
    )

    assert reset["errors"][0]["attempt"] == 1
    assert analyst.calls == 2
    reset_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="EU")
    assert reset_failure is not None
    assert reset_failure["retry_lineage"] != first_failure["retry_lineage"]
    assert reset_failure["retry_attempt"] == 1


def test_macro_success_signature_ignores_freshness_timestamps() -> None:
    original = news_macro_runtime._input_signature(
        venue="EU",
        macro_next=({"event": "ECB", "at": "2026-07-10T12:00:00+00:00", "in_h": 3},),
        macro_series=(
            {
                "label": "ecb_deposit_rate",
                "value": 2.0,
                "ts_collected": "2026-07-09T08:00:00+00:00",
            },
        ),
        input_refs={
            "candidate_scope_id": "scope-1",
            "candidate_scope_as_of": "2026-07-09T08:00:00+00:00",
            "company_brief_refs": {"AIR.PA": {"brief_id": "brief-1", "as_of": "2026-07-09T08:00:00+00:00"}},
        },
    )
    timestamp_only = news_macro_runtime._input_signature(
        venue="EU",
        macro_next=({"event": "ECB", "at": "2026-07-10T12:00:00+00:00", "in_h": 1},),
        macro_series=(
            {
                "label": "ecb_deposit_rate",
                "value": 2.0,
                "ts_collected": "2026-07-09T08:04:00+00:00",
            },
        ),
        input_refs={
            "candidate_scope_id": "scope-1",
            "candidate_scope_as_of": "2026-07-09T08:04:00+00:00",
            "company_brief_refs": {"AIR.PA": {"brief_id": "brief-1", "as_of": "2026-07-09T08:04:00+00:00"}},
        },
    )
    material_change = news_macro_runtime._input_signature(
        venue="EU",
        macro_next=({"event": "ECB", "at": "2026-07-10T12:00:00+00:00"},),
        macro_series=(
            {
                "label": "ecb_deposit_rate",
                "value": 2.25,
                "ts_collected": "2026-07-09T08:04:00+00:00",
            },
        ),
        input_refs={"candidate_scope_id": "scope-1"},
    )

    assert original == timestamp_only
    assert original != material_change


def test_failure_keeps_last_successful_macro_brief_until_same_venue_recovers(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    healthy = FakeAnalyst()
    broken = BrokenAnalyst()

    first = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=healthy,
        venues=("EU",),
    )
    success_ref = first["triggered"][0]["brief_ref"]
    failed = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=1),
        analyst=broken,
        venues=("EU",),
        force=True,
    )
    before_deferred = json.loads((state_dir / "news_macro_analysis_status.json").read_text(encoding="utf-8"))
    deferred = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=2),
        analyst=broken,
        venues=("EU",),
    )
    after_deferred = json.loads((state_dir / "news_macro_analysis_status.json").read_text(encoding="utf-8"))

    assert failed["errors"][0]["code"] == "RuntimeError"
    assert before_deferred["EU"]["last_brief_ref"] == success_ref
    assert before_deferred["EU"]["last_success_at"] == now.isoformat()
    assert before_deferred["EU"]["latest_failure"]["retry_attempt"] == 1
    assert deferred["skipped"][0]["reason"] == "active_brief_same_scope_and_inputs"
    assert after_deferred == before_deferred
    latest_brief = json.loads((state_dir / "news_briefs" / "latest-EU.jsonl").read_text(encoding="utf-8"))
    assert latest_brief["brief_id"] == success_ref["brief_id"]

    recovered = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=3),
        analyst=healthy,
        venues=("EU",),
        force=True,
    )
    success_status = json.loads((state_dir / "news_macro_analysis_status.json").read_text(encoding="utf-8"))["EU"]
    assert recovered["triggered"][0]["venue"] == "EU"
    assert success_status["latest_failure"] is None
    assert success_status["last_failure_at"] is None


def test_latest_failure_projection_reads_legacy_macro_status(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy_failure = {
        "last_success_at": "2026-07-09T08:00:00+00:00",
        # Old releases overwrote this value during a failure.  It must not be
        # reused as the successful brief signature during migration.
        "last_input_signature": "failed-input",
        "last_failure_at": "2026-07-09T09:00:00+00:00",
        "last_error": {"code": "TimeoutError", "message": "provider unavailable"},
    }
    (state_dir / "news_macro_analysis_status.json").write_text(
        json.dumps({"EU": legacy_failure}),
        encoding="utf-8",
    )

    assert news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="eu") == {
        "failed_at": "2026-07-09T09:00:00+00:00",
        "error_code": "TimeoutError",
        "error_message": "provider unavailable",
        "error": {"code": "TimeoutError", "message": "provider unavailable"},
    }
    assert not news_macro_runtime._last_success_matches_signature(legacy_failure, "failed-input")


def test_tick_news_macro_analysis_force_bypasses_failed_signature_backoff(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    broken = BrokenAnalyst()
    recovered = FakeAnalyst()

    first = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=broken,
        venues=("EU",),
    )
    forced = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=recovered,
        venues=("EU",),
        force=True,
    )

    assert first["errors"][0]["code"] == "RuntimeError"
    assert forced["triggered"][0]["venue"] == "EU"
    assert len(recovered.requests) == 1
    status = json.loads((state_dir / "news_macro_analysis_status.json").read_text(encoding="utf-8"))["EU"]
    assert status["latest_failure"] is None
    assert status["last_failure_at"] is None


def test_tick_news_macro_analysis_can_exclude_global_explicitly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    _write_venue_state(state_dir)
    _write_jsonl(
        state_dir / "news_items" / "2026-07-09.jsonl",
        [{"uuid": "u-eu", "symbol": "AIR.PA", "title": "Airbus demand warning"}],
    )
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-09.jsonl",
        [{"uuid": "u-global", "title": "Global yields rise", "regions": ["GLOBAL"]}],
    )
    analyst = FakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=("EU",),
        include_global=False,
        force=True,
    )

    assert [item["venue"] for item in result["triggered"]] == ["EU"]
    assert [request.venue for request in analyst.requests] == ["EU"]


# ---------------------------------------------------------------------------
# Passe GLOBAL
# ---------------------------------------------------------------------------


class GlobalFakeAnalyst:
    """Analyste factice acceptant les requêtes GLOBAL (candidate_symbols vide)."""

    def __init__(self) -> None:
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        brief = NewsMacroBrief.from_mapping(
            {
                "brief_id": f"{request.as_of}|{request.venue}",
                "venue": request.venue,
                "as_of": request.as_of,
                "valid_until": request.valid_until,
                "input_refs": request.input_refs or {},
                "zones": {
                    request.venue: [
                        {
                            "point": "Geopolitical risk elevated",
                            "sources": [],
                            "symbols": [],
                            "direction": "risk_off",
                        }
                    ]
                },
            }
        )
        assert brief is not None
        return brief


def _write_gdelt_events(state_dir, events) -> None:
    path = state_dir / "gdelt" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(ev) + "\n" for ev in events), encoding="utf-8")


def test_global_pass_produces_brief_with_gdelt_and_global_news(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [{"uuid": "g-1", "title": "US yields spike", "regions": ["GLOBAL"]}],
    )
    _write_gdelt_events(
        state_dir,
        [
            {
                "ts_collected": "2026-07-10T08:00:00+00:00",
                "url": "https://reuters.com/a",
                "title": "Trade war escalates",
                "seendate": "20260710T080000Z",
                "domain": "reuters.com",
                "sourcecountry": "US",
                "language": "English",
            }
        ],
    )
    analyst = GlobalFakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=(),  # pas de boucle régionale
    )

    assert result["triggered"][0]["venue"] == "GLOBAL"
    assert len(analyst.requests) == 1
    req = analyst.requests[0]
    assert req.venue == "GLOBAL"
    assert len(req.geopolitical_events) == 1
    assert req.geopolitical_events[0]["url"] == "https://reuters.com/a"
    assert req.geopolitical_events[0]["title"] == "Trade war escalates"
    assert len(req.global_news_items) == 1
    assert req.global_news_items[0]["uuid"] == "g-1"
    assert req.input_refs["geopolitical_event_count"] == 1
    assert req.input_refs["global_news_uuids"] == ["g-1"]


def test_global_pass_skips_no_inputs(tmp_path, monkeypatch) -> None:
    """Skip no_inputs quand global_news, macro_series, gdelt et macro_next tous vides.

    On utilise une date après tous les événements DEFAULT_CALENDAR (FOMC 2026)
    pour garantir que macro_next soit vide sans dépendre de la liste codée en dur.
    """
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    # Dépasse tous les événements FOMC 2026 (le dernier est 2026-12-09) → macro_next vide.
    now = datetime(2027, 1, 1, 9, 0, tzinfo=timezone.utc)
    analyst = GlobalFakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=(),
    )

    assert result["triggered"] == []
    assert any(s["venue"] == "GLOBAL" and s["reason"] == "no_inputs" for s in result["skipped"])
    assert analyst.requests == []


def test_global_pass_dedup_same_inputs(tmp_path, monkeypatch) -> None:
    """Deuxième tick avec mêmes inputs → skip, pas de second appel analyste."""
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [{"uuid": "g-2", "title": "ECB holds rates", "regions": ["GLOBAL"]}],
    )
    _write_gdelt_events(
        state_dir,
        [
            {
                "ts_collected": "2026-07-10T07:00:00+00:00",
                "url": "https://ft.com/b",
                "title": "Sanctions widened",
                "seendate": "20260710T070000Z",
                "domain": "ft.com",
                "sourcecountry": "GB",
                "language": "English",
            }
        ],
    )
    analyst = GlobalFakeAnalyst()

    first = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=(),
    )
    second = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=(),
    )

    assert first["triggered"][0]["venue"] == "GLOBAL"
    assert len(analyst.requests) == 1, "deuxième tick doit être dédupliqué"
    assert any(s.get("venue") == "GLOBAL" and s.get("reason") == "active_brief_same_inputs" for s in second["skipped"])


def test_global_retry_lineage_is_anchored_to_last_successful_brief() -> None:
    bootstrap = news_macro_runtime._global_retry_lineage_key(last_brief_ref=None)
    same_bootstrap = news_macro_runtime._global_retry_lineage_key(last_brief_ref={})
    first_success = news_macro_runtime._global_retry_lineage_key(
        last_brief_ref={
            "brief_id": "global-brief-1",
            "as_of": "2026-07-10T09:00:00+00:00",
            "input_signature": "old-material-input",
        }
    )
    same_success = news_macro_runtime._global_retry_lineage_key(
        last_brief_ref={
            "brief_id": "global-brief-1",
            "as_of": "2026-07-10T10:00:00+00:00",
            "input_signature": "changed-material-input",
        }
    )
    next_success = news_macro_runtime._global_retry_lineage_key(
        last_brief_ref={"brief_id": "global-brief-2"}
    )

    assert bootstrap == same_bootstrap
    assert first_success == same_success
    assert bootstrap != first_success
    assert first_success != next_success


def test_global_failure_backoff_ignores_new_headlines_until_success_changes_epoch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [
            {
                "uuid": "g-1",
                "title": "Global yields rise",
                "regions": ["GLOBAL"],
                "fetched_at": "2026-07-10T08:00:00+00:00",
                "updates": [{"at": "2026-07-10T08:00:00+00:00"}],
            }
        ],
    )
    _write_gdelt_events(
        state_dir,
        [
            {
                "url": "https://example.test/geopolitics",
                "title": "Sanctions widened",
                "ts_collected": "2026-07-10T08:00:00+00:00",
                "updated_at": "2026-07-10T08:00:00+00:00",
            }
        ],
    )
    broken = BrokenAnalyst()

    first = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=broken,
        venues=(),
    )
    first_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="GLOBAL")
    assert first_failure is not None
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [
            {
                "uuid": "g-1",
                "title": "Global yields rise",
                "regions": ["GLOBAL"],
                "fetched_at": "2026-07-10T08:04:00+00:00",
                "updates": [{"at": "2026-07-10T08:04:00+00:00"}],
            }
        ],
    )
    _write_gdelt_events(
        state_dir,
        [
            {
                "url": "https://example.test/geopolitics",
                "title": "Sanctions widened",
                "ts_collected": "2026-07-10T08:04:00+00:00",
                "updated_at": "2026-07-10T08:04:00+00:00",
            }
        ],
    )
    deferred = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=4),
        analyst=broken,
        venues=(),
    )

    assert first["errors"][0]["code"] == "RuntimeError"
    assert deferred["skipped"][0]["reason"] == "failure_backoff"
    assert deferred["skipped"][0]["attempt"] == 1
    assert broken.calls == 1

    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [
            {
                "uuid": "g-1",
                "title": "Global yields rise",
                "regions": ["GLOBAL"],
                "fetched_at": "2026-07-10T08:04:00+00:00",
            },
            {"uuid": "g-2", "title": "New trade measure", "regions": ["GLOBAL"]},
        ],
    )
    still_deferred = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=5),
        analyst=broken,
        venues=(),
    )

    assert still_deferred["skipped"][0]["reason"] == "failure_backoff"
    assert still_deferred["skipped"][0]["attempt"] == 1
    assert broken.calls == 1

    retry = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=30),
        analyst=broken,
        venues=(),
    )
    second_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="GLOBAL")
    assert retry["errors"][0]["attempt"] == 2
    assert broken.calls == 2
    assert second_failure is not None
    assert second_failure["retry_lineage"] == first_failure["retry_lineage"]
    assert second_failure["retry_attempt"] == 2

    recovered = GlobalFakeAnalyst()
    forced = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=31),
        analyst=recovered,
        venues=(),
        force=True,
    )
    assert forced["triggered"][0]["venue"] == "GLOBAL"
    assert news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="GLOBAL") is None
    success_status = json.loads((state_dir / "news_macro_analysis_status.json").read_text(encoding="utf-8"))["GLOBAL"]

    after_success_failure = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=32),
        analyst=broken,
        venues=(),
        force=True,
    )
    post_success_failure = news_macro_runtime.latest_failure_for_venue(state_dir=state_dir, venue="GLOBAL")
    assert after_success_failure["errors"][0]["attempt"] == 1
    assert post_success_failure is not None
    assert post_success_failure["retry_lineage"] == news_macro_runtime._global_retry_lineage_key(
        last_brief_ref=success_status["last_brief_ref"]
    )
    assert post_success_failure["retry_lineage"] != first_failure["retry_lineage"]

    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [
            {"uuid": "g-1", "title": "Global yields rise", "regions": ["GLOBAL"]},
            {"uuid": "g-2", "title": "New trade measure", "regions": ["GLOBAL"]},
            {"uuid": "g-3", "title": "Fresh headline during outage", "regions": ["GLOBAL"]},
        ],
    )
    after_success_news = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now + timedelta(minutes=33),
        analyst=broken,
        venues=(),
    )
    assert after_success_news["skipped"][0]["reason"] == "failure_backoff"
    assert broken.calls == 3


def test_read_recent_gdelt_events_reads_and_caps(tmp_path) -> None:
    """_read_recent_gdelt_events lit les events et respecte le cap."""
    state_dir = tmp_path / "state"
    events = [
        {
            "ts_collected": f"2026-07-10T0{i}:00:00+00:00",
            "url": f"https://example.com/{i}",
            "title": f"Event {i}",
            "seendate": f"2026070{i}T000000Z",
            "domain": "example.com",
            "sourcecountry": "US",
            "language": "English",
        }
        for i in range(5)
    ]
    _write_gdelt_events(state_dir, events)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)

    result = news_macro_runtime._read_recent_gdelt_events(state_dir, now=now)

    assert len(result) == 5
    # Les plus récents en tête (reversed lines → event 4 en premier)
    assert result[0]["url"] == "https://example.com/4"
    assert result[-1]["url"] == "https://example.com/0"


def test_read_recent_gdelt_events_caps_at_max(tmp_path) -> None:
    state_dir = tmp_path / "state"
    events = [
        {"ts_collected": "2026-07-10T09:00:00+00:00", "url": f"https://x.com/{i}", "title": f"E{i}"}
        for i in range(80)
    ]
    _write_gdelt_events(state_dir, events)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)

    result = news_macro_runtime._read_recent_gdelt_events(state_dir, now=now)

    assert len(result) == news_macro_runtime.DEFAULT_MAX_GDELT_EVENTS


def test_read_recent_gdelt_events_missing_file(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)

    result = news_macro_runtime._read_recent_gdelt_events(state_dir, now=now)

    assert result == []


def test_global_pass_disabled_by_env(tmp_path, monkeypatch) -> None:
    """CASYS_NEWS_MACRO_GLOBAL_ENABLED=0 → pas de passe GLOBAL."""
    monkeypatch.setenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "0")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    _write_jsonl(
        state_dir / "macro_headlines" / "2026-07-10.jsonl",
        [{"uuid": "g-x", "title": "Global macro note", "regions": ["GLOBAL"]}],
    )
    analyst = GlobalFakeAnalyst()

    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        analyst=analyst,
        venues=(),
    )

    assert analyst.requests == []
    assert not any(t.get("venue") == "GLOBAL" for t in result["triggered"])
