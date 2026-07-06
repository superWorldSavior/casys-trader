from trader.reporting.tool_trace import (
    TOOLS,
    finalize_action_tool_outcomes,
    summarize_tools,
)


TOOL_ORDER = ["context_request", "indicator_watch", "next_wake", "order", "learning", "cancel_watch"]


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


def test_order_trace_inclut_les_warnings_exit_plan_runtime() -> None:
    warnings = [{"code": "hard_stop_above_max_pct", "field": "max_pct"}]
    summary = _summary(
        {
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
            "runtime": {"exit_plan_warnings": warnings},
        }
    )

    assert _entry(summary, "order")["detail"]["exit_plan_warnings"] == warnings


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
    # tools_skipped reste sur les 5 pseudo-tools legacy (cancel_watch n'y est pas :
    # c'est un outil optionnel, tracé seulement quand invoqué).
    assert summary["tools_skipped"] == TOOLS
    assert summary["rounds"] == 0
    assert summary["trace"] == [{"tool": tool, "invoked": False} for tool in TOOL_ORDER]


# ---------------------------------------------------------------------------
# Task 6 : summarize_tools voit les domain tools
# ---------------------------------------------------------------------------


def test_summarize_tools_inclut_les_domain_tools():
    row = {
        "action": "HOLD",
        "runtime": {
            "tool_rounds": 1,
            "tool_calls": [
                {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]},
                 "outcome": "ok", "detail": {"result_count": 1}},
                {"id": "c2", "tool": "get_position_risk", "args": {"symbol": "2330.TW"},
                 "outcome": "rejected", "detail": {"reason": "invalid_args"}},
            ],
        },
    }
    summary = summarize_tools(row)
    assert "get_freshness" in summary["tools_used"]
    assert "get_position_risk" in summary["tools_used"]
    assert summary["rounds"] == 1
    domain_items = [item for item in summary["trace"] if item["tool"] == "get_freshness"]
    assert domain_items[0]["outcome"] == "ok"


def test_summarize_tools_sans_domain_tools_inchange():
    summary = summarize_tools({"action": "HOLD", "runtime": {}})
    assert summary["tools_used"] == []
    assert summary["rounds"] == 0


def test_summarize_tools_domain_tools_error_et_budget_exhausted():
    """Les outcomes error et budget_exhausted dans runtime.tool_calls
    sont retranscrits fidèlement dans trace/tools_used."""
    row = {
        "action": "HOLD",
        "runtime": {
            "tool_rounds": 1,
            "tool_calls": [
                {
                    "id": "c1",
                    "tool": "get_indicator_context",
                    "args": {"symbol": "SPY"},
                    "outcome": "error",
                    "detail": {"message": "ValueError: handler crash"},
                },
                {
                    "id": "c2",
                    "tool": "get_freshness",
                    "args": {"symbols": ["SPY"]},
                    "outcome": "budget_exhausted",
                    "detail": {"reason": "max_total_calls"},
                },
            ],
        },
    }
    summary = summarize_tools(row)

    # Les deux outils sont tracés, quels que soient leurs outcomes.
    assert "get_indicator_context" in summary["tools_used"]
    assert "get_freshness" in summary["tools_used"]

    # Outcomes exacts préservés dans la trace.
    error_items = [item for item in summary["trace"] if item["tool"] == "get_indicator_context"]
    assert len(error_items) == 1
    assert error_items[0]["outcome"] == "error"
    assert error_items[0]["detail"]["message"] == "ValueError: handler crash"

    budget_items = [item for item in summary["trace"] if item["tool"] == "get_freshness"]
    assert len(budget_items) == 1
    assert budget_items[0]["outcome"] == "budget_exhausted"
    assert budget_items[0]["detail"]["reason"] == "max_total_calls"

    # rounds dérivé de tool_rounds
    assert summary["rounds"] == 1


def test_cancel_watch_absent_non_invoque() -> None:
    summary = summarize_tools({"symbol": "SPY", "runtime": {}})
    assert _entry(summary, "cancel_watch") == {"tool": "cancel_watch", "invoked": False}


def test_cancel_watch_annulee() -> None:
    """F4 : le résultat d'un cancel_watch (cancelled/not_owned) doit être dérivé
    dans la trace d'audit, comme watch/wake/order/learning — pas seulement en event."""
    row = {
        "symbol": "SPY",
        "runtime": {
            "cancel_watch_results": [{"watch_id": "SPY:abc", "outcome": "cancelled"}],
        },
    }
    summary = summarize_tools(row)

    assert "cancel_watch" in summary["tools_used"]
    assert _entry(summary, "cancel_watch") == {
        "tool": "cancel_watch",
        "invoked": True,
        "outcome": "cancelled",
        "detail": {"results": [{"watch_id": "SPY:abc", "outcome": "cancelled"}]},
    }


def test_cancel_watch_rejetee_si_non_possedee() -> None:
    """Un seul id non possédé par le symbole -> outcome rejected (ownership refusé)."""
    row = {
        "symbol": "SPY",
        "runtime": {
            "cancel_watch_results": [
                {"watch_id": "SPY:abc", "outcome": "cancelled"},
                {"watch_id": "QQQ:zzz", "outcome": "not_owned"},
            ],
        },
    }
    summary = summarize_tools(row)

    assert _entry(summary, "cancel_watch")["outcome"] == "rejected"


def test_finalize_reecrit_les_vrais_outcomes_des_action_tools() -> None:
    """F4 : les traces brutes runtime.tool_calls des action tools finaux portent
    outcome:"ok" figé. On les réécrit avec le résultat réel de la décision — SANS
    toucher les outils de tournée (recall_learnings/get_*) dont outcome=="ok" pilote
    le recall."""
    entry = {
        "executed": True,
        "next_wake_requested": 30.0,
        "next_wake_in_minutes": 30.0,
        "indicator_watch_created": True,
        "cancel_watch_results": [{"watch_id": "SPY:a", "outcome": "cancelled"}],
        "exit_update_applied": True,
        "tool_calls": [
            {"id": "1", "tool": "strategy_entry", "outcome": "ok"},
            {"id": "2", "tool": "set_next_wake", "outcome": "ok"},
            {"id": "3", "tool": "propose_indicator_watch", "outcome": "ok"},
            {"id": "4", "tool": "record_learning", "outcome": "ok"},
            {"id": "5", "tool": "cancel_watch", "outcome": "ok"},
            {"id": "6", "tool": "recall_learnings", "outcome": "ok"},
            {"id": "7", "tool": "strategy_exit", "outcome": "ok"},
        ],
    }

    by_id = {c["id"]: c["outcome"] for c in finalize_action_tool_outcomes(entry)}

    assert by_id == {
        "1": "executed",
        "2": "applied",
        "3": "created",
        "4": "applied",
        "5": "cancelled",
        "6": "ok",  # tool de tournée : outcome d'origine PRÉSERVÉ (pilote le recall)
        "7": "applied",
    }


def test_finalize_marque_blocked_clamped_rejected() -> None:
    entry = {
        "executed": False,
        "reason": "risk:gross_exposure_exceeded",
        "next_wake_requested": 120.0,
        "next_wake_in_minutes": 60.0,
        "indicator_watch_created": False,
        "cancel_watch_results": [{"watch_id": "X:a", "outcome": "not_owned"}],
        "exit_update_applied": False,
        "exit_update_reason": "resolve_failed:bad_stop",
        "tool_calls": [
            {"id": "1", "tool": "strategy_entry", "outcome": "ok"},
            {"id": "2", "tool": "set_next_wake", "outcome": "ok"},
            {"id": "3", "tool": "propose_indicator_watch", "outcome": "ok"},
            {"id": "5", "tool": "cancel_watch", "outcome": "ok"},
            {"id": "6", "tool": "strategy_exit", "outcome": "ok"},
        ],
    }

    by_id = {c["id"]: c["outcome"] for c in finalize_action_tool_outcomes(entry)}

    assert by_id == {"1": "blocked", "2": "clamped", "3": "rejected", "5": "rejected", "6": "rejected"}


def test_finalize_sans_tool_calls_ne_casse_pas() -> None:
    assert finalize_action_tool_outcomes({"symbol": "SPY"}) is None
    assert finalize_action_tool_outcomes({"tool_calls": []}) == []
