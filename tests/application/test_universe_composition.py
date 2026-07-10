from __future__ import annotations

import pytest

from trader.application.universe import (
    UniverseAgentDecision,
    build_universe_composition_request,
    compose_universe,
)
from trader.domain.universe import UniverseSituationContext


def _request(*, baseline=("SAP.DE",), sticky=("SIE.DE",)):
    return build_universe_composition_request(
        venue="EU",
        as_of="2026-07-10T15:30:00+00:00",
        candidates=(
            {
                "symbol": "SAP.DE",
                "attractiveness": 0.8,
                "bias": "long",
                "candidate_source": "radar",
            },
            {
                "symbol": "ASML.AS",
                "attractiveness": 0.7,
                "bias": "neutral",
                "candidate_source": "fresh_news",
                "fresh_news": {"source_refs": ["u1"]},
            },
        ),
        baseline=baseline,
        sticky=sticky,
        market_context={"regime_families": {"eu_tech": {"dir": "up", "frac": 0.7}}},
        situation_context=UniverseSituationContext.not_available(candidate_count=2),
        global_family_board={
            "board_id": "global-family-board-1",
            "role": "comparative_context_not_capital_allocation",
            "status": "partial",
        },
    )


def test_request_enriches_candidates_builds_snapshot_and_disables_retrieval() -> None:
    request = _request()

    assert request.venue == "EU"
    assert request.candidate_scope_id.startswith("candidate_scope:v1:EU:")
    assert [item["family"] for item in request.candidates] == ["eu_tech", "eu_tech"]
    assert request.baseline == ("SAP.DE",)
    assert request.sticky == ("SIE.DE",)
    assert request.family_snapshot["eu_tech"]["candidate_count"] == 2
    assert request.family_snapshot["eu_tech"]["situation_status"] == "not_reported"
    assert request.family_snapshot["eu_tech"]["regime_status"] == "observed"
    assert request.family_snapshot["eu_tech"]["regime"] == {
        "dir": "up",
        "frac": 0.7,
    }
    assert request.family_snapshot["eu_industrials"]["sticky_symbols"] == ["SIE.DE"]
    assert request.family_snapshot["eu_industrials"]["regime_status"] == "missing"
    assert request.retrieval_refs == ()
    assert request.retrieval_status == "not_enabled"
    assert request.global_family_board["board_id"] == "global-family-board-1"
    assert request.to_dict()["global_family_board"]["role"] == (
        "comparative_context_not_capital_allocation"
    )


def test_request_rejects_baseline_over_25_or_outside_pool_or_sticky() -> None:
    candidates = tuple(
        {"symbol": f"SYM{i}", "attractiveness": 1.0, "bias": "long"}
        for i in range(26)
    )
    with pytest.raises(ValueError, match="baseline_cap_exceeded"):
        build_universe_composition_request(
            venue="US",
            as_of="2026-07-10T20:00:00+00:00",
            candidates=candidates,
            baseline=tuple(item["symbol"] for item in candidates),
            sticky=(),
            market_context={},
            situation_context=UniverseSituationContext.not_available(candidate_count=26),
        )
    with pytest.raises(ValueError, match="baseline_outside_pool"):
        _request(baseline=("AIR.PA",))
    with pytest.raises(ValueError, match="baseline_contains_sticky"):
        _request(baseline=("SAP.DE",), sticky=("SAP.DE",))


def test_compose_universe_returns_success_only_for_explicit_valid_selection() -> None:
    request = _request()

    class Agent:
        def compose(self, received):
            assert received is request
            return UniverseAgentDecision(
                selected_hotlist=("ASML.AS",),
                summary="Prefer the fresh catalyst with supportive family regime.",
                family_postures={"eu_tech": "constructive"},
                symbol_rationales={"ASML.AS": "Fresh event and positive family context."},
                contract_version="universe.v1",
            )

    result = compose_universe(request, agent=Agent())

    assert result.status == "success"
    assert result.decision is not None
    assert result.decision.selected_hotlist == ("ASML.AS",)
    assert result.validation_errors == ()
    assert result.error_code is None
    assert result.fallback_used is False


@pytest.mark.parametrize(
    ("decision", "expected_error"),
    [
        (
            UniverseAgentDecision((), "Empty", {}, {}, "universe.v1"),
            "selected_hotlist_empty",
        ),
        (
            UniverseAgentDecision(("AIR.PA",), "Outside", {}, {"AIR.PA": "No"}, "universe.v1"),
            "selection_outside_pool:AIR.PA",
        ),
        (
            UniverseAgentDecision(("SIE.DE",), "Sticky", {}, {"SIE.DE": "No"}, "universe.v1"),
            "selection_contains_sticky:SIE.DE",
        ),
        (
            UniverseAgentDecision(("SAP.DE",), "No rationale", {}, {}, "universe.v1"),
            "missing_symbol_rationale:SAP.DE",
        ),
    ],
)
def test_compose_universe_returns_invalid_without_fallback(decision, expected_error) -> None:
    class Agent:
        def compose(self, _request):
            return decision

    result = compose_universe(_request(), agent=Agent())

    assert result.status == "invalid"
    assert expected_error in result.validation_errors
    assert result.decision is None
    assert result.fallback_used is False


def test_compose_universe_requires_a_posture_for_each_selected_family() -> None:
    class Agent:
        def compose(self, _request):
            return UniverseAgentDecision(
                selected_hotlist=("SAP.DE",),
                summary="Select SAP.",
                family_postures={},
                symbol_rationales={"SAP.DE": "Strongest candidate."},
                contract_version="universe.v1",
            )

    result = compose_universe(_request(), agent=Agent())

    assert result.status == "invalid"
    assert "missing_family_posture:eu_tech" in result.validation_errors


def test_compose_universe_distinguishes_invalid_agent_payload_from_transport_error() -> None:
    class InvalidAgent:
        def compose(self, _request):
            raise ValueError("invalid_json")

    class BrokenAgent:
        def compose(self, _request):
            raise TimeoutError("agent timeout")

    class WrongTypeAgent:
        def compose(self, _request):
            return {"selected_hotlist": ["SAP.DE"]}

    invalid = compose_universe(_request(), agent=InvalidAgent())
    error = compose_universe(_request(), agent=BrokenAgent())
    wrong_type = compose_universe(_request(), agent=WrongTypeAgent())

    assert invalid.status == "invalid"
    assert invalid.error_code == "invalid_agent_response"
    assert invalid.error_message == "invalid_json"
    assert invalid.fallback_used is False
    assert error.status == "error"
    assert error.error_code == "TimeoutError"
    assert error.error_message == "agent timeout"
    assert error.fallback_used is False
    assert wrong_type.status == "invalid"
    assert wrong_type.validation_errors == ("invalid_agent_decision_type",)
    assert wrong_type.fallback_used is False
