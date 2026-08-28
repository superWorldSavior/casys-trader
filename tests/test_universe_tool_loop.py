from __future__ import annotations

import json

import pytest

from trader.agent import llm
from trader.agent.universe.agent import UniverseAgentPayloadError
from trader.agent.universe.tool_loop import compose_with_tool_loop
from trader.application.universe import (
    build_universe_composition_request,
    compose_universe,
    validate_universe_decision,
)
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.universe import UniverseSituationContext


def _request():
    return build_universe_composition_request(
        venue="EU",
        as_of="2026-07-10T15:30:00+00:00",
        candidates=({"symbol": "SAP.DE", "attractiveness": 0.8, "bias": "long"},),
        baseline=("SAP.DE",),
        sticky=(),
        market_context={},
        situation_context=UniverseSituationContext.not_available(candidate_count=1),
    )


def _us_request():
    return build_universe_composition_request(
        venue="US",
        as_of="2026-08-27T13:30:00+00:00",
        candidates=({"symbol": "ABT", "attractiveness": 0.8, "bias": "long"},),
        baseline=("ABT",),
        sticky=(),
        market_context={},
        situation_context=UniverseSituationContext.not_available(candidate_count=1),
    )


def _final_text() -> str:
    return json.dumps(
        {
            "selected_hotlist": ["SAP.DE"],
            "summary": "Keep SAP after reviewing the company brief.",
            "family_postures": {"eu_tech": "constructive"},
            "symbol_rationales": {"SAP.DE": "The strongest candidate."},
        }
    )


def _abt_mandate() -> dict:
    return {
        "why_selected": "Abbott remains the clearest healthcare setup.",
        "role": "core_candidate",
        "posture": "constructive",
        "directional_view": "long_bias",
        "allowed_sides": ["long"],
    }


def _v2_text(*, include_mandate: bool) -> str:
    return json.dumps(
        {
            "selected_hotlist": ["ABT"],
            "summary": "Keep Abbott as the healthcare core.",
            "family_postures": {"us_healthcare": "constructive on quality names."},
            "symbol_rationales": {"ABT": "Abbott remains the clearest healthcare setup."},
            "symbol_mandates": {"ABT": _abt_mandate()} if include_mandate else {},
        }
    )


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def read_current_many(self, symbols, *, depth="screen"):
        requested = list(symbols)
        self.calls.append(requested)
        # A real store resolves "preferred" to a concrete stored depth.
        resolved_depth = "screen" if depth == "preferred" else depth
        brief = CompanyIntelligenceBrief.from_mapping(
            {
                "symbol": "SAP.DE",
                "as_of": "2026-07-10T01:00:00+00:00",
                "input_signature": "sig-1",
                "depth": resolved_depth,
                "company_thesis": {"status": "intact", "summary": "Cloud transition remains sound."},
                "selection_view": {"posture": "supports_selection", "confidence": "medium"},
                "security_readiness": "conditional",
            }
        )
        assert brief is not None
        return {symbol: brief if symbol == "SAP.DE" else None for symbol in requested}


class SequencedRouter:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: int):
        assert timeout_s == 17
        self.prompts.append(prompt)
        return llm.LlmCompletion(provider="test", model="stub", text=self.texts.pop(0))


def test_compose_with_tool_loop_executes_tool_between_two_completions() -> None:
    router = SequencedRouter(
        [
            'prefix {"tool_calls":[{"id":"c1","tool":"get_company_briefs",'
            '"args":{"symbols":["SAP.DE"],"sections":["thesis"],"max_chars":300}}]} suffix',
            _final_text(),
        ]
    )
    store = FakeStore()

    decision = compose_with_tool_loop(_request(), router=router, intelligence_store=store, max_rounds=3, timeout_s=17)

    assert decision.selected_hotlist == ("SAP.DE",)
    assert decision.provider == "test"
    assert store.calls == [["SAP.DE"]]
    assert len(router.prompts) == 2
    assert "tool_results" in router.prompts[1]
    assert "Cloud transition remains sound" in router.prompts[1]
    # LlmRouter.complete est stateless : le tour suivant doit reprendre le
    # contexte, les candidats et le contrat final, pas seulement le résultat.
    assert '"candidates"' in router.prompts[1]
    assert "SAP.DE" in router.prompts[1]
    assert "selected_hotlist" in router.prompts[1]
    assert "get_company_briefs" in router.prompts[1]
    assert decision.tool_rounds == 1
    assert decision.tool_calls[0]["tool"] == "get_company_briefs"
    assert decision.tool_calls[0]["outcome"] == "ok"
    assert decision.tool_results[0]["tool"] == "get_company_briefs"
    assert decision.tool_results[0]["ok"] is True
    assert decision.tool_results[0]["result"]["rows"][0]["brief_ref"]["brief_id"]


def test_compose_with_tool_loop_accepts_direct_final_completion() -> None:
    router = SequencedRouter([_final_text()])
    store = FakeStore()

    decision = compose_with_tool_loop(_request(), router=router, intelligence_store=store, max_rounds=3, timeout_s=17)

    assert decision.selected_hotlist == ("SAP.DE",)
    assert store.calls == []
    assert len(router.prompts) == 1
    assert "tool_calls" in router.prompts[0]
    assert decision.tool_rounds == 0
    assert decision.tool_calls == ()
    assert decision.tool_results == ()


def test_compose_with_tool_loop_repairs_one_invalid_final_payload() -> None:
    router = SequencedRouter(['{"selected_hotlist":["SAP.DE"]', _final_text(), "SHOULD_NOT_BE_CALLED"])

    decision = compose_with_tool_loop(
        _request(), router=router, intelligence_store=FakeStore(), max_rounds=3, timeout_s=17
    )

    assert decision.selected_hotlist == ("SAP.DE",)
    assert len(router.prompts) == 2
    assert router.texts == ["SHOULD_NOT_BE_CALLED"]
    assert "Correction bornée de la sortie précédente" in router.prompts[1]
    assert '"parse_error":"invalid_json"' in router.prompts[1]
    assert '"validation_errors"' not in router.prompts[1]
    assert "Aucun nouvel outil n'est accepté" in router.prompts[1]
    assert "un seul contrat JSON final complet" in router.prompts[1]


def test_compose_with_tool_loop_forces_final_turn_after_budget_is_exhausted() -> None:
    tool_call = json.dumps(
        {
            "tool_calls": [
                {
                    "id": "c1",
                    "tool": "get_company_briefs",
                    "args": {"symbols": ["SAP.DE"], "sections": ["risks"], "max_chars": 300},
                }
            ]
        }
    )
    router = SequencedRouter([tool_call, tool_call, _final_text()])
    store = FakeStore()

    decision = compose_with_tool_loop(_request(), router=router, intelligence_store=store, max_rounds=2, timeout_s=17)

    assert decision.selected_hotlist == ("SAP.DE",)
    assert len(router.prompts) == 3
    assert "hotlist finale" in router.prompts[-1]
    assert "tool_results" in router.prompts[-1]
    assert "Aucun nouvel appel d'outil n'est accepté" in router.prompts[-1]
    assert "selected_hotlist" in router.prompts[-1]


def test_compose_with_tool_loop_rejects_legacy_contract() -> None:
    router = SequencedRouter(['{"add":[],"remove":[]}'])

    with pytest.raises(UniverseAgentPayloadError, match="legacy_contract_not_allowed"):
        compose_with_tool_loop(_request(), router=router, intelligence_store=FakeStore(), timeout_s=17)


def test_compose_with_tool_loop_repairs_semantic_invalid_v2_then_succeeds() -> None:
    request = _us_request()
    router = SequencedRouter([_v2_text(include_mandate=False), _v2_text(include_mandate=True), "SHOULD_NOT_BE_CALLED"])

    decision = compose_with_tool_loop(
        request, router=router, intelligence_store=FakeStore(), max_rounds=3, timeout_s=17
    )

    assert decision.selected_hotlist == ("ABT",)
    assert decision.contract_version == "universe.v2"
    assert "ABT" in decision.symbol_mandates
    assert len(router.prompts) == 2
    assert router.texts == ["SHOULD_NOT_BE_CALLED"]
    assert '"parse_error":"semantic_contract_invalid"' in router.prompts[1]
    assert "missing_symbol_mandate:ABT" in router.prompts[1]
    assert "un seul contrat JSON final complet" in router.prompts[1]
    assert validate_universe_decision(request, decision) == ()


def test_compose_with_tool_loop_returns_semantic_invalid_repair_to_application() -> None:
    request = _us_request()
    router = SequencedRouter(
        [
            _v2_text(include_mandate=False),
            _v2_text(include_mandate=False),
            _v2_text(include_mandate=True),
        ]
    )

    class Agent:
        def compose(self, received):
            return compose_with_tool_loop(
                received,
                router=router,
                intelligence_store=FakeStore(),
                max_rounds=3,
                timeout_s=17,
            )

    result = compose_universe(request, agent=Agent())

    assert result.status == "invalid"
    assert "missing_symbol_mandate:ABT" in result.validation_errors
    assert result.decision is None
    assert result.fallback_used is False
    assert len(router.prompts) == 2
    assert router.texts == [_v2_text(include_mandate=True)]
    assert "missing_symbol_mandate:ABT" in router.prompts[1]


def test_compose_with_tool_loop_accepts_valid_v2_without_repair() -> None:
    request = _us_request()
    router = SequencedRouter([_v2_text(include_mandate=True), "SHOULD_NOT_BE_CALLED"])

    decision = compose_with_tool_loop(
        request, router=router, intelligence_store=FakeStore(), max_rounds=3, timeout_s=17
    )

    assert decision.selected_hotlist == ("ABT",)
    assert decision.symbol_mandates["ABT"]["why_selected"]
    assert len(router.prompts) == 1
    assert router.texts == ["SHOULD_NOT_BE_CALLED"]
    assert "Correction bornée de la sortie précédente" not in router.prompts[0]
    assert validate_universe_decision(request, decision) == ()
