import ast
from pathlib import Path

import trader.reporting.read_models.decision_filters as decision_filters
from trader.reporting.read_models.decision_filters import (
    _count_filters,
    _filter_rows,
    _group_into_ledger_rows,
    _is_batch_row,
    _is_risk_row,
    _is_stale_row,
)


def _decision(symbol: str, action: str = "HOLD", **overrides: object) -> dict:
    row = {
        "symbol": symbol,
        "action": action,
        "reason": "hold",
        "cycle_ts": "2026-07-06T01:00:00+00:00",
        "decision_source": "llm",
        "model_called": True,
    }
    row.update(overrides)
    return row


def test_classifies_risk_stale_and_batch_rows() -> None:
    assert _is_risk_row(_decision("A", reason="risk:gross_exposure")) is True
    assert _is_risk_row(_decision("B", reason="blocked_no_stop")) is True
    assert _is_risk_row(_decision("C", reason="hold")) is False

    stale_state = {"stale_market_data": {"AAPL": {"data_age_minutes": 480}}}
    assert _is_stale_row(stale_state, _decision("AAPL", reason="hold")) is False
    assert _is_stale_row({}, _decision("SPY", reason="stale_market_data")) is True

    assert (
        _is_batch_row(
            _decision("D", decision_source="infra", model_called=False)
        )
        is True
    )
    assert (
        _is_batch_row(
            _decision("E", decision_source="infra_hold", model_called=False)
        )
        is True
    )
    assert _is_batch_row(_decision("F", decision_source="llm", model_called=True)) is False


def test_filters_and_counts_decision_rows() -> None:
    rows = [
        _decision("BUY-1", "BUY"),
        _decision("SELL-1", "SELL"),
        _decision("HOLD-1", "HOLD"),
        _decision("RISK-1", "HOLD", reason="risk:order_value_exceeded"),
        _decision("STALE-1", "HOLD", reason="stale_market_data"),
    ]

    assert [row["symbol"] for row in _filter_rows(rows, "buy", {})] == ["BUY-1"]
    assert [row["symbol"] for row in _filter_rows(rows, "sell", {})] == ["SELL-1"]
    assert [row["symbol"] for row in _filter_rows(rows, "hold", {})] == [
        "HOLD-1",
        "RISK-1",
        "STALE-1",
    ]
    assert [row["symbol"] for row in _filter_rows(rows, "risk", {})] == ["RISK-1"]
    assert [row["symbol"] for row in _filter_rows(rows, "stale", {})] == ["STALE-1"]
    assert _filter_rows(rows, "all", {}) == rows

    assert _count_filters(rows, {}) == {
        "all": 5,
        "buy": 1,
        "sell": 1,
        "hold": 3,
        "risk": 1,
        "stale": 1,
    }


def test_groups_large_consecutive_infra_holds_per_cycle() -> None:
    ts = "2026-07-06T01:00:00+00:00"
    rows = [
        _decision(f"BATCH-{idx}", decision_source="infra", model_called=False, cycle_ts=ts)
        for idx in range(3)
    ]
    rows.append(_decision("LLM-1", cycle_ts=ts))

    grouped = _group_into_ledger_rows(rows)

    assert grouped[0] == {
        "_is_batch_summary": True,
        "cycle_ts": ts,
        "symbol": "— batch",
        "action": "HOLD",
        "decision_source": "heuristic",
        "reason": "3 due — 0 LLM calls, 3 quiet holds",
    }
    assert grouped[1]["symbol"] == "LLM-1"


def test_small_batch_candidates_pass_through() -> None:
    rows = [
        _decision("BATCH-1", decision_source="infra", model_called=False),
        _decision("BATCH-2", decision_source="infra", model_called=False),
    ]

    assert _group_into_ledger_rows(rows) == rows


def test_decision_filters_has_no_ui_imports() -> None:
    module_path = Path(decision_filters.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(
        module == "textual"
        or module.startswith("textual.")
        or module == "trader.interfaces"
        or module.startswith("trader.interfaces.")
        for module in imported_modules
    )


def test_cockpit_decisions_page_does_not_define_filtering_logic() -> None:
    page_path = Path("trader/interfaces/cockpit/pages/decisions.py")
    tree = ast.parse(page_path.read_text(encoding="utf-8"))
    local_defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert local_defs.isdisjoint(
        {
            "_count_filters",
            "_filter_rows",
            "_group_into_ledger_rows",
            "_is_batch_row",
            "_is_risk_row",
            "_is_stale_row",
        }
    )
