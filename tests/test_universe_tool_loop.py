from __future__ import annotations

import json

import pytest

from trader.agent import llm
from trader.agent.universe.agent import UniverseAgentPayloadError
from trader.agent.universe.tool_loop import compose_with_tool_loop
from trader.application.universe import build_universe_composition_request
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


def _final_text() -> str:
    return json.dumps(
        {
            "selected_hotlist": ["SAP.DE"],
            "summary": "Keep SAP after reviewing the company brief.",
            "family_postures": {"diversified": "constructive"},
            "symbol_rationales": {"SAP.DE": "The strongest candidate."},
        }
    )


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def read_current_many(self, symbols, *, depth="screen"):
        requested = list(symbols)
        self.calls.append(requested)
        brief = CompanyIntelligenceBrief.from_mapping(
            {
                "symbol": "SAP.DE",
                "as_of": "2026-07-10T01:00:00+00:00",
                "input_signature": "sig-1",
                "depth": depth,
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

    decision = compose_with_tool_loop(
        _request(), router=router, intelligence_store=store, max_rounds=3, timeout_s=17
    )

    assert decision.selected_hotlist == ("SAP.DE",)
    assert decision.provider == "test"
    assert store.calls == [["SAP.DE"]]
    assert len(router.prompts) == 2
    assert "tool_results" in router.prompts[1]
    assert "Cloud transition remains sound" in router.prompts[1]


def test_compose_with_tool_loop_accepts_direct_final_completion() -> None:
    router = SequencedRouter([_final_text()])
    store = FakeStore()

    decision = compose_with_tool_loop(
        _request(), router=router, intelligence_store=store, max_rounds=3, timeout_s=17
    )

    assert decision.selected_hotlist == ("SAP.DE",)
    assert store.calls == []
    assert len(router.prompts) == 1
    assert "tool_calls" in router.prompts[0]


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

    decision = compose_with_tool_loop(
        _request(), router=router, intelligence_store=store, max_rounds=2, timeout_s=17
    )

    assert decision.selected_hotlist == ("SAP.DE",)
    assert len(router.prompts) == 3
    assert "hotlist finale" in router.prompts[-1]
    assert "tool_results" in router.prompts[-1]


def test_compose_with_tool_loop_rejects_legacy_contract() -> None:
    router = SequencedRouter(['{"add":[],"remove":[]}'])

    with pytest.raises(UniverseAgentPayloadError, match="legacy_contract_not_allowed"):
        compose_with_tool_loop(
            _request(), router=router, intelligence_store=FakeStore(), timeout_s=17
        )
