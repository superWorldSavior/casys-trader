from trader.codex_client import DEFAULT_MODEL, ContextResearchRequest, parse_decision, parse_decision_or_context_request, decide
from trader.llm import LlmCompletion


def test_default_model_utilise_reasoning_medium() -> None:
    assert DEFAULT_MODEL == "gpt-5.3-codex-spark[medium]"


def test_parse_decision_accepte_next_wake_in_minutes_optionnel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range","next_wake_in_minutes":45}',
        "SPY",
    )

    assert decision.next_wake_in_minutes == 45.0


def test_parse_decision_accepte_intent_et_exit_plan() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.8,'
        '"rationale":"breakout","intent":"OPEN_LONG",'
        '"exit_plan":{"hard_stop":{"type":"price","price":95},'
        '"take_profits":[{"name":"tp1","price":105,"fraction":0.5}]}}',
        "SPY",
    )

    assert decision.intent == "OPEN_LONG"
    assert decision.exit_plan["hard_stop"]["price"] == 95


def test_parse_decision_accepte_indicator_watch_multi_timeframe() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.7,'
        '"rationale":"attente breakout",'
        '"indicator_watch":{"ttl_minutes":90,"logic":"all","on_trigger":"ORDER",'
        '"conditions":[{"symbol":"SPY","indicator":"z_score","op":">=",'
        '"value":1.8,"interval":"15m","window":32},'
        '{"symbol":"QQQ","indicator":"return","op":">","value":0.01,'
        '"interval":"1h","window":24}],'
        '"order":{"action":"BUY","quantity":10,"intent":"OPEN_LONG"}}}',
        "SPY",
    )

    assert decision.indicator_watch["ttl_minutes"] == 90
    assert decision.indicator_watch["conditions"][0]["interval"] == "15m"
    assert decision.indicator_watch["order"]["action"] == "BUY"


def test_parse_decision_accepte_un_learning_optionnel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.6,'
        '"rationale":"range serre",'
        '"learning":"le range tient depuis 3 reveils, j attends une cassure nette"}',
        "SPY",
    )

    assert decision.learning == "le range tient depuis 3 reveils, j attends une cassure nette"


def test_parse_decision_ignore_un_learning_non_textuel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,'
        '"rationale":"r","learning":{"a":1}}',
        "SPY",
    )

    assert decision.learning is None


def test_parse_decision_borne_la_longueur_du_learning() -> None:
    huge = "x" * 5000
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,'
        f'"rationale":"r","learning":"{huge}"}}',
        "SPY",
    )

    assert decision.learning is not None
    assert len(decision.learning) <= 1000


def test_parse_decision_learning_absent_vaut_none() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range"}',
        "SPY",
    )

    assert decision.learning is None


def test_parse_decision_garde_compatibilite_sans_next_wake() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range"}',
        "SPY",
    )

    assert decision.next_wake_in_minutes is None


def test_parse_decision_or_context_request_accepte_une_requete_indicateurs() -> None:
    response = parse_decision_or_context_request(
        '{"symbol":"SPY","action":"REQUEST_CONTEXT","rationale":"besoin de confirmer",'
        '"requests":[{"symbol":"QQQ","indicators":["z_score","spread_zscore"],'
        '"timeframe":"4h","lookback":"1mo","window":24}]}',
        "SPY",
    )

    assert isinstance(response, ContextResearchRequest)
    assert response.requests[0].symbol == "QQQ"
    assert response.requests[0].indicators == ["z_score", "spread_zscore"]
    assert response.requests[0].timeframe == "4h"
    assert response.requests[0].lookback == "1mo"
    assert response.requests[0].as_of == "latest"
    assert response.requests[0].window == 24


def test_decide_attache_les_metadonnees_llm() -> None:
    class StubRouter:
        def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
            return LlmCompletion(
                provider="ollama-cloud",
                model="nemotron-3-nano:30b-cloud",
                fallback_reason="spark:rate_limited",
                text=(
                    '{"symbol":"SPY","action":"HOLD","quantity":0,'
                    '"confidence":0.9,"rationale":"flat"}'
                ),
            )

    decision = decide(
        symbol="SPY",
        mandate="# Mandat",
        memory="# Memoire",
        context={},
        llm_router=StubRouter(),
    )

    assert decision.llm_provider == "ollama-cloud"
    assert decision.llm_model == "nemotron-3-nano:30b-cloud"
    assert decision.llm_fallback_reason == "spark:rate_limited"
