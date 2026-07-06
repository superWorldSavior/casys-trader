from trader.application.tool_outcomes import (
    ACTION_TOOLS,
    finalize_action_tool_outcomes,
)


def test_finalize_action_tool_outcomes_rewrites_final_action_tools_only() -> None:
    entry = {
        "executed": True,
        "next_wake_requested": 30.0,
        "next_wake_in_minutes": 30.0,
        "indicator_watch_created": True,
        "cancel_watch_results": [{"watch_id": "SPY:a", "outcome": "cancelled"}],
        "amend_exit_applied": True,
        "tool_calls": [
            {"id": "1", "tool": "propose_order", "outcome": "ok"},
            {"id": "2", "tool": "set_next_wake", "outcome": "ok"},
            {"id": "3", "tool": "propose_indicator_watch", "outcome": "ok"},
            {"id": "4", "tool": "record_learning", "outcome": "ok"},
            {"id": "5", "tool": "cancel_watch", "outcome": "ok"},
            {"id": "6", "tool": "recall_learnings", "outcome": "ok"},
            {"id": "7", "tool": "amend_exit", "outcome": "ok"},
        ],
    }

    by_id = {call["id"]: call["outcome"] for call in finalize_action_tool_outcomes(entry)}

    assert by_id == {
        "1": "executed",
        "2": "applied",
        "3": "created",
        "4": "applied",
        "5": "cancelled",
        "6": "ok",
        "7": "applied",
    }


def test_finalize_action_tool_outcomes_attaches_amend_exit_warnings() -> None:
    warnings = [
        {
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance": 20.0,
            "distance_pct": 0.2,
            "limit_distance": 8.0,
            "limit_pct": 0.08,
        }
    ]
    entry = {
        "amend_exit_applied": True,
        "amend_exit_warnings": warnings,
        "tool_calls": [{"id": "1", "tool": "amend_exit", "outcome": "ok"}],
    }

    calls = finalize_action_tool_outcomes(entry)

    assert calls == [
        {
            "id": "1",
            "tool": "amend_exit",
            "outcome": "applied",
            "detail": {"warnings": warnings},
        }
    ]


def test_finalize_action_tool_outcomes_attaches_propose_order_risk_warnings() -> None:
    warnings = [
        {
            "code": "risk_per_trade_exceeded",
            "field": "max_risk_per_trade_pct",
            "risk_qty": 300.0,
            "max_qty": 200.0,
            "risk_pct": 0.015,
            "limit": 0.01,
        }
    ]
    entry = {
        "executed": True,
        "risk_warnings": warnings,
        "tool_calls": [{"id": "1", "tool": "propose_order", "outcome": "ok"}],
    }

    calls = finalize_action_tool_outcomes(entry)

    assert calls == [
        {
            "id": "1",
            "tool": "propose_order",
            "outcome": "executed",
            "detail": {"warnings": warnings},
        }
    ]


def test_finalize_action_tool_outcomes_attaches_propose_order_exit_plan_warnings() -> None:
    warnings = [
        {
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance_pct": 0.10,
            "limit_pct": 0.04,
        }
    ]
    entry = {
        "executed": True,
        "exit_plan_warnings": warnings,
        "tool_calls": [{"id": "1", "tool": "propose_order", "outcome": "ok"}],
    }

    calls = finalize_action_tool_outcomes(entry)

    assert calls == [
        {
            "id": "1",
            "tool": "propose_order",
            "outcome": "executed",
            "detail": {"warnings": warnings},
        }
    ]


def test_finalize_action_tool_outcomes_merges_propose_order_warnings() -> None:
    risk_warning = {"code": "risk_per_trade_exceeded", "field": "max_risk_per_trade_pct"}
    exit_warning = {"code": "hard_stop_above_max_pct", "field": "max_pct"}
    entry = {
        "executed": True,
        "risk_warnings": [risk_warning],
        "exit_plan_warnings": [exit_warning],
        "tool_calls": [{"id": "1", "tool": "propose_order", "outcome": "ok"}],
    }

    calls = finalize_action_tool_outcomes(entry)

    assert calls[0]["detail"]["warnings"] == [risk_warning, exit_warning]


def test_finalize_action_tool_outcomes_marks_blocked_clamped_rejected() -> None:
    entry = {
        "executed": False,
        "next_wake_requested": 120.0,
        "next_wake_in_minutes": 60.0,
        "indicator_watch_created": False,
        "cancel_watch_results": [{"watch_id": "X:a", "outcome": "not_owned"}],
        "amend_exit_applied": False,
        "amend_exit_reason": "resolve_failed:bad_stop",
        "tool_calls": [
            {"id": "1", "tool": "propose_order", "outcome": "ok"},
            {"id": "2", "tool": "set_next_wake", "outcome": "ok"},
            {"id": "3", "tool": "propose_indicator_watch", "outcome": "ok"},
            {"id": "4", "tool": "cancel_watch", "outcome": "ok"},
            {"id": "5", "tool": "amend_exit", "outcome": "ok"},
        ],
    }

    by_id = {call["id"]: call["outcome"] for call in finalize_action_tool_outcomes(entry)}

    assert by_id == {
        "1": "blocked",
        "2": "clamped",
        "3": "rejected",
        "4": "rejected",
        "5": "rejected",
    }


def test_finalize_action_tool_outcomes_keeps_noop_and_missing_calls_stable() -> None:
    assert finalize_action_tool_outcomes({"symbol": "SPY"}) is None
    assert finalize_action_tool_outcomes({"tool_calls": []}) == []
    assert finalize_action_tool_outcomes({"tool_calls": [{"id": "1", "tool": "cancel_watch", "outcome": "ok"}]}) == [
        {"id": "1", "tool": "cancel_watch", "outcome": "noop"}
    ]
    assert finalize_action_tool_outcomes({"tool_calls": [{"id": "2", "tool": "amend_exit", "outcome": "ok"}]}) == [
        {"id": "2", "tool": "amend_exit", "outcome": "noop"}
    ]


def test_action_tool_contract_lists_all_final_action_tools() -> None:
    assert ACTION_TOOLS == {
        "propose_order",
        "set_next_wake",
        "propose_indicator_watch",
        "record_learning",
        "cancel_watch",
        "amend_exit",
    }
