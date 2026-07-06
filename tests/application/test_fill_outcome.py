from __future__ import annotations

from trader.application.fill_outcome import build_fill_accounting, llm_exit_reason_for_intent
from trader.execution.contracts import Fill


def test_llm_exit_reason_tags_only_llm_driven_exits() -> None:
    assert llm_exit_reason_for_intent("CLOSE") == "llm_exit"
    assert llm_exit_reason_for_intent("REDUCE") == "llm_exit"
    assert llm_exit_reason_for_intent("FLIP") == "llm_exit"
    assert llm_exit_reason_for_intent("OPEN_LONG") is None
    assert llm_exit_reason_for_intent("SCALE_IN") is None
    assert llm_exit_reason_for_intent("HOLD") is None


def test_build_fill_accounting_returns_model_performance_and_entry_updates() -> None:
    fill = Fill(
        symbol="SPY",
        side="BUY",
        quantity=2.0,
        price=100.0,
        ts="2026-07-05T08:00:00+00:00",
        commission=0.35,
        commission_currency="USD",
        commission_model="ibkr_us_stock_tiered",
        fx_rate=1.0,
    )

    accounting = build_fill_accounting(
        fill=fill,
        symbol="SPY",
        action="BUY",
        intent="OPEN_LONG",
        quantity=2.0,
        price=100.0,
        confidence=0.91,
        llm_provider="acpx",
        llm_model="gpt-5.5/medium",
        llm_fallback_reason=None,
        equity=100_199.30,
        cash=99_799.65,
        position_quantity=2.0,
    )

    assert accounting.model_performance == {
        "ts": "2026-07-05T08:00:00+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "quantity": 2.0,
        "price": 100.0,
        "commission": 0.35,
        "commission_currency": "USD",
        "commission_model": "ibkr_us_stock_tiered",
        "fx_rate": 1.0,
        "confidence": 0.91,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5/medium",
        "llm_fallback_reason": None,
        "equity": 100_199.30,
        "cash": 99_799.65,
        "position_quantity": 2.0,
    }
    assert accounting.entry_updates == {
        "model_performance_logged": True,
        "commission": 0.35,
        "commission_currency": "USD",
        "commission_model": "ibkr_us_stock_tiered",
        "fx_rate": 1.0,
    }


def test_build_fill_accounting_tags_exit_reason_for_close() -> None:
    fill = Fill(
        symbol="SPY",
        side="SELL",
        quantity=2.0,
        price=101.0,
        ts="2026-07-05T08:05:00+00:00",
    )

    accounting = build_fill_accounting(
        fill=fill,
        symbol="SPY",
        action="SELL",
        intent="CLOSE",
        quantity=2.0,
        price=101.0,
        confidence=0.72,
        llm_provider=None,
        llm_model=None,
        llm_fallback_reason="fallback",
        equity=100_202.0,
        cash=100_202.0,
        position_quantity=0.0,
    )

    payload = accounting.model_performance
    assert payload["exit_reason"] == "llm_exit"
    assert payload["llm_provider"] == "unknown"
    assert payload["llm_model"] == "unknown"
    assert payload["llm_fallback_reason"] == "fallback"
