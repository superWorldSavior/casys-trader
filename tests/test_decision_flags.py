from trader.reporting.read_models.decision_flags import collect_flags, extract_flags


def _row(**overrides):
    row = {
        "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
        "cycle_ts": "2026-06-08T12:15:21+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "executed": True,
        "reason": "ok",
        "runtime": {},
    }
    row.update(overrides)
    return row


def test_extract_flags_deduplique_tool_detail_et_runtime_warning() -> None:
    warning = {
        "code": "risk_per_trade_exceeded",
        "field": "max_risk_per_trade_pct",
        "risk_pct": 0.015,
        "limit": 0.01,
        "context": "risk_qty=300 max_qty=200 risk_pct=0.015 limit=0.01",
    }
    row = _row(
        runtime={
            "risk_warnings": [warning],
            "tool_calls": [
                {
                    "id": "call-1",
                    "tool": "strategy_entry",
                    "outcome": "executed",
                    "detail": {"warnings": [warning]},
                }
            ],
        }
    )

    assert extract_flags(row) == [
        {
            "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
            "cycle_ts": "2026-06-08T12:15:21+00:00",
            "symbol": "SPY",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
            "tool": "strategy_entry",
            "outcome": "executed",
            "source": "tool_call",
            "code": "risk_per_trade_exceeded",
            "field": "max_risk_per_trade_pct",
            "risk_pct": 0.015,
            "limit": 0.01,
            "context": "risk_qty=300 max_qty=200 risk_pct=0.015 limit=0.01",
        }
    ]


def test_collect_flags_filtre_symbol_code_et_limit() -> None:
    rows = [
        _row(
            decision_id="1",
            symbol="SPY",
            runtime={"exit_plan_warnings": [{"code": "hard_stop_above_max_pct", "field": "max_pct"}]},
        ),
        _row(
            decision_id="2",
            symbol="QQQ",
            runtime={"risk_warnings": [{"code": "risk_per_trade_exceeded", "field": "max_risk_per_trade_pct"}]},
        ),
        _row(
            decision_id="3",
            symbol="SPY",
            runtime={"risk_warnings": [{"code": "risk_per_trade_exceeded", "field": "max_risk_per_trade_pct"}]},
        ),
    ]

    flags = collect_flags(rows, symbol="SPY", code="risk_per_trade_exceeded", limit=1)

    assert [flag["decision_id"] for flag in flags] == ["3"]


def test_extract_flags_cree_un_flag_de_refus_sans_warning_outil() -> None:
    row = _row(
        executed=False,
        reason="risk:order_value_exceeded",
        context="order_value=12000 max_order_value=10000",
        runtime={},
    )

    assert extract_flags(row) == [
        {
            "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
            "cycle_ts": "2026-06-08T12:15:21+00:00",
            "symbol": "SPY",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": False,
            "reason": "risk:order_value_exceeded",
            "tool": "strategy_entry",
            "outcome": "blocked",
            "source": "decision_reason",
            "code": "order_value_exceeded",
            "context": "order_value=12000 max_order_value=10000",
        }
    ]


def test_extract_flags_route_les_sorties_position_aware_vers_strategy_close() -> None:
    row = _row(
        action="SELL",
        intent="CLOSE",
        executed=False,
        reason="risk:nothing_to_close",
        runtime={},
    )

    flags = extract_flags(row)

    assert flags[0]["tool"] == "strategy_close"
    assert flags[0]["code"] == "nothing_to_close"


def test_extract_flags_route_flip_et_scale_in_vers_strategy_entry() -> None:
    for intent in ("FLIP", "SCALE_IN"):
        row = _row(
            action="BUY",
            intent=intent,
            executed=False,
            reason="risk:gross_exposure_exceeded",
            runtime={},
        )

        flags = extract_flags(row)

        assert flags[0]["tool"] == "strategy_entry"
        assert flags[0]["code"] == "gross_exposure_exceeded"


def test_extract_flags_ignore_hold_quiet_gate_synthetique() -> None:
    row = _row(action="HOLD", intent="HOLD", executed=False, reason="quiet_gate", runtime={})

    assert extract_flags(row) == []
