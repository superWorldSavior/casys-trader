import json

from trader.agent import llm
from trader.agent.news_macro import LlmNewsMacroAnalyst, build_news_macro_prompt, parse_news_macro_completion
from trader.application.analyst import NewsMacroAnalysisRequest


def test_news_macro_prompt_contains_bounded_contract() -> None:
    prompt = build_news_macro_prompt(
        NewsMacroAnalysisRequest(
            as_of="2026-07-09T07:00:00+00:00",
            valid_until="2026-07-10T07:00:00+00:00",
            news_items=({"uuid": "u1", "title": "Markets fall"},),
            macro_next=({"event": "FOMC", "in_h": 12},),
            candidate_symbols=("TSM",),
        )
    )

    assert "Retourne uniquement un objet JSON valide" in prompt
    assert "Markets fall" in prompt
    assert "FOMC" in prompt
    assert "TSM" in prompt


def test_parse_news_macro_completion_uses_last_json_object() -> None:
    payload = {
        "zones": {"US": [{"point": "Liquidity tightening", "sources": ["u1"]}]},
        "alerts": [{"point": "Risk-off broadening", "severity": "watch"}],
    }

    brief, error = parse_news_macro_completion(
        "status avant JSON\n" + json.dumps(payload),
        as_of="2026-07-09T07:00:00+00:00",
        valid_until="2026-07-10T07:00:00+00:00",
    )

    assert error is None
    assert brief is not None
    assert brief.as_of == "2026-07-09T07:00:00+00:00"
    assert brief.zones[0].points[0].sources == ("u1",)


def test_llm_news_macro_analyst_returns_parsed_brief() -> None:
    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            assert "news_items" in prompt
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps(
                    {
                        "zones": {"US": [{"point": "Liquidity tightening"}]},
                    }
                ),
            )

    analyst = LlmNewsMacroAnalyst(Router(), timeout_s=1)
    brief = analyst.analyze(
        NewsMacroAnalysisRequest(
            as_of="2026-07-09T07:00:00+00:00",
            valid_until="2026-07-10T07:00:00+00:00",
        )
    )

    assert brief.zones[0].name == "US"
