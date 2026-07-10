from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from trader.application.universe import UniverseAgentDecision
from trader.domain.situation import NewsMacroBrief
from trader.domain.universe import candidate_scope_id
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.universe_run_store import UniverseRunStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime import universe_intelligence_runtime


NOW = datetime(2026, 7, 10, 20, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _universe_intelligence_enabled(monkeypatch):
    monkeypatch.setenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "1")


class FakeAgent:
    def __init__(self) -> None:
        self.requests = []

    def compose(self, request):
        self.requests.append(request)
        selected = request.candidate_symbols[-1]
        family = request.candidates[-1]["family"]
        return UniverseAgentDecision(
            selected_hotlist=(selected,),
            summary=f"Prefer {selected}",
            family_postures={family: "favor"},
            symbol_rationales={selected: "Best combined family and situation signal."},
            contract_version="universe.v1",
            provider="test-provider",
            model="test-model",
            provider_fallback_reason="primary:retryable",
        )


def _write_scope_and_brief(
    state_dir,
    *,
    venue: str,
    symbols: list[str],
    family: str,
    scope_phase: str | None = None,
) -> str:
    candidates = [
        {
            "symbol": symbol,
            "attractiveness": 1.0 - index / 10,
            "bias": "long",
            "family": family,
            "candidate_source": "radar",
        }
        for index, symbol in enumerate(symbols)
    ]
    baseline = symbols[:1]
    scope_as_of = "2026-07-10T20:00:00+00:00"
    scope_id = candidate_scope_id(venue, candidates, baseline, scope_as_of)
    scope_record = {
            "schema_version": 1,
            "candidate_scope_id": scope_id,
            "candidate_run_ids": [f"candidate-{venue}"],
            "venue": venue,
            "as_of": scope_as_of,
            "candidates": candidates,
            "default_hotlist": baseline,
            "sticky_context_at_close": [],
        }
    if scope_phase is not None:
        scope_record["scope_phase"] = scope_phase
    CandidateScopeStore(state_dir / "candidate_scopes").append(scope_record)
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": f"brief-{venue}",
            "venue": venue,
            "as_of": "2026-07-10T20:01:00+00:00",
            "valid_until": "2026-07-11T16:01:00+00:00",
            "input_refs": {
                "candidate_scope_id": scope_id,
                "candidate_symbols": symbols,
                "coverage": {
                    "status": "partial",
                    "candidate_count": len(symbols),
                    "candidates_with_news": 1,
                    "news_items_injected": 2,
                    "news_cap_reached": False,
                    "global_headlines_status": "present",
                    "macro_series_count": 1,
                    "macro_series_stale_labels": [],
                },
            },
            "zones": {
                "GLOBAL": [
                    {
                        "point": f"Global context for {venue}",
                        "sources": ["Reuters"],
                        "source_refs": [f"global-{venue}"],
                        "signal": "strong",
                    }
                ]
            },
            "families": {
                family: [
                    {
                        "point": f"{family} breadth improves",
                        "sources": ["Reuters"],
                        "source_refs": [f"family-{venue}"],
                        "signal": "strong",
                    }
                ]
            },
            "symbols": {
                symbols[-1]: [
                    {
                        "point": "Company catalyst",
                        "sources": ["Reuters"],
                        "source_refs": [f"symbol-{venue}"],
                        "signal": "event",
                    }
                ],
                "OUTSIDE": [{"point": "Must be filtered"}],
            },
        }
    )
    assert brief is not None
    NewsMacroBriefStore(state_dir / "news_briefs").append(brief)
    return scope_id


def test_tick_prepares_three_independent_venue_runs_with_briefs_and_families(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scopes = {
        "TW": _write_scope_and_brief(
            state_dir, venue="TW", symbols=["2330.TW", "2454.TW"], family="semis_tw"
        ),
        "EU": _write_scope_and_brief(
            state_dir, venue="EU", symbols=["AIR.PA", "SAP.DE"], family="eu_industrials"
        ),
        "US": _write_scope_and_brief(
            state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
        ),
    }
    (state_dir / "last_regime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-10T19:00:00+00:00",
                "coverage": {"status": "partial", "basis": "active_tradable_universe"},
                "regime_families": {
                    "semis_tw": {"dir": "up", "frac": 0.8},
                    "eu_industrials": {"dir": "mixed", "frac": 0.5},
                    "us_mega_tech": {"dir": "down", "frac": 0.7},
                },
            }
        ),
        encoding="utf-8",
    )
    agent = FakeAgent()

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
    )

    assert [item["venue"] for item in result["prepared"]] == ["TW", "EU", "US"]
    assert len(agent.requests) == 3
    board_rows = (
        state_dir / "global_family_boards" / "2026-07-10.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(board_rows) == 1
    for request in agent.requests:
        assert request.candidate_scope_id == scopes[request.venue]
        assert request.retrieval_status == "not_enabled"
        assert request.retrieval_refs == ()
        assert request.global_family_board["status"] == "complete"
        assert request.global_family_board["role"] == (
            "comparative_context_not_capital_allocation"
        )
        assert set(request.global_family_board["venues"]) == {"TW", "EU", "US"}
        context = request.situation_context.to_dict()
        assert context["status"] == "active"
        assert context["coverage"]["status"] == "partial"
        assert context["coverage"]["global_headlines"] == "present"
        assert "GLOBAL" in context["zones"]
        assert "OUTSIDE" not in context["symbols"]
        assert "input_refs" not in json.dumps(context)
        assert request.company_context.mode == "active"
        assert request.company_context.coverage["missing"] == 2

        prepared = UniverseRunStore(state_dir / "universe_runs").read_prepared(
            request.candidate_scope_id
        )
        assert prepared is not None
        assert prepared["status"] == "success"
        assert prepared["fallback_used"] is False
        assert prepared["brief_ref"]["venue"] == request.venue
        assert prepared["candidate_run_ids"] == [f"candidate-{request.venue}"]
        assert prepared["retrieval_status"] == "not_enabled"
        assert prepared["agent_provider"] == "test-provider"
        assert prepared["agent_model"] == "test-model"
        assert prepared["agent_provider_fallback_reason"] == "primary:retryable"
        assert prepared["request_payload_hash"]
        assert prepared["market_context_status"] == "partial"
        assert prepared["market_context"]["as_of"] == "2026-07-10T19:00:00+00:00"
        assert request.candidates[-1]["family"] in prepared["family_snapshot"]
        assert prepared["family_snapshot"][request.candidates[-1]["family"]][
            "regime_status"
        ] == "observed"
        assert prepared["global_family_board"]["board_id"] == (
            request.global_family_board["board_id"]
        )
        assert prepared["company_context_mode"] == "active"
        assert prepared["company_context_coverage"]["missing"] == 2
        mandate = UniverseMandateStore(state_dir / "universe_mandates").read_prepared(
            request.candidate_scope_id
        )
        assert mandate is not None
        assert mandate["status"] == "prepared"
        assert list(mandate["symbols"]) == [request.candidate_symbols[-1]]
        assert mandate["symbols"][request.candidate_symbols[-1]]["why_selected"]


def test_tick_universe_intelligence_waits_for_preopen_child_scope(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_scope_and_brief(
        state_dir,
        venue="US",
        symbols=["AAPL", "MSFT"],
        family="us_mega_tech",
        scope_phase="close",
    )
    agent = FakeAgent()

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("US",),
    )

    assert agent.requests == []
    assert result["prepared"] == []
    assert result["skipped"] == [
        {"venue": "US", "reason": "awaiting_preopen_scope"}
    ]


def test_tick_records_brief_missing_once_without_calling_agent(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    candidates = [{"symbol": "AAPL", "attractiveness": 1.0, "bias": "long"}]
    scope_id = candidate_scope_id("US", candidates, ["AAPL"], NOW.isoformat())
    CandidateScopeStore(state_dir / "candidate_scopes").append(
        {
            "candidate_scope_id": scope_id,
            "venue": "US",
            "as_of": NOW.isoformat(),
            "candidates": candidates,
            "default_hotlist": ["AAPL"],
        }
    )
    agent = FakeAgent()

    first = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("US",),
    )
    second = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("US",),
    )

    assert first["waiting"] == [{"venue": "US", "reason": "brief_missing"}]
    assert second["skipped"] == [{"venue": "US", "reason": "brief_missing_already_recorded"}]
    assert agent.requests == []
    rows = (state_dir / "universe_runs" / "2026-07-10.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["status"] == "waiting_brief"


def test_tick_repairs_a_missing_prepared_projection_without_recalling_agent(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["AIR.PA", "SAP.DE"],
        family="eu_industrials",
    )
    agent = FakeAgent()

    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("EU",),
    )
    store = UniverseRunStore(state_dir / "universe_runs")
    store.prepared_path_for_scope(scope_id).unlink()

    repaired = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("EU",),
    )

    assert len(agent.requests) == 1
    assert repaired["prepared"][0]["repaired"] is True
    assert store.read_prepared(scope_id)["status"] == "success"


def test_tick_never_appends_success_when_prepared_write_fails(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir,
        venue="US",
        symbols=["AAPL", "MSFT"],
        family="us_mega_tech",
    )

    class FailingPreparedStore(UniverseRunStore):
        def write_prepared(self, scope_id, record):
            raise OSError("disk full")

    store = FailingPreparedStore(state_dir / "universe_runs")
    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=FakeAgent(),
        run_store=store,
        venues=("US",),
    )

    assert result["prepared"] == []
    assert result["errors"][0]["reason"] == "prepared_write_error"
    assert store.read_latest("US")["status"] == "error"
    assert store.read_latest("US")["error_code"] == "prepared_write_error"
    assert store.read_prepared(scope_id) is None


def test_family_board_persistence_failure_does_not_block_preparation(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir,
        venue="US",
        symbols=["AAPL", "MSFT"],
        family="us_mega_tech",
    )

    class BrokenBoardStore:
        def append_if_changed(self, _record):
            raise OSError("board disk unavailable")

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=FakeAgent(),
        family_board_store=BrokenBoardStore(),
        venues=("US",),
    )

    assert result["prepared"][0]["candidate_scope_id"] == scope_id
    prepared = UniverseRunStore(state_dir / "universe_runs").read_prepared(scope_id)
    assert prepared["status"] == "success"
    assert prepared["global_family_board"]["ref"]["persistence_status"] == "error"


def test_market_context_drops_stale_regime_values_but_keeps_status(tmp_path) -> None:
    path = tmp_path / "last_regime.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-01T00:00:00+00:00",
                "coverage": {"status": "partial"},
                "regime_families": {"us_mega_tech": {"dir": "up", "frac": 0.8}},
            }
        ),
        encoding="utf-8",
    )

    context = universe_intelligence_runtime._load_market_context(path, now=NOW)

    assert context["status"] == "stale"
    assert context["regime_families"] == {}
    assert context["as_of"] == "2026-07-01T00:00:00+00:00"


def test_tick_rejects_brief_for_another_candidate_scope(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
    )
    latest_path = state_dir / "news_briefs" / "latest-US.jsonl"
    payload = json.loads(latest_path.read_text())
    payload["input_refs"]["candidate_scope_id"] = "another-scope"
    latest_path.write_text(json.dumps(payload) + "\n")
    agent = FakeAgent()

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        venues=("US",),
    )

    assert result["waiting"] == [{"venue": "US", "reason": "brief_scope_mismatch"}]
    assert agent.requests == []
    assert UniverseRunStore(state_dir / "universe_runs").read_prepared(scope_id) is None


def test_runner_is_non_blocking_and_coalesces_latest_trigger() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []

    def tick_fn(**kwargs):
        calls.append(kwargs["loop_now"])
        started.set()
        assert release.wait(timeout=2.0)
        return {"prepared": [], "waiting": [], "skipped": [], "errors": []}

    runner = universe_intelligence_runtime.UniverseIntelligenceRunner(
        tick_fn=tick_fn,
        stop_timeout_s=0.1,
    )
    first = runner.trigger(loop_now="first")
    assert first["triggered"] is True
    assert started.wait(timeout=1.0)
    queued = runner.trigger(loop_now="second")
    assert queued == {"triggered": False, "reason": "queued_latest"}

    release.set()
    first["_thread"].join(timeout=2.0)
    assert calls == ["first", "second"]
    runner.stop()
