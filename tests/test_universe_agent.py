from __future__ import annotations

import json

import pytest

from trader.agent import llm
from trader.agent.universe import (
    LlmUniverseAgent,
    UniverseAgentError,
    build_universe_router_from_env,
    build_universe_prompt,
    parse_universe_completion,
)
from trader.application.universe import build_universe_composition_request
from trader.domain.universe import UniverseSituationContext


def _request():
    situation = UniverseSituationContext(
        status="active",
        brief_ref={
            "date": "2026-07-10",
            "venue": "EU",
            "brief_id": "brief-eu-1",
            "as_of": "2026-07-10T15:40:00+00:00",
        },
        metadata={
            "brief_id": "brief-eu-1",
            "venue": "EU",
            "as_of": "2026-07-10T15:40:00+00:00",
            "valid_until": "2026-07-11T11:40:00+00:00",
        },
        coverage={
            "status": "partial",
            "candidate_count": 2,
            "candidates_with_news": 1,
            "news_items_injected": 3,
            "news_cap_hit": False,
            "global_headlines": "present",
            "macro_series": "present",
        },
        zones={
            "GLOBAL": [
                {
                    "point": "Rates remain restrictive",
                    "sources": ["DBnomics"],
                    "source_refs": ["macro:rates"],
                    "symbols": [],
                    "severity": "watch",
                    "signal": "strong",
                }
            ]
        },
        families={
            "eu_tech": [
                {
                    "point": "European software demand is resilient",
                    "sources": ["Reuters"],
                    "source_refs": ["u-global"],
                    "symbols": ["SAP.DE"],
                    "severity": "info",
                    "signal": "strong",
                }
            ]
        },
        symbols={
            "SAP.DE": [
                {
                    "point": "SAP raises cloud guidance",
                    "sources": ["Reuters"],
                    "source_refs": ["u1"],
                    "symbols": ["SAP.DE"],
                    "severity": "watch",
                    "signal": "event",
                }
            ]
        },
        alerts=[],
        truncated=False,
        point_count=3,
        text_chars=95,
    )
    return build_universe_composition_request(
        venue="EU",
        as_of="2026-07-10T15:30:00+00:00",
        candidates=(
            {"symbol": "SAP.DE", "attractiveness": 0.8, "bias": "long", "candidate_source": "radar"},
            {
                "symbol": "ASML.AS",
                "attractiveness": 0.7,
                "bias": "neutral",
                "candidate_source": "fresh_news",
                "fresh_news": {"source_refs": ["u2"]},
            },
        ),
        baseline=("SAP.DE",),
        sticky=("SIE.DE",),
        market_context={"regime_families": {"eu_tech": {"dir": "up", "frac": 0.7}}},
        situation_context=situation,
        global_family_board={
            "board_id": "board-global-1",
            "status": "complete",
            "role": "comparative_context_not_capital_allocation",
            "venues": {
                "US": {
                    "families": {
                        "us_tech": {
                            "radar_rank_within_venue": 1,
                            "situation_status": "observed",
                        }
                    }
                }
            },
        },
    )


def test_prompt_says_agent_composes_full_hotlist_and_includes_all_context_layers() -> None:
    prompt = build_universe_prompt(_request())

    assert "compose toi-même la hotlist complète" in prompt
    assert "selected_hotlist" in prompt
    assert "EU" in prompt
    assert "candidate_scope:v1:EU:" in prompt
    assert "SAP.DE" in prompt and "ASML.AS" in prompt
    assert "SIE.DE" in prompt
    assert "eu_tech" in prompt
    assert "Rates remain restrictive" in prompt
    assert "European software demand is resilient" in prompt
    assert "SAP raises cloud guidance" in prompt
    assert "retrieval_status" in prompt and "not_enabled" in prompt
    assert "explique chaque symbole retenu" in prompt
    assert "global_family_board" in prompt
    assert "us_tech" in prompt
    assert "jamais comme quota" in prompt
    assert "input_refs" not in prompt
    assert '"news_items":' not in prompt


def test_parse_full_contract_and_legacy_add_remove() -> None:
    full, error = parse_universe_completion(
        json.dumps(
            {
                "selected_hotlist": ["ASML.AS"],
                "summary": "Prefer the fresh catalyst.",
                "family_postures": {"eu_tech": "constructive"},
                "symbol_rationales": {"ASML.AS": "Fresh catalyst with family support."},
            }
        ),
        baseline=("SAP.DE",),
    )
    legacy, legacy_error = parse_universe_completion(
        'prefix {"add":["ASML.AS"],"remove":["SAP.DE"]} suffix',
        baseline=("SAP.DE",),
    )

    assert error is None
    assert full is not None
    assert full.selected_hotlist == ("ASML.AS",)
    assert full.contract_version == "universe.v1"
    assert legacy_error is None
    assert legacy is not None
    assert legacy.selected_hotlist == ("ASML.AS",)
    assert legacy.contract_version == "legacy.add_remove.v1"
    assert legacy.symbol_rationales == {"ASML.AS": "Selected by legacy add/remove compatibility contract."}


def test_parse_prefixed_full_contract_keeps_outer_object_not_nested_mappings() -> None:
    decision, error = parse_universe_completion(
        "result:\n"
        + json.dumps(
            {
                "selected_hotlist": ["SAP.DE"],
                "summary": "Keep SAP.",
                "family_postures": {"eu_tech": "constructive"},
                "symbol_rationales": {"SAP.DE": "Strongest evidence."},
            }
        )
        + "\nend",
        baseline=("SAP.DE",),
    )

    assert error is None
    assert decision is not None
    assert decision.selected_hotlist == ("SAP.DE",)


def test_parse_invalid_payload_is_explicit() -> None:
    decision, error = parse_universe_completion("not json", baseline=("SAP.DE",))
    wrong_shape, shape_error = parse_universe_completion('{"selected_hotlist":"SAP.DE"}', baseline=("SAP.DE",))
    incomplete, incomplete_error = parse_universe_completion(
        '{"selected_hotlist":["SAP.DE"],"summary":"ok","symbol_rationales":{"SAP.DE":"why"}}',
        baseline=("SAP.DE",),
    )

    assert decision is None
    assert error is not None
    assert wrong_shape is None
    assert shape_error == "selected_hotlist_not_list"
    assert incomplete is None
    assert incomplete_error == "family_postures_missing"


def test_llm_universe_agent_returns_decision_and_classifies_failures() -> None:
    request = _request()

    class GoodRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            assert timeout_s == 7
            assert "compose toi-même" in prompt
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                fallback_reason="primary:quota",
                text=json.dumps(
                    {
                        "selected_hotlist": ["SAP.DE"],
                        "summary": "Keep the strongest candidate.",
                        "family_postures": {"eu_tech": "constructive"},
                        "symbol_rationales": {"SAP.DE": "Best combined evidence."},
                    }
                ),
            )

    decision = LlmUniverseAgent(GoodRouter(), timeout_s=7).compose(request)
    assert decision.selected_hotlist == ("SAP.DE",)
    assert decision.provider == "test"
    assert decision.model == "stub"
    assert decision.provider_fallback_reason == "primary:quota"

    class InvalidRouter:
        def complete(self, _prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(provider="test", model="stub", text="bad json")

    with pytest.raises(ValueError, match="invalid_json") as invalid_exc:
        LlmUniverseAgent(InvalidRouter()).compose(request)
    assert invalid_exc.value.provider == "test"
    assert invalid_exc.value.model == "stub"

    class LegacyRouter:
        def complete(self, _prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text='{"add":["ASML.AS"],"remove":["SAP.DE"]}',
            )

    with pytest.raises(ValueError, match="legacy_contract_not_allowed"):
        LlmUniverseAgent(LegacyRouter()).compose(request)

    class FailedRouter:
        def complete(self, _prompt: str, *, timeout_s: int):
            return llm.LlmFailure(
                provider="test",
                model="stub",
                code="timeout",
                message="too slow",
                retryable=True,
            )

    with pytest.raises(UniverseAgentError, match="too slow") as exc_info:
        LlmUniverseAgent(FailedRouter()).compose(request)
    assert exc_info.value.code == "timeout"


def test_universe_router_uses_a_distinct_role_profile(monkeypatch) -> None:
    captured = {}
    sentinel = object()

    def build_router(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm, "build_default_router_from_env", build_router)

    result = build_universe_router_from_env(
        env_path=None,
        acpx_bin="test-acpx",
        acpx_agent="codex",
        model="test-model",
    )

    assert result is sentinel
    assert captured == {
        "env_path": None,
        "acpx_bin": "test-acpx",
        "spark_model": "test-model",
        "acpx_provider": "universe",
        "acpx_agent": "codex",
        "acpx_session_label": "casys-trader:universe-agent",
    }
