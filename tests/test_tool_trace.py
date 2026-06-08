from trader.tool_trace import summarize_tools


TOOL_ORDER = ["context_request", "indicator_watch", "next_wake", "order", "learning"]


def _summary(row: dict) -> dict:
    summary = summarize_tools(row)
    assert [item["tool"] for item in summary["trace"]] == TOOL_ORDER
    return summary


def _entry(summary: dict, tool: str) -> dict:
    return next(item for item in summary["trace"] if item["tool"] == tool)


def test_context_request_resolved() -> None:
    context_request = {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 1,
    }

    summary = _summary({"runtime": {"context_request": context_request}})

    assert summary["rounds"] == 1
    assert summary["tools_used"] == ["context_request"]
    assert _entry(summary, "context_request") == {
        "tool": "context_request",
        "invoked": True,
        "args": {
            "requested": context_request["requested"],
            "rounds": 1,
        },
        "outcome": "resolved",
        "detail": {"resolved": 1},
    }


def test_context_request_noop_quand_rien_resolu() -> None:
    context_request = {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 0,
    }

    summary = _summary({"runtime": {"context_request": context_request}})

    assert summary["rounds"] == 1
    assert summary["tools_used"] == ["context_request"]
    assert _entry(summary, "context_request")["outcome"] == "noop"
    assert _entry(summary, "context_request")["detail"] == {"resolved": 0}


def test_indicator_watch_created() -> None:
    summary = _summary(
        {
            "runtime": {
                "indicator_watch_requested": True,
                "indicator_watch_created": True,
            }
        }
    )

    assert summary["tools_used"] == ["indicator_watch"]
    assert _entry(summary, "indicator_watch") == {
        "tool": "indicator_watch",
        "invoked": True,
        "outcome": "created",
    }


def test_indicator_watch_rejected_avec_rejets() -> None:
    rejections = [{"reason": "unknown_indicator", "indicator": "er"}]
    summary = _summary(
        {
            "runtime": {
                "indicator_watch_requested": True,
                "indicator_watch_created": False,
                "indicator_watch_rejections": rejections,
            }
        }
    )

    assert summary["tools_used"] == ["indicator_watch"]
    assert _entry(summary, "indicator_watch") == {
        "tool": "indicator_watch",
        "invoked": True,
        "outcome": "rejected",
        "detail": {"rejections": rejections},
    }


def test_next_wake_applied() -> None:
    summary = _summary(
        {
            "next_wake_in_minutes": 30.0,
            "runtime": {"next_wake_requested": 30.0},
        }
    )

    assert summary["tools_used"] == ["next_wake"]
    assert _entry(summary, "next_wake") == {
        "tool": "next_wake",
        "invoked": True,
        "args": {"requested_minutes": 30.0},
        "outcome": "applied",
        "detail": {"effective_minutes": 30.0},
    }


def test_next_wake_clamped() -> None:
    summary = _summary(
        {
            "next_wake_in_minutes": 60.0,
            "runtime": {"next_wake_requested": 120.0},
        }
    )

    assert summary["tools_used"] == ["next_wake"]
    assert _entry(summary, "next_wake") == {
        "tool": "next_wake",
        "invoked": True,
        "args": {"requested_minutes": 120.0},
        "outcome": "clamped",
        "detail": {"effective_minutes": 60.0},
    }


def test_order_executed() -> None:
    summary = _summary(
        {
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
        }
    )

    assert summary["tools_used"] == ["order"]
    assert _entry(summary, "order") == {
        "tool": "order",
        "invoked": True,
        "args": {"action": "BUY", "intent": "OPEN_LONG"},
        "outcome": "executed",
        "detail": {"reason": "ok"},
    }


def test_order_trace_inclut_les_champs_risque_runtime() -> None:
    summary = _summary(
        {
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
            "runtime": {
                "risk_pct": 0.0075,
                "stop_distance": 5.0,
                "risk_clamped": True,
                "risk_unbounded_no_stop": False,
            },
        }
    )

    assert _entry(summary, "order")["detail"] == {
        "reason": "ok",
        "risk_pct": 0.0075,
        "stop_distance": 5.0,
        "risk_clamped": True,
        "risk_unbounded_no_stop": False,
    }


def test_order_blocked() -> None:
    summary = _summary(
        {
            "action": "SELL",
            "intent": "CLOSE",
            "executed": False,
            "reason": "risk:max_order_value",
        }
    )

    assert summary["tools_used"] == ["order"]
    assert _entry(summary, "order") == {
        "tool": "order",
        "invoked": True,
        "args": {"action": "SELL", "intent": "CLOSE"},
        "outcome": "blocked",
        "detail": {"reason": "risk:max_order_value"},
    }


def test_learning_applied() -> None:
    summary = _summary({"learning": "Surveiller le range SPY."})

    assert summary["tools_used"] == ["learning"]
    assert _entry(summary, "learning") == {
        "tool": "learning",
        "invoked": True,
        "outcome": "applied",
    }


def test_hold_pur_et_vieilles_rows_sans_cles() -> None:
    summary = _summary({"action": "HOLD"})

    assert summary["tools_used"] == []
    assert summary["tools_skipped"] == TOOL_ORDER
    assert summary["rounds"] == 0
    assert summary["trace"] == [{"tool": tool, "invoked": False} for tool in TOOL_ORDER]
