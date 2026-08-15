from datetime import datetime, timezone

from trader.application.record.decision_ledger_rows import collect_session_by_symbol
from trader.domain.market.sessions import session_snapshot
from trader.reporting.ledger import decision_ledger


def test_decision_row_expose_source_modele_et_ordre_arme() -> None:
    report = {
        "ts": "2026-06-17T01:20:27+00:00",
        "dry_run": False,
        "symbols_due": ["2492.TW"],
        "model_calls_used": 1,
        "prices": {"2492.TW": 487.5},
        "stale_market_data": {},
        "portfolio": {"cash": 100000.0, "equity": 100000.0},
    }
    decision = {
        "symbol": "2492.TW",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.72,
        "rationale": "arme une continuation apres digestion",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "indicator_watch": {
            "id": "2492.TW:test",
            "on_trigger": "EXECUTE_ORDER",
            "conditions": [],
            "order": {
                "intent": "OPEN_LONG",
                "action": "BUY",
                "qty": 20.0,
                "confidence": 0.72,
                "exit_plan": {"hard_stop": {"type": "price", "price": 450.0}},
            },
        },
    }

    row = decision_ledger.build_decision_row(report, decision, sequence=0)

    assert row["decision_source"] == "llm"
    assert row["model_called"] is True
    assert row["runtime"]["indicator_watch_order"] == decision["indicator_watch"]["order"]
    assert row["market_snapshot"]["since_open_m"] is None
    assert row["market_snapshot"]["to_close_m"] is None
    assert row["market_snapshot"]["venue"] is None


def _armed_plan_decision(symbol: str = "SPY") -> dict:
    return {
        "symbol": symbol,
        "action": "BUY",
        "qty": 10.0,
        "confidence": 0.7,
        "rationale": "armed_plan:w1 — breakout",
        "intent": "OPEN_LONG",
        "executed": True,
        "reason": "ok",
        "decision_source": "armed_plan",
        "armed_plan_id": "w1",
        "armed_plan_order": {"intent": "OPEN_LONG", "action": "BUY", "qty": 10.0},
    }


def test_armed_plan_row_records_numeric_since_open_during_us_session() -> None:
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    expected = session_snapshot("SPY", now=now)
    assert expected["open"] is True
    assert isinstance(expected["since_open_m"], int)

    report = {
        "ts": now.isoformat(),
        "dry_run": False,
        "symbols_due": ["SPY"],
        "model_calls_used": 0,
        "prices": {"SPY": 532.12},
        "stale_market_data": {},
        "portfolio": {"cash": 100000.0, "equity": 100000.0},
        "session_by_symbol": collect_session_by_symbol(
            ["SPY"],
            snapshot=lambda symbol: session_snapshot(symbol, now=now),
        ),
    }

    row = decision_ledger.build_decision_row(
        report,
        _armed_plan_decision(),
        sequence=0,
        source="armed_plan",
    )

    assert row["source"] == "armed_plan"
    assert row["market_snapshot"]["since_open_m"] == expected["since_open_m"]
    assert row["market_snapshot"]["to_close_m"] == expected["to_close_m"]
    assert row["market_snapshot"]["since_open_m"] == 60


def test_decision_row_records_none_since_open_when_session_closed() -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    expected = session_snapshot("SPY", now=now)
    assert expected["open"] is False
    assert expected["since_open_m"] is None

    report = {
        "ts": now.isoformat(),
        "dry_run": False,
        "symbols_due": ["SPY"],
        "model_calls_used": 0,
        "prices": {"SPY": 532.12},
        "stale_market_data": {},
        "portfolio": {"cash": 100000.0, "equity": 100000.0},
        "session_by_symbol": collect_session_by_symbol(
            ["SPY"],
            snapshot=lambda symbol: session_snapshot(symbol, now=now),
        ),
    }

    row = decision_ledger.build_decision_row(report, _armed_plan_decision(), sequence=0)

    assert row["market_snapshot"]["since_open_m"] is None
    assert row["market_snapshot"]["to_close_m"] is None


def test_decision_row_records_none_since_open_when_session_unavailable() -> None:
    report = {
        "ts": "2026-06-15T14:30:00+00:00",
        "dry_run": False,
        "symbols_due": ["SPY"],
        "model_calls_used": 0,
        "prices": {"SPY": 532.12},
        "stale_market_data": {},
        "portfolio": {"cash": 100000.0, "equity": 100000.0},
        "session_by_symbol": {"SPY": "not-a-dict"},
    }

    row = decision_ledger.build_decision_row(report, _armed_plan_decision(), sequence=0)

    assert row["market_snapshot"]["since_open_m"] is None
    assert row["market_snapshot"]["to_close_m"] is None
    assert row["market_snapshot"]["venue"] is None


def test_collect_session_by_symbol_swallows_snapshot_errors() -> None:
    def boom(_symbol: str) -> dict:
        raise RuntimeError("calendar unavailable")

    assert collect_session_by_symbol(["SPY", ""], snapshot=boom) == {}
