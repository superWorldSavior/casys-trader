import pytest

from trader.reporting.tool_trace import TOOLS, summarize_tools
from trader.reporting.read_models.tool_usage import (
    build_report,
    clamp_count,
    domain_tool_usage,
    risk_observability,
    tool_usage_rates,
    tool_vs_quality,
)


def _trace(
    tools_used: list[str],
    *,
    next_wake_outcome: str | None = None,
    order_outcome: str | None = None,
    order_intent: str | None = None,
    order_detail: dict | None = None,
) -> dict:
    trace = []
    for tool in ["context_request", "indicator_watch", "next_wake", "order", "learning"]:
        item = {"tool": tool, "invoked": tool in tools_used}
        if tool == "next_wake" and next_wake_outcome is not None:
            item["outcome"] = next_wake_outcome
        if tool == "order":
            if order_outcome is not None:
                item["outcome"] = order_outcome
            if order_intent is not None:
                item["args"] = {"intent": order_intent}
            if order_detail is not None:
                item["detail"] = order_detail
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


def test_risk_observability_rapporte_les_clamps_aux_ouvertures_pures() -> None:
    traces = [
        _trace(
            ["order"],
            order_outcome="executed",
            order_intent="OPEN_LONG",
            order_detail={
                "risk_pct": 0.0075,
                "risk_clamped": True,
                "risk_unbounded_no_stop": False,
            },
        ),
        _trace(
            ["order"],
            order_outcome="executed",
            order_intent="CLOSE",
            order_detail={},
        ),
    ]

    risk = risk_observability(traces)

    assert risk["executed_trades"] == 2
    assert risk["executed_trades_with_risk_pct"] == 1
    assert risk["mean_risk_pct"] == pytest.approx(0.0075)
    assert risk["risk_clamped_count"] == 1
    assert risk["risk_clamped_rate"] == pytest.approx(1.0)
    assert risk["opening_orders"] == 1


def test_risk_observability_rapporte_les_ouvertures_sans_stop_aux_ouvertures_tracees() -> None:
    traces = [
        _trace(
            ["order"],
            order_outcome="executed",
            order_intent="OPEN_LONG",
            order_detail={
                "risk_pct": None,
                "risk_clamped": False,
                "risk_unbounded_no_stop": True,
            },
        ),
        _trace(
            ["order"],
            order_outcome="executed",
            order_intent="OPEN_SHORT",
            order_detail={
                "risk_pct": 0.0125,
                "risk_clamped": False,
                "risk_unbounded_no_stop": False,
            },
        ),
        _trace(
            ["order"],
            order_outcome="blocked",
            order_intent="OPEN_LONG",
            order_detail={
                "risk_pct": None,
                "risk_clamped": None,
                "risk_unbounded_no_stop": None,
            },
        ),
        _trace(["order"], order_outcome="executed", order_intent="CLOSE", order_detail={}),
    ]

    risk = risk_observability(traces)

    assert risk == {
        "executed_trades": 3,
        "executed_trades_with_risk_pct": 1,
        "mean_risk_pct": pytest.approx(0.0125),
        "risk_clamped_count": 0,
        "risk_clamped_rate": 0.0,
        "opening_orders": 2,
        "risk_unbounded_no_stop_count": 1,
        "risk_unbounded_no_stop_rate": pytest.approx(0.5),
    }


def test_tool_usage_rates_compat_avec_domain_tools() -> None:
    """Séparation legacy / domain (design §8/§12, findings Codex 2026-07-02).

    tool_usage_rates ne retourne que les 5 pseudo-tools legacy (invariant legacy).
    Les domain tools (get_freshness, get_position_risk…) sont comptabilisés
    séparément via domain_tool_usage, qui scanne runtime.tool_calls bruts.
    """
    row_with_domain = {
        "action": "HOLD",
        "runtime": {
            "tool_rounds": 1,
            "tool_calls": [
                {
                    "id": "c1", "tool": "get_freshness",
                    "args": {"symbols": ["2330.TW"]}, "outcome": "ok", "detail": {},
                },
                {
                    "id": "c2", "tool": "get_position_risk",
                    "args": {"symbol": "2330.TW"}, "outcome": "rejected",
                    "detail": {"reason": "invalid_args"},
                },
            ],
        },
    }
    row_legacy = {
        "action": "HOLD",
        "runtime": {"next_wake_requested": 30.0},
        "next_wake_in_minutes": 30.0,
    }

    summary_domain = summarize_tools(row_with_domain)
    summary_legacy = summarize_tools(row_legacy)

    # Les domain tools sont présents dans tools_used de leur summary (via _domain_tool_traces).
    assert "get_freshness" in summary_domain["tools_used"]
    assert "get_position_risk" in summary_domain["tools_used"]

    # tool_usage_rates avec les deux summaries : counts legacy corrects.
    rates = {item["tool"]: item for item in tool_usage_rates([summary_domain, summary_legacy])}

    assert rates["next_wake"]["used"] == 1
    assert rates["next_wake"]["skipped"] == 1
    assert rates["context_request"]["used"] == 0
    assert rates["context_request"]["skipped"] == 2

    # Invariant legacy inchangé : tool_usage_rates ne retourne que les 5 outils legacy.
    assert set(rates.keys()) == set(TOOLS)

    # Domain tools tracés dans domain_tool_usage (section séparée, §8/§12).
    by_tool, global_outcomes = domain_tool_usage([row_with_domain, row_legacy])
    indexed = {item["tool"]: item for item in by_tool}
    assert "get_freshness" in indexed
    assert indexed["get_freshness"]["outcomes"] == {"ok": 1}
    assert "get_position_risk" in indexed
    assert indexed["get_position_risk"]["outcomes"] == {"rejected": 1}
    # row_legacy n'a pas de runtime.tool_calls → pas de domain tools
    assert indexed["get_freshness"]["used"] == 1
    assert global_outcomes == {"ok": 1, "rejected": 1}


def test_domain_tool_usage_quatre_outcomes() -> None:
    """domain_tool_usage comptabilise les 4 outcomes domain (ok/rejected/error/budget_exhausted)."""
    row = {
        "runtime": {
            "tool_calls": [
                {"tool": "get_freshness", "outcome": "ok", "args": {}, "detail": {}},
                {"tool": "get_freshness", "outcome": "rejected", "args": {}, "detail": {}},
                {"tool": "get_freshness", "outcome": "error", "args": {}, "detail": {}},
                {"tool": "get_freshness", "outcome": "budget_exhausted", "args": {}, "detail": {}},
                {"tool": "get_position_risk", "outcome": "ok", "args": {}, "detail": {}},
                # Outil legacy : doit être ignoré par domain_tool_usage.
                {"tool": "order", "outcome": "ok", "args": {}, "detail": {}},
            ],
        }
    }
    per_tool, global_outcomes = domain_tool_usage([row])
    by_tool = {item["tool"]: item for item in per_tool}

    assert "order" not in by_tool  # legacy tool ignoré
    assert by_tool["get_freshness"]["used"] == 4
    assert by_tool["get_freshness"]["outcomes"] == {
        "ok": 1, "rejected": 1, "error": 1, "budget_exhausted": 1,
    }
    assert by_tool["get_position_risk"]["used"] == 1
    assert global_outcomes == {"ok": 2, "rejected": 1, "error": 1, "budget_exhausted": 1}


def test_build_report_integre_les_agregats_risque(monkeypatch, tmp_path) -> None:
    def fake_score_from_ledger(ledger, *, band, days_buffer):
        return {
            "judgeable": [
                {
                    "decision_id": "d1",
                    "action": "BUY",
                    "intent": "OPEN_LONG",
                    "executed": True,
                    "reason": "ok",
                    "runtime": {
                        "risk_pct": 0.0075,
                        "risk_clamped": True,
                        "risk_unbounded_no_stop": False,
                    },
                },
                {
                    "decision_id": "d2",
                    "action": "BUY",
                    "intent": "OPEN_LONG",
                    "executed": True,
                    "reason": "ok",
                    "runtime": {
                        "risk_pct": None,
                        "risk_clamped": False,
                        "risk_unbounded_no_stop": True,
                    },
                },
            ],
            "scored_rows": [],
            "ledger_rows": [{"decision_id": "d1"}, {"decision_id": "d2"}],
            "window": ("2026-06-01", "2026-06-08"),
            "exclusions": {"stale_market_data": 0, "gates": 0, "by_reason": {}},
            "unavailable_symbols": [],
        }

    monkeypatch.setattr("trader.reporting.read_models.tool_usage.score_from_ledger", fake_score_from_ledger)

    report = build_report(tmp_path / "decisions.jsonl")

    assert report["risk"]["mean_risk_pct"] == pytest.approx(0.0075)
    assert report["risk"]["risk_clamped_count"] == 1
    assert report["risk"]["risk_unbounded_no_stop_count"] == 1
