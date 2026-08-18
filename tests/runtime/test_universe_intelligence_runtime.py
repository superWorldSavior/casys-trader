from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import logging

from trader.application.universe import UniverseAgentDecision
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.situation import NewsMacroBrief
from trader.domain.universe import candidate_scope_id
from trader.domain.universe.global_posture import GlobalUniversePosture
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.global_situation_digest_store import GlobalSituationDigestStore
from trader.infrastructure.state_db.global_universe_posture_store import GlobalUniversePostureStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.universe_run_store import UniverseRunStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime import universe_intelligence_runtime


NOW = datetime(2026, 7, 10, 20, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _universe_intelligence_enabled(monkeypatch):
    monkeypatch.setenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "1")
    monkeypatch.setattr(
        universe_intelligence_runtime,
        "LlmGlobalPostureAgent",
        FakePostureAgent,
    )


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


class FakePostureAgent:
    def __init__(self) -> None:
        self.requests = []

    def compose(self, request):
        self.requests.append(request)
        return GlobalUniversePosture(
            as_of=request.as_of,
            venue_posture={venue: "selective" for venue in request.venues},
            family_priority={"favored": ("us_mega_tech",)},
            gross_mode="cautious",
            net_bias="long",
            rationale="Prefer the strongest global family while keeping gross bounded.",
        )


class FailingAgent:
    def __init__(self) -> None:
        self.requests = []

    def compose(self, request):
        self.requests.append(request)
        raise TimeoutError("model deadline exceeded")


def _write_scope_and_brief(
    state_dir,
    *,
    venue: str,
    symbols: list[str],
    family: str,
    scope_phase: str | None = None,
    sticky: tuple[str, ...] = (),
    write_brief: bool = True,
    reuse_scope_id: str | None = None,
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
    scope_id = reuse_scope_id or candidate_scope_id(venue, candidates, baseline, scope_as_of)
    scope_record = {
            "schema_version": 1,
            "candidate_scope_id": scope_id,
            "candidate_run_ids": [f"candidate-{venue}"],
            "venue": venue,
            "as_of": scope_as_of,
            "candidates": candidates,
            "default_hotlist": baseline,
            "sticky_context_at_close": list(sticky),
        }
    if scope_phase is not None:
        scope_record["scope_phase"] = scope_phase
    if reuse_scope_id is None:
        CandidateScopeStore(state_dir / "candidate_scopes").append(scope_record)
    if not write_brief:
        return scope_id
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


def _write_company_brief(state_dir, *, symbol: str, input_signature: str) -> dict[str, str]:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": symbol,
            "as_of": "2026-07-10T20:04:00+00:00",
            "input_signature": input_signature,
            "depth": "screen",
            "issuer_identity": {
                "issuer_name": symbol,
                "identity_status": "verified",
            },
            "coverage": {"status": "partial"},
            "business": {
                "summary": f"Business context for {symbol}",
                "source_refs": [f"source:{symbol}"],
                "freshness": {"freshness": "fresh"},
            },
            "company_thesis": {
                "status": "intact",
                "summary": f"Thesis for {symbol}",
            },
            "selection_view": {
                "posture": "supports_selection",
                "confidence": "medium",
                "reasons": ["Fresh company evidence"],
            },
            "security_readiness": "conditional",
            "source_refs": [f"source:{symbol}"],
        }
    )
    assert brief is not None
    ref, written = universe_intelligence_runtime.CompanyIntelligenceStore(
        state_dir / "company_intelligence"
    ).append(brief)
    assert written is True
    return ref


def test_build_prepared_mandate_preserves_decided_family_and_symbol_contexts() -> None:
    request = SimpleNamespace(
        candidates=({"symbol": "AIR.PA", "family": "defense_aero_eu"},),
        family_snapshot={"defense_aero_eu": {"regime_status": "observed"}},
        company_context=SimpleNamespace(symbols={"AIR.PA": {}}),
        candidate_scope_id="scope-eu",
        venue="EU",
        as_of="2026-07-11T08:00:00+00:00",
    )
    decision = SimpleNamespace(
        selected_hotlist=("AIR.PA",),
        family_postures={"defense_aero_eu": "favored"},
        symbol_mandates={
            "AIR.PA": {
                "why_selected": "Defense demand remains supportive.",
                "role": "core_candidate",
                "posture": "constructive",
                "directional_view": "long_bias",
                "portfolio_context": {"exposure_note": "Keep sector exposure bounded."},
            }
        },
        symbol_rationales={},
        portfolio_posture={"gross_mode": "cautious", "net_bias": "neutral", "notes": ["Risk"]},
    )

    mandate = universe_intelligence_runtime._build_prepared_mandate(
        request=request,
        decision=decision,
        record={"agent_run_id": "run-eu", "as_of": request.as_of},
        valid_until="2026-07-12T08:00:00+00:00",
    )

    symbol_mandate = mandate.symbols["AIR.PA"]
    assert mandate.family_postures["defense_aero_eu"] == "favored"
    assert symbol_mandate.family_context == {
        "family": "defense_aero_eu",
        "posture": "favored",
    }
    assert symbol_mandate.directional_view == "long_bias"
    assert symbol_mandate.portfolio_context == {
        "exposure_note": "Keep sector exposure bounded."
    }
    assert mandate.portfolio_posture == {
        "gross_mode": "cautious",
        "net_bias": "neutral",
        "notes": ["Risk"],
    }


def test_tick_prepares_three_independent_venue_runs_with_briefs_and_families(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scopes = {
        "TW": _write_scope_and_brief(
            state_dir,
            venue="TW",
            symbols=["2330.TW", "2454.TW"],
            family="semis_tw",
            sticky=("1101.TW",),
        ),
        "EU": _write_scope_and_brief(
            state_dir,
            venue="EU",
            symbols=["AIR.PA", "SAP.DE"],
            family="eu_industrials",
            sticky=("SIE.DE",),
        ),
        "US": _write_scope_and_brief(
            state_dir,
            venue="US",
            symbols=["AAPL", "MSFT"],
            family="us_mega_tech",
            sticky=("SIE.DE", "NVDA"),
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
    posture_agent = FakePostureAgent()

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=posture_agent,
    )

    assert [item["venue"] for item in result["prepared"]] == ["TW", "EU", "US"]
    assert len(agent.requests) == 3
    assert len(posture_agent.requests) == 1
    assert posture_agent.requests[0].sticky == ("1101.TW", "NVDA", "SIE.DE")
    assert posture_agent.requests[0].venues == ("TW", "EU", "US")
    board_rows = (
        state_dir / "global_family_boards" / "2026-07-10.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(board_rows) == 1
    digest_rows = (
        state_dir / "global_situation_digests" / "2026-07-10.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(digest_rows) == 1
    posture_rows = (
        state_dir / "global_universe_postures" / "2026-07-10.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(posture_rows) == 1
    assert len({json.dumps(request.global_situation_digest, sort_keys=True) for request in agent.requests}) == 1
    for request in agent.requests:
        assert request.candidate_scope_id == scopes[request.venue]
        assert request.retrieval_status == "not_enabled"
        assert request.retrieval_refs == ()
        assert request.global_family_board["status"] == "complete"
        assert request.global_family_board["role"] == (
            "comparative_context_not_capital_allocation"
        )
        assert set(request.global_family_board["venues"]) == {"TW", "EU", "US"}
        assert request.global_situation_digest["coverage"]["venues_seen"] == ["EU", "TW", "US"]
        assert request.global_family_board["global_situation_digest_ref"]["digest_id"] == (
            request.global_situation_digest["digest_id"]
        )
        assert request.global_universe_posture["gross_mode"] == "cautious"
        assert request.to_dict()["global_universe_posture"] == request.global_universe_posture
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
        assert prepared["global_situation_digest"]["digest_id"] == (
            request.global_situation_digest["digest_id"]
        )
        assert prepared["global_universe_posture"]["posture_id"] == (
            request.global_universe_posture["posture_id"]
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


def test_tick_uses_tool_loop_with_existing_company_store(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["SAP.DE"],
        family="eu_tech",
    )
    companies = universe_intelligence_runtime.CompanyIntelligenceStore(
        state_dir / "company_intelligence"
    )
    router = object()
    captured = {}

    monkeypatch.setattr(
        universe_intelligence_runtime,
        "build_universe_router_from_env",
        lambda: router,
    )

    def compose_with_tools(request, *, router, intelligence_store, timeout_s):
        captured.update(
            {
                "request": request,
                "router": router,
                "intelligence_store": intelligence_store,
                "timeout_s": timeout_s,
            }
        )
        return UniverseAgentDecision(
            selected_hotlist=("SAP.DE",),
            summary="Prefer SAP after exact company review.",
            family_postures={"eu_tech": "constructive"},
            symbol_rationales={"SAP.DE": "Best evidence."},
            contract_version="universe.v1",
            provider="test-provider",
            model="test-model",
        )

    monkeypatch.setattr(
        universe_intelligence_runtime,
        "compose_with_tool_loop",
        compose_with_tools,
    )

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        company_store=companies,
        venues=("EU",),
    )

    assert result["prepared"][0]["venue"] == "EU"
    assert captured["router"] is router
    assert captured["intelligence_store"] is companies
    assert captured["timeout_s"] == 120


def test_global_posture_agent_failure_is_fail_open(tmp_path, caplog) -> None:
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
    agent = FakeAgent()

    class FailingPostureAgent:
        def compose(self, _request):
            raise RuntimeError("posture unavailable")

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=FailingPostureAgent(),
        venues=("US",),
    )

    assert result["prepared"][0]["candidate_scope_id"] == scope_id
    assert len(agent.requests) == 1
    assert agent.requests[0].global_universe_posture == {}
    prepared = UniverseRunStore(state_dir / "universe_runs").read_prepared(scope_id)
    assert prepared["status"] == "success"
    assert prepared["global_universe_posture"]["status"] == "error"
    assert prepared["global_universe_posture"]["error"] == "RuntimeError"
    assert "global universe posture preparation failed" in caplog.text


def test_global_posture_store_read_failure_does_not_abort_tick(tmp_path) -> None:
    """A posture store read failure must only skip the (advisory) posture, never
    abort the surrounding venue preparation (Finding 1 — fail-open restored)."""
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
    )
    agent = FakeAgent()

    class BrokenPostureStore:
        def read_current(self):
            raise OSError("posture store unavailable")

        def append_if_changed(self, _posture):  # pragma: no cover - must not run
            raise AssertionError("append must not be reached when read fails")

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_store=BrokenPostureStore(),
        venues=("US",),
    )

    assert result["prepared"][0]["candidate_scope_id"] == scope_id
    prepared = UniverseRunStore(state_dir / "universe_runs").read_prepared(scope_id)
    assert prepared["global_universe_posture"]["status"] == "error"


def test_global_posture_failure_backoff_preserves_current_and_ignores_input_churn(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="casys-trader")
    scopes = CandidateScopeStore(tmp_path / "candidate_scopes")
    store = GlobalUniversePostureStore(tmp_path / "global_universe_postures")
    current, _, _ = store.append_if_changed(
        GlobalUniversePosture(
            as_of=(NOW - timedelta(hours=5)).isoformat(),
            venue_posture={"TW": "watch", "EU": "selective", "US": "favor"},
            gross_mode="cautious",
            net_bias="long",
            rationale="Last usable global posture.",
        )
    )

    class FailingPostureAgent:
        def __init__(self) -> None:
            self.requests = []

        def compose(self, request):
            self.requests.append(request)
            raise TimeoutError("model deadline exceeded")

    agent = FailingPostureAgent()

    first, first_ref = universe_intelligence_runtime._prepare_global_universe_posture(
        scopes=scopes,
        store=store,
        now=NOW,
        log=logging.getLogger("casys-trader"),
        global_family_board={"board_id": "board-before"},
        global_situation_digest={"digest_id": "digest-before"},
        agent=agent,
        cooldown_hours=4,
    )

    failure = store.read_latest_failure()
    assert first == current
    assert first_ref["status"] == "error"
    assert first_ref["persistence_status"] == "failure_recorded"
    assert store.read_current() == current
    assert failure is not None
    assert failure["current_posture_id"] == current["posture_id"]
    assert failure["retry_attempt"] == 1
    assert failure["retry_delay_seconds"] == 30 * 60

    deferred, deferred_ref = universe_intelligence_runtime._prepare_global_universe_posture(
        scopes=scopes,
        store=store,
        now=NOW + timedelta(minutes=1),
        log=logging.getLogger("casys-trader"),
        global_family_board={"board_id": "board-changed"},
        global_situation_digest={"digest_id": "digest-changed"},
        agent=agent,
        cooldown_hours=4,
    )

    assert deferred == current
    assert deferred_ref["status"] == "deferred"
    assert deferred_ref["refresh_reason"] == "failure_backoff"
    assert deferred_ref["retry_attempt"] == 1
    assert len(agent.requests) == 1
    assert store.read_latest_failure() == failure
    assert "global posture retry scheduled attempt=1 delay_s=1800" in caplog.text
    assert "global posture retry deferred attempt=1 delay_s=1800" in caplog.text


def test_global_posture_failure_backoff_is_exponential_and_bounded(tmp_path) -> None:
    scopes = CandidateScopeStore(tmp_path / "candidate_scopes")
    store = GlobalUniversePostureStore(tmp_path / "global_universe_postures")

    class FailingPostureAgent:
        def __init__(self) -> None:
            self.requests = []

        def compose(self, request):
            self.requests.append(request)
            raise RuntimeError("posture unavailable")

    agent = FailingPostureAgent()
    attempt_at = NOW
    expected_minutes = (30, 60, 120, 240, 360, 360)
    for attempt, delay_minutes in enumerate(expected_minutes, start=1):
        posture, ref = universe_intelligence_runtime._prepare_global_universe_posture(
            scopes=scopes,
            store=store,
            now=attempt_at,
            log=logging.getLogger("casys-trader"),
            global_family_board={"board_id": f"board-{attempt}"},
            global_situation_digest={"digest_id": f"digest-{attempt}"},
            agent=agent,
            cooldown_hours=4,
        )

        failure = store.read_latest_failure()
        assert posture == {}
        assert ref["status"] == "error"
        assert failure is not None
        assert failure["retry_attempt"] == attempt
        assert failure["retry_delay_seconds"] == delay_minutes * 60
        assert failure["next_retry_at"] == (
            attempt_at + timedelta(minutes=delay_minutes)
        ).isoformat()
        attempt_at += timedelta(minutes=delay_minutes)

    assert len(agent.requests) == len(expected_minutes)


def test_global_posture_success_clears_failure_and_resets_retry_sequence(tmp_path) -> None:
    scopes = CandidateScopeStore(tmp_path / "candidate_scopes")
    store = GlobalUniversePostureStore(tmp_path / "global_universe_postures")

    class FailingPostureAgent:
        def compose(self, _request):
            raise RuntimeError("posture unavailable")

    common = {
        "scopes": scopes,
        "store": store,
        "log": logging.getLogger("casys-trader"),
        "global_family_board": {"board_id": "board-1"},
        "global_situation_digest": {"digest_id": "digest-1"},
        "cooldown_hours": 4,
    }
    universe_intelligence_runtime._prepare_global_universe_posture(
        **common,
        now=NOW,
        agent=FailingPostureAgent(),
    )
    assert store.read_latest_failure()["retry_attempt"] == 1

    recovered_at = NOW + timedelta(minutes=30)
    recovered, recovered_ref = universe_intelligence_runtime._prepare_global_universe_posture(
        **common,
        now=recovered_at,
        agent=FakePostureAgent(),
    )

    assert recovered_ref["persistence_status"] == "appended"
    stored = store.read_current()
    assert universe_intelligence_runtime._consumer_posture(stored) == recovered
    assert "input_coverage" in stored
    assert store.read_latest_failure() is None

    after_success, after_success_ref = universe_intelligence_runtime._prepare_global_universe_posture(
        **common,
        now=recovered_at + timedelta(hours=4, minutes=1),
        agent=FailingPostureAgent(),
    )

    failure = store.read_latest_failure()
    assert after_success == recovered
    assert after_success_ref["status"] == "error"
    assert failure is not None
    assert failure["retry_attempt"] == 1
    assert failure["current_posture_id"] == recovered["posture_id"]


def test_decide_refresh_cooldown_zero_never_forces_refresh() -> None:
    old = _posture_current((NOW - timedelta(days=3)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=old, preopen_now=(), cooldown=timedelta(0), preopen_window=_WINDOW
    ) == (False, "fresh")


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


def test_micro_refresh_is_venue_explicit_and_keeps_exact_company_provenance(tmp_path) -> None:
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
    first = UniverseRunStore(state_dir / "universe_runs").read_latest("EU")
    company_ref = _write_company_brief(
        state_dir,
        symbol="SAP.DE",
        input_signature="company-sap-v2",
    )

    ordinary = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(minutes=1),
        agent=agent,
        venues=("EU",),
    )
    assert len(agent.requests) == 1
    assert ordinary["skipped"] == [{"venue": "EU", "reason": "already_prepared"}]

    refreshed = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(minutes=2),
        agent=agent,
        venues=("EU",),
        refresh_venues=("EU",),
    )

    assert refreshed["prepared"][0]["candidate_scope_id"] == scope_id
    assert len(agent.requests) == 2
    latest = UniverseRunStore(state_dir / "universe_runs").read_latest("EU")
    assert latest["refresh_reason"] == "company_brief_wave"
    assert latest["request_payload_hash"] != first["request_payload_hash"]
    assert latest["company_context_hash"] != first["company_context_hash"]
    assert latest["company_brief_refs_by_symbol"]["SAP.DE"] == company_ref


def test_exact_activated_scope_is_terminal_even_for_force(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    scope_id = _write_scope_and_brief(
        state_dir,
        venue="TW",
        symbols=["2330.TW", "2454.TW"],
        family="semis_tw",
    )
    (state_dir / "venue_state.json").write_text(
        json.dumps(
            {
                "venues": {
                    "TW": {
                        "candidate_scope_id": scope_id,
                        "last_universe_activation_scope_id": scope_id,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    agent = FakeAgent()
    posture_agent = FakePostureAgent()

    result = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=posture_agent,
        venues=("TW",),
        refresh_venues=("TW",),
        force=True,
    )

    assert result["skipped"] == [
        {
            "venue": "TW",
            "candidate_scope_id": scope_id,
            "reason": "scope_already_activated",
        }
    ]
    assert agent.requests == []
    assert result["prepared"] == []
    # Regional is terminal; the advisory posture can still bootstrap once.
    assert len(posture_agent.requests) == 1

    second = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=posture_agent,
        venues=("TW",),
        refresh_venues=("TW",),
        force=True,
    )
    assert second["skipped"] == result["skipped"]
    assert agent.requests == []
    assert len(posture_agent.requests) == 1


def test_refresh_signature_derives_from_legacy_success_record() -> None:
    refresh_signature = universe_intelligence_runtime._universe_refresh_signature(
        venue="EU",
        scope_id="scope-eu",
        brief_id="brief-eu",
    )
    legacy = {
        "status": "success",
        "venue": "EU",
        "candidate_scope_id": "scope-eu",
        "brief_ref": {"brief_id": "brief-eu"},
    }

    assert "refresh_signature" not in legacy
    assert universe_intelligence_runtime._same_refresh_success(
        legacy,
        refresh_signature,
    )


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


def test_prepared_projection_repair_uses_exponential_backoff(tmp_path) -> None:
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
    normal_store = UniverseRunStore(state_dir / "universe_runs")
    normal_store.prepared_path_for_scope(scope_id).unlink()

    class FailingPreparedStore(UniverseRunStore):
        writes = 0

        def write_prepared(self, scope_id, record):
            self.writes += 1
            raise OSError("disk full")

    store = FailingPreparedStore(state_dir / "universe_runs")
    first = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        run_store=store,
        venues=("EU",),
    )
    assert first["errors"][0]["reason"] == "prepared_write_error"
    assert store.read_latest("EU")["latest_failure"]["retry_attempt"] == 1
    assert store.read_latest("EU")["latest_failure"]["retry_delay_seconds"] == 60

    deferred = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(seconds=30),
        agent=agent,
        run_store=store,
        venues=("EU",),
    )
    assert deferred["skipped"][0]["reason"] == "failure_backoff"
    assert store.writes == 1

    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(seconds=61),
        agent=agent,
        run_store=store,
        venues=("EU",),
    )
    latest_failure = store.read_latest("EU")["latest_failure"]
    assert store.writes == 2
    assert latest_failure["retry_attempt"] == 2
    assert latest_failure["retry_delay_seconds"] == 2 * 60
    assert "latest_failure" not in latest_failure
    assert len(agent.requests) == 1


def test_failure_backoff_is_exponential_and_stable_for_one_scope_brief(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="casys-trader")
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_scope_and_brief(
        state_dir, venue="EU", symbols=["AIR.PA", "SAP.DE"], family="eu_industrials"
    )
    agent = FailingAgent()

    first = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir, state_dir=state_dir, loop_now=NOW, agent=agent, venues=("EU",)
    )
    assert first["errors"][0]["reason"] == "TimeoutError"
    latest = UniverseRunStore(state_dir / "universe_runs").read_latest("EU")
    assert latest["retry_attempt"] == 1
    assert latest["retry_delay_seconds"] == 30 * 60
    assert latest["next_retry_at"] == (NOW + timedelta(minutes=30)).isoformat()

    deferred = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(minutes=1),
        agent=agent,
        venues=("EU",),
    )
    assert len(agent.requests) == 1
    assert deferred["skipped"][0]["reason"] == "failure_backoff"
    assert deferred["skipped"][0]["attempt"] == 1

    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW + timedelta(minutes=31),
        agent=agent,
        venues=("EU",),
    )
    assert len(agent.requests) == 2
    latest = UniverseRunStore(state_dir / "universe_runs").read_latest("EU")
    assert latest["retry_attempt"] == 2
    assert latest["retry_delay_seconds"] == 60 * 60
    assert "universe retry scheduled venue=EU" in caplog.text
    assert "universe retry deferred venue=EU" in caplog.text


def test_material_signature_ignores_timestamps_but_retry_lineage_is_scope_brief_bound() -> None:
    original = {
        "as_of": "2026-07-10T20:00:00+00:00",
        "market_context": {"as_of": "2026-07-10T19:00:00+00:00", "status": "partial"},
        "candidates": [{"symbol": "AIR.PA", "attractiveness": 0.8}],
    }
    timestamp_only = {
        **original,
        "as_of": "2026-07-10T20:02:00+00:00",
        "market_context": {"as_of": "2026-07-10T19:02:00+00:00", "status": "partial"},
    }
    material_change = {
        **timestamp_only,
        "market_context": {"as_of": "2026-07-10T19:02:00+00:00", "status": "stale"},
    }

    assert universe_intelligence_runtime._material_request_signature(original) == (
        universe_intelligence_runtime._material_request_signature(timestamp_only)
    )
    assert universe_intelligence_runtime._material_request_signature(original) != (
        universe_intelligence_runtime._material_request_signature(material_change)
    )
    assert universe_intelligence_runtime._retry_lineage_key(
        venue="EU", scope_id="scope-1", brief=SimpleNamespace(brief_id="brief-1")
    ) == universe_intelligence_runtime._retry_lineage_key(
        venue="EU", scope_id="scope-1", brief=SimpleNamespace(brief_id="brief-1")
    )
    assert universe_intelligence_runtime._retry_lineage_key(
        venue="EU", scope_id="scope-1", brief=SimpleNamespace(brief_id="brief-1")
    ) != universe_intelligence_runtime._retry_lineage_key(
        venue="EU", scope_id="scope-2", brief=SimpleNamespace(brief_id="brief-1")
    )


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


def test_prepare_global_situation_digest_reads_global_brief_and_prioritises_it(tmp_path) -> None:
    """When a GLOBAL brief exists it is read and its points appear first in the digest."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    briefs = NewsMacroBriefStore(state_dir / "news_briefs")

    regional = NewsMacroBrief.from_mapping({
        "brief_id": "brief-US",
        "venue": "US",
        "as_of": "2026-07-10T20:01:00+00:00",
        "valid_until": "2026-07-11T16:01:00+00:00",
        "alerts": [{
            "point": "US regional alert.",
            "sources": ["Reuters"],
            "source_refs": ["us-ref"],
            "severity": "risk",
            "signal": "event",
        }],
    })
    assert regional is not None
    briefs.append(regional)

    global_brief = NewsMacroBrief.from_mapping({
        "brief_id": "brief-GLOBAL",
        "venue": "GLOBAL",
        "as_of": "2026-07-10T19:00:00+00:00",
        "valid_until": "2026-07-11T16:01:00+00:00",
        "alerts": [{
            "point": "Global macro cross-asset signal.",
            "sources": ["Reuters"],
            "source_refs": ["global-ref"],
            "severity": "info",
            "signal": "weak",
        }],
    })
    assert global_brief is not None
    briefs.append(global_brief)

    store = GlobalSituationDigestStore(state_dir / "global_situation_digests")
    digest, ref = universe_intelligence_runtime._prepare_global_situation_digest(
        briefs=briefs,
        store=store,
        now=NOW,
        log=logging.getLogger("test"),
    )

    assert digest
    assert ref.get("persistence_status") == "appended"
    point_texts = [p["point"] for p in digest.get("points", [])]
    assert "Global macro cross-asset signal." in point_texts
    # Global point must be ranked first despite lower severity/signal than regional
    assert digest["points"][0]["point"] == "Global macro cross-asset signal."


def test_prepare_global_situation_digest_is_fail_open_when_global_brief_absent(tmp_path) -> None:
    """No GLOBAL brief → digest is produced normally with regional points only."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    briefs = NewsMacroBriefStore(state_dir / "news_briefs")

    regional = NewsMacroBrief.from_mapping({
        "brief_id": "brief-EU",
        "venue": "EU",
        "as_of": "2026-07-10T20:01:00+00:00",
        "valid_until": "2026-07-11T16:01:00+00:00",
        "alerts": [{
            "point": "EU regional alert.",
            "sources": ["Reuters"],
            "source_refs": ["eu-ref"],
        }],
    })
    assert regional is not None
    briefs.append(regional)

    store = GlobalSituationDigestStore(state_dir / "global_situation_digests")
    digest, ref = universe_intelligence_runtime._prepare_global_situation_digest(
        briefs=briefs,
        store=store,
        now=NOW,
        log=logging.getLogger("test"),
    )

    assert digest
    assert ref.get("persistence_status") == "appended"
    point_texts = [p["point"] for p in digest.get("points", [])]
    assert "EU regional alert." in point_texts


_COOLDOWN = timedelta(hours=4)
_WINDOW = timedelta(minutes=90)


def _posture_current(as_of: str) -> dict:
    return {
        "posture_id": "global_universe_posture:v1:deadbeef",
        "as_of": as_of,
        "gross_mode": "cautious",
        "net_bias": "long",
    }


def test_decide_refresh_bootstrap_when_no_current() -> None:
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=None, preopen_now=(), cooldown=_COOLDOWN, preopen_window=_WINDOW
    ) == (True, "bootstrap")


def test_decide_refresh_skips_when_fresh_and_no_preopen() -> None:
    current = _posture_current((NOW - timedelta(minutes=10)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=current, preopen_now=(), cooldown=_COOLDOWN, preopen_window=_WINDOW
    ) == (False, "fresh")


def test_decide_refresh_on_cooldown_elapsed() -> None:
    current = _posture_current((NOW - timedelta(hours=5)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=current, preopen_now=(), cooldown=_COOLDOWN, preopen_window=_WINDOW
    ) == (True, "cooldown")


def test_decide_refresh_forced_once_per_preopen_window() -> None:
    # Posture prepared 2h ago (> 90min window) with a venue entering pre-open.
    stale = _posture_current((NOW - timedelta(hours=2)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=stale, preopen_now=("EU",), cooldown=_COOLDOWN, preopen_window=_WINDOW
    ) == (True, "preopen:EU")
    # Once refreshed inside the window, a still-recent posture is not recomputed.
    recent = _posture_current((NOW - timedelta(minutes=10)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW, current=recent, preopen_now=("EU",), cooldown=_COOLDOWN, preopen_window=_WINDOW
    ) == (False, "fresh")


def test_decide_refresh_when_matching_brief_arrives() -> None:
    current = _posture_current((NOW - timedelta(minutes=10)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW,
        current=current,
        preopen_now=("EU",),
        cooldown=_COOLDOWN,
        preopen_window=_WINDOW,
        previous_coverage={"brief_ids": {"TW": "brief-tw"}, "digest_id": "digest-old"},
        current_coverage={
            "brief_ids": {"TW": "brief-tw", "EU": "brief-eu"},
            "digest_id": "digest-new",
            "missing_brief_venues": [],
        },
    ) == (True, "coverage_changed")


def test_decide_refresh_does_not_fire_when_brief_disappears() -> None:
    current = _posture_current((NOW - timedelta(hours=5)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW,
        current=current,
        preopen_now=("EU",),
        cooldown=_COOLDOWN,
        preopen_window=_WINDOW,
        previous_coverage={"brief_ids": {"TW": "brief-tw", "EU": "brief-eu"}, "digest_id": "d1"},
        current_coverage={
            "brief_ids": {"TW": "brief-tw"},
            "digest_id": "d1",
            "missing_brief_venues": ["EU"],
        },
    ) == (False, "awaiting_brief:EU")


def test_decide_refresh_legacy_coverage_once_when_briefs_exist() -> None:
    current = _posture_current((NOW - timedelta(minutes=10)).isoformat())
    assert universe_intelligence_runtime.decide_global_posture_refresh(
        now=NOW,
        current=current,
        preopen_now=(),
        cooldown=_COOLDOWN,
        preopen_window=_WINDOW,
        previous_coverage=None,
        current_coverage={"brief_ids": {"EU": "brief-eu"}, "digest_id": "digest-1"},
    ) == (True, "coverage_changed")


def test_tick_reuses_global_posture_within_cooldown_without_recalling_agent(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_scope_and_brief(
        state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
    )
    posture_agent = FakePostureAgent()

    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir, state_dir=state_dir, loop_now=NOW,
        agent=FakeAgent(), posture_agent=posture_agent, venues=("US",),
    )
    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir, state_dir=state_dir, loop_now=NOW,
        agent=FakeAgent(), posture_agent=posture_agent, venues=("US",),
    )

    # NOW (Fri 20:05 UTC) has no venue in pre-open: the second tick reuses the
    # fresh posture instead of issuing a second LLM call.
    assert len(posture_agent.requests) == 1
    posture_rows = (
        state_dir / "global_universe_postures" / "2026-07-10.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(posture_rows) == 1


def test_tick_recalls_posture_when_missing_regional_brief_arrives(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    _write_scope_and_brief(
        state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
    )
    eu_scope_id = _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["AIR.PA", "SAP.DE"],
        family="eu_industrials",
        write_brief=False,
    )
    posture_agent = FakePostureAgent()

    first = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=FakeAgent(),
        posture_agent=posture_agent,
        venues=("US", "EU"),
    )
    assert first["waiting"] == [{"venue": "EU", "reason": "brief_missing"}]
    assert len(posture_agent.requests) == 1

    _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["AIR.PA", "SAP.DE"],
        family="eu_industrials",
        reuse_scope_id=eu_scope_id,
    )
    universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=FakeAgent(),
        posture_agent=posture_agent,
        venues=("US", "EU"),
    )

    assert len(posture_agent.requests) == 2
    current = GlobalUniversePostureStore(
        state_dir / "global_universe_postures"
    ).read_current()
    assert current is not None
    assert "EU" in (current.get("input_coverage") or {}).get("brief_ids", {})


def test_tick_refreshes_posture_after_activation_when_brief_arrives(tmp_path) -> None:
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    us_scope = _write_scope_and_brief(
        state_dir, venue="US", symbols=["AAPL", "MSFT"], family="us_mega_tech"
    )
    eu_scope = _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["AIR.PA", "SAP.DE"],
        family="eu_industrials",
        write_brief=False,
    )
    (state_dir / "venue_state.json").write_text(
        json.dumps(
            {
                "venues": {
                    "US": {
                        "candidate_scope_id": us_scope,
                        "last_universe_activation_scope_id": us_scope,
                    },
                    "EU": {
                        "candidate_scope_id": eu_scope,
                        "last_universe_activation_scope_id": eu_scope,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    posture_agent = FakePostureAgent()
    agent = FakeAgent()

    first = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=posture_agent,
        venues=("US", "EU"),
    )
    assert first["prepared"] == []
    assert len(posture_agent.requests) == 1

    _write_scope_and_brief(
        state_dir,
        venue="EU",
        symbols=["AIR.PA", "SAP.DE"],
        family="eu_industrials",
        reuse_scope_id=eu_scope,
    )
    second = universe_intelligence_runtime.tick_universe_intelligence(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=NOW,
        agent=agent,
        posture_agent=posture_agent,
        venues=("US", "EU"),
    )

    assert second["prepared"] == []
    assert agent.requests == []
    assert len(posture_agent.requests) == 2
    current = GlobalUniversePostureStore(
        state_dir / "global_universe_postures"
    ).read_current()
    assert "EU" in (current or {}).get("input_coverage", {}).get("brief_ids", {})


def test_runner_is_non_blocking_and_coalesces_latest_trigger(caplog) -> None:
    caplog.set_level(logging.INFO, logger="casys-trader")
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
    assert "universe trigger coalesced pending_replaced=False" in caplog.text
    runner.stop()


def test_runner_keeps_post_cycle_immediate_and_merges_delayed_micro_venues() -> None:
    calls = []
    two_calls = threading.Event()

    def tick_fn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            two_calls.set()
        return {"prepared": [], "waiting": [], "skipped": [], "errors": []}

    runner = universe_intelligence_runtime.UniverseIntelligenceRunner(
        tick_fn=tick_fn,
        stop_timeout_s=0.1,
    )
    first = runner.trigger(
        delay_s=0.05,
        loop_now="micro-eu",
        venues=("EU",),
        refresh_venues=("EU",),
    )
    runner.trigger(
        delay_s=0.05,
        loop_now="micro-tw",
        venues=("TW",),
        refresh_venues=("TW",),
    )
    runner.trigger(
        loop_now="post-cycle",
        venues=universe_intelligence_runtime.VENUES,
    )

    assert two_calls.wait(timeout=1.0)
    first["_thread"].join(timeout=1.0)
    assert [call["loop_now"] for call in calls] == ["post-cycle", "micro-tw"]
    assert calls[0]["venues"] == universe_intelligence_runtime.VENUES
    assert "refresh_venues" not in calls[0]
    assert calls[1]["venues"] == ("TW", "EU")
    assert calls[1]["refresh_venues"] == ("TW", "EU")
    runner.stop()


def test_runner_extends_trailing_deadline_without_waiting_for_real_debounce() -> None:
    clock = [10.0]
    runner = universe_intelligence_runtime.UniverseIntelligenceRunner(
        tick_fn=lambda **_kwargs: {
            "prepared": [],
            "waiting": [],
            "skipped": [],
            "errors": [],
        },
        stop_timeout_s=0.1,
        monotonic_fn=lambda: clock[0],
    )
    runner.trigger(
        delay_s=120.0,
        venues=("EU",),
        refresh_venues=("EU",),
    )
    first_deadline = runner._delayed_deadline
    clock[0] = 25.0
    runner.trigger(
        delay_s=120.0,
        venues=("TW",),
        refresh_venues=("TW",),
    )

    assert first_deadline == 130.0
    assert runner._delayed_deadline == 145.0
    assert runner._delayed_kwargs["venues"] == ("TW", "EU")
    assert runner._delayed_kwargs["refresh_venues"] == ("TW", "EU")
    runner.stop()


def test_company_brief_refresh_routes_only_supported_symbol_venue(tmp_path) -> None:
    calls: list[dict] = []

    class Runner:
        def trigger(self, **kwargs):
            calls.append(kwargs)
            return {"triggered": True}

    runner = Runner()
    routed = universe_intelligence_runtime.trigger_company_brief_refresh(
        {"symbol": "2330.TW"},
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
        delay_s=12.0,
    )
    fx = universe_intelligence_runtime.trigger_company_brief_refresh(
        {"symbol": "EURUSD=X"},
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
    )
    missing = universe_intelligence_runtime.trigger_company_brief_refresh(
        {},
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
    )

    assert routed == {"triggered": True}
    assert calls[0]["venues"] == ("TW",)
    assert calls[0]["refresh_venues"] == ("TW",)
    assert calls[0]["delay_s"] == 12.0
    assert fx == {"triggered": False, "reason": "unsupported_venue", "venue": "FX"}
    assert missing == {"triggered": False, "reason": "symbol_missing"}
    assert len(calls) == 1


def test_regional_brief_refresh_wakes_supported_venues(tmp_path) -> None:
    calls: list[dict] = []

    class Runner:
        def trigger(self, **kwargs):
            calls.append(kwargs)
            return {"triggered": True}

    runner = Runner()
    regional = universe_intelligence_runtime.trigger_regional_brief_refresh(
        ({"venue": "EU", "brief_ref": {"brief_id": "brief-eu"}},),
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
    )
    global_only = universe_intelligence_runtime.trigger_regional_brief_refresh(
        ({"venue": "GLOBAL", "brief_ref": {"brief_id": "brief-global"}},),
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
    )
    ignored = universe_intelligence_runtime.trigger_regional_brief_refresh(
        ({"venue": "FX"},),
        runner=runner,
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        loop_now=NOW,
    )

    assert regional == {"triggered": True}
    assert global_only == {"triggered": True}
    assert ignored == {"triggered": False, "reason": "no_supported_venue"}
    assert calls[0]["venues"] == ("EU",)
    assert calls[1]["venues"] == ("TW", "EU", "US")
    assert len(calls) == 2
