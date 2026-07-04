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
