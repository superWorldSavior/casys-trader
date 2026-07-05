from trader.agent.client import Decision
from trader.market.market_data import Bar
from trader.execution.broker import IbkrCommissionModel

from backtest.engine import run_backtest


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close - 1.0, high=close + 1.0, low=close - 2.0, close=close, volume=100.0)


class FakeHistory:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]):
        self._bars_by_symbol = bars_by_symbol

    def price_asof(self, symbol: str, ts: str) -> float | None:
        bars = [bar for bar in self._bars_by_symbol.get(symbol, []) if bar.ts <= ts]
        if not bars:
            return None
        return bars[-1].close

    def bars_asof(self, symbol: str, ts: str, lookback: int) -> list[Bar]:
        bars = [bar for bar in self._bars_by_symbol.get(symbol, []) if bar.ts <= ts]
        return bars[-lookback:]


def _risk_limits(max_order_value: float = 10_000.0) -> dict:
    return {
        "max_position_value": 10_000.0,
        "max_gross_exposure": 10_000.0,
        "max_order_value": max_order_value,
        "min_equity": 0.0,
    }


def test_run_backtest_execute_les_decisions_et_trace_l_equity_curve() -> None:
    timeline = [
        "2026-01-01T10:00:00",
        "2026-01-01T11:00:00",
        "2026-01-01T12:00:00",
    ]
    bars = [_bar(timeline[0], 100.0), _bar(timeline[1], 110.0), _bar(timeline[2], 105.0)]
    history = FakeHistory({"AAPL": bars})
    contexts: list[dict] = []

    def decision_fn(symbol: str, context: dict) -> Decision:
        contexts.append(context)
        if context["now"] == timeline[0]:
            return Decision(symbol=symbol, action="BUY", confidence=0.9, quantity=2.0, rationale="entrée scriptée")
        return Decision.hold(symbol, "attente")

    result = run_backtest(
        history=history,
        timeline=timeline,
        symbols=["AAPL"],
        decision_fn=decision_fn,
        risk_limits=_risk_limits(),
        starting_cash=1_000.0,
        lookback=2,
    )

    assert result.starting_equity == 1_000.0
    assert result.trades == [
        {"ts": timeline[0], "symbol": "AAPL", "side": "BUY", "quantity": 2.0, "price": 100.0}
    ]
    assert result.equity_curve == [
        (timeline[0], 1_000.0),
        (timeline[1], 1_020.0),
        (timeline[2], 1_010.0),
    ]
    assert result.final_equity == 1_010.0
    assert [ts for ts, _equity in result.equity_curve] == timeline
    assert contexts[0]["bars"] == [
        {"ts": timeline[0], "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 100.0}
    ]
    assert contexts[0]["portfolio"] == {"positions": {}, "cash": 1_000.0, "equity": 1_000.0}


def test_run_backtest_peut_mesurer_le_pnl_net_de_commissions() -> None:
    timeline = [
        "2026-01-01T10:00:00",
        "2026-01-01T11:00:00",
    ]
    history = FakeHistory({"USO": [_bar(timeline[0], 100.0), _bar(timeline[1], 110.0)]})

    def decision_fn(symbol: str, context: dict) -> Decision:
        if context["portfolio"]["positions"]:
            return Decision.hold(symbol, "déjà en position")
        return Decision(symbol=symbol, action="BUY", confidence=0.9, quantity=2.0, rationale="entrée scriptée")

    result = run_backtest(
        history=history,
        timeline=timeline,
        symbols=["USO"],
        decision_fn=decision_fn,
        risk_limits=_risk_limits(),
        starting_cash=1_000.0,
        lookback=2,
        commission_model=IbkrCommissionModel(),
    )

    assert result.trades == [
        {
            "ts": timeline[0],
            "symbol": "USO",
            "side": "BUY",
            "quantity": 2.0,
            "price": 100.0,
            "commission": 0.35,
            "commission_currency": "USD",
            "commission_model": "ibkr_us_stock_tiered",
        }
    ]
    assert result.equity_curve == [
        (timeline[0], 999.65),
        (timeline[1], 1_019.65),
    ]
    assert result.final_equity == 1_019.65


def test_run_backtest_ignore_un_ordre_refuse_par_le_fusible() -> None:
    timeline = ["2026-01-01T10:00:00", "2026-01-01T11:00:00"]
    history = FakeHistory({"AAPL": [_bar(timeline[0], 100.0), _bar(timeline[1], 120.0)]})

    def decision_fn(symbol: str, context: dict) -> Decision:
        return Decision(symbol=symbol, action="BUY", confidence=1.0, quantity=2.0, rationale="trop gros")

    result = run_backtest(
        history=history,
        timeline=timeline,
        symbols=["AAPL"],
        decision_fn=decision_fn,
        risk_limits=_risk_limits(max_order_value=50.0),
        starting_cash=1_000.0,
    )

    assert result.trades == []
    assert result.equity_curve == [(timeline[0], 1_000.0), (timeline[1], 1_000.0)]
    assert result.final_equity == 1_000.0


def test_run_backtest_autorise_sortie_qui_reduit_risque_meme_si_order_value_depasse() -> None:
    timeline = ["2026-01-01T10:00:00", "2026-01-01T11:00:00"]
    history = FakeHistory({"AAPL": [_bar(timeline[0], 100.0), _bar(timeline[1], 120.0)]})

    def decision_fn(symbol: str, context: dict) -> Decision:
        if context["now"] == timeline[0]:
            return Decision(symbol=symbol, action="BUY", confidence=1.0, quantity=100.0, rationale="open")
        return Decision(symbol=symbol, action="SELL", confidence=1.0, quantity=100.0, rationale="close")

    result = run_backtest(
        history=history,
        timeline=timeline,
        symbols=["AAPL"],
        decision_fn=decision_fn,
        risk_limits={
            "max_position_value": 20_000.0,
            "max_gross_exposure": 20_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 0.0,
        },
        starting_cash=20_000.0,
    )

    assert result.trades == [
        {"ts": timeline[0], "symbol": "AAPL", "side": "BUY", "quantity": 100.0, "price": 100.0},
        {"ts": timeline[1], "symbol": "AAPL", "side": "SELL", "quantity": 100.0, "price": 120.0},
    ]
    assert result.final_equity == 22_000.0
