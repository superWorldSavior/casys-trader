"""Tests du helper batch _batch_decide (budget d'appels modèle honoré)."""

from trader import daemon
from trader.codex_client import ContextResearchRequest, Decision, IndicatorRequest

_COMMON = dict(
    mandate="",
    memory="",
    shared_context={},
    triggers_by_symbol={},
    tradable_bars_by_symbol={},
    tradable_symbols=["SPY", "QQQ"],
    runtime_interval="15m",
    runtime_lookback="5d",
    max_context_requests_per_symbol=2,
    max_indicators_per_request=4,
)


def test_budget_zero_ne_fait_aucun_appel_et_tout_hold(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_batch(**kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=0, **_COMMON)

    assert calls["n"] == 0
    assert n == 0
    assert decisions["SPY"].action == "HOLD"
    assert decisions["SPY"].rationale == "model_call_budget_exhausted"
    assert decisions["QQQ"].action == "HOLD"


def test_budget_un_fait_un_seul_batch_et_request_context_devient_hold(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_batch(*, symbols, allow_context_request, **kwargs):
        calls["n"] += 1
        return {
            "SPY": ContextResearchRequest(
                symbol="SPY",
                rationale="besoin z",
                requests=[IndicatorRequest(symbol="SPY", indicators=["z_score"])],
            ),
            "QQQ": Decision.hold("QQQ", "range"),
        }

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=1, **_COMMON)

    assert calls["n"] == 1  # pas de 2e batch
    assert n == 1
    assert decisions["SPY"].action == "HOLD"
    assert "budget" in decisions["SPY"].rationale
    assert decisions["QQQ"].action == "HOLD"
