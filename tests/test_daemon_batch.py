"""Tests du helper batch _batch_decide (budget d'appels modèle honoré)."""

from datetime import datetime, timezone

from trader import daemon
from trader.codex_client import ContextResearchRequest, Decision, IndicatorRequest
from trader.tools.market import Bar
from trader.tools.scheduler import Scheduler

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


def _write_runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text("starting_cash: 100000\nsymbols:\n  - SPY\n")
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 20000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


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


def test_batch_decide_passe_le_plafond_decisionnel_900s_par_defaut(monkeypatch) -> None:
    timeouts: list[int] = []

    def fake_batch(*, symbols, timeout_s, **kwargs):
        timeouts.append(timeout_s)
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=1, **_COMMON)

    assert n == 1
    assert decisions["SPY"].action == "HOLD"
    assert timeouts == [900]


def test_batch_decide_passe_le_plafond_custom_aux_deux_appels(monkeypatch) -> None:
    timeouts: list[tuple[bool, int]] = []

    def fake_batch(*, symbols, allow_context_request, timeout_s, **kwargs):
        timeouts.append((allow_context_request, timeout_s))
        if allow_context_request:
            return {
                "SPY": ContextResearchRequest(
                    symbol="SPY",
                    rationale="besoin z",
                    requests=[IndicatorRequest(symbol="SPY", indicators=["z_score"])],
                )
            }
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=2,
        decision_timeout_s=333,
        **_COMMON,
    )

    assert n == 2
    assert decisions["SPY"].action == "HOLD"
    assert timeouts == [(True, 333), (False, 333)]


def test_batch_decide_attache_context_request_apres_round_trip(monkeypatch) -> None:
    request = IndicatorRequest(symbol="SPY", indicators=["z_score"], timeframe="1h")
    calls: list[bool] = []

    def fake_batch(*, symbols, allow_context_request, **kwargs):
        calls.append(allow_context_request)
        if allow_context_request:
            return {
                "SPY": ContextResearchRequest(
                    symbol="SPY",
                    rationale="besoin z",
                    requests=[request],
                ),
                "QQQ": Decision.hold("QQQ", "range"),
            }
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    def fake_resolve_indicator_requests(requests, *args, **kwargs):
        assert list(requests) == [request]
        return {"requests": [{"symbol": "SPY", "indicators": {"z_score": 1.2}}]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    monkeypatch.setattr(daemon, "resolve_indicator_requests", fake_resolve_indicator_requests)

    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=2, **_COMMON)

    assert n == 2
    assert calls == [True, False]
    assert decisions["SPY"].context_request == {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 1,
    }
    assert decisions["QQQ"].context_request is None


def test_run_cycle_passe_le_plafond_decisionnel_a_batch_decide(monkeypatch, tmp_path, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, int] = {}

    def fake_batch_decide(**kwargs):
        captured["decision_timeout_s"] = kwargs["decision_timeout_s"]
        return {sym: Decision.hold(sym, "attente") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        decision_timeout_s=444,
    )

    assert captured["decision_timeout_s"] == 444


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
    assert decisions["SPY"].context_request == {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 0,
    }
    assert decisions["QQQ"].action == "HOLD"
