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
