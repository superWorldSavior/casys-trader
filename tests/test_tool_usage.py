import pytest

from trader.tool_usage import clamp_count, tool_usage_rates, tool_vs_quality


def _trace(tools_used: list[str], *, next_wake_outcome: str | None = None) -> dict:
    trace = []
    for tool in ["context_request", "indicator_watch", "next_wake", "order", "learning"]:
        item = {"tool": tool, "invoked": tool in tools_used}
        if tool == "next_wake" and next_wake_outcome is not None:
            item["outcome"] = next_wake_outcome
        trace.append(item)
    return {
        "tools_used": tools_used,
        "tools_skipped": [item["tool"] for item in trace if not item["invoked"]],
        "rounds": 0,
        "trace": trace,
    }


def test_tool_usage_rates_compte_used_skipped_et_taux() -> None:
    traces = [
        _trace(["context_request"]),
        _trace(["next_wake"]),
        _trace([]),
    ]

    rates = {item["tool"]: item for item in tool_usage_rates(traces)}

    assert rates["context_request"] == {
        "tool": "context_request",
        "used": 1,
        "skipped": 2,
        "usage_rate": pytest.approx(1 / 3),
    }
    assert rates["indicator_watch"] == {
        "tool": "indicator_watch",
        "used": 0,
        "skipped": 3,
        "usage_rate": 0.0,
    }
    assert [item["tool"] for item in tool_usage_rates(traces)] == [
        "context_request",
        "indicator_watch",
        "next_wake",
        "order",
        "learning",
    ]


def test_clamp_count_compte_les_next_wake_ecretes() -> None:
    traces = [
        _trace(["next_wake"], next_wake_outcome="clamped"),
        _trace(["next_wake"], next_wake_outcome="applied"),
        _trace([]),
        _trace(["context_request"]),
    ]

    assert clamp_count(traces) == 1


def test_tool_vs_quality_croise_usage_et_score_forward() -> None:
    scored_rows = [
        {"decision_id": "d1", "forward_return": 0.02, "evaluable": True},
        {"decision_id": "d2", "forward_return": -0.01, "evaluable": True},
        {"decision_id": "d3", "forward_return": None, "evaluable": False},
        {"decision_id": "missing", "forward_return": 0.50, "evaluable": True},
    ]
    traces_by_decision_id = {
        "d1": _trace(["context_request", "next_wake"], next_wake_outcome="applied"),
        "d2": _trace(["next_wake"], next_wake_outcome="clamped"),
        "d3": _trace([]),
    }

    rows = {
        (item["tool"], item["group"]): item
        for item in tool_vs_quality(scored_rows, traces_by_decision_id)
    }

    assert rows[("context_request", "used")] == {
        "tool": "context_request",
        "group": "used",
        "n": 1,
        "n_evaluable": 1,
        "mean_forward_return": pytest.approx(0.02),
    }
    assert rows[("context_request", "skipped")] == {
        "tool": "context_request",
        "group": "skipped",
        "n": 2,
        "n_evaluable": 1,
        "mean_forward_return": pytest.approx(-0.01),
    }
    assert rows[("next_wake", "used")]["n"] == 2
    assert rows[("next_wake", "used")]["n_evaluable"] == 2
    assert rows[("next_wake", "used")]["mean_forward_return"] == pytest.approx(0.005)
    assert rows[("learning", "used")]["n"] == 0
    assert rows[("learning", "used")]["n_evaluable"] == 0
    assert rows[("learning", "used")]["mean_forward_return"] is None
