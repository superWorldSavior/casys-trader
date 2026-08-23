import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
            _decision(
                "D",
                decision_source="infra",
                model_called=False,
                reason="quiet_gate",
            )
        )
        is True
    )
    assert (
        _is_batch_row(
            _decision(
                "E",
                decision_source="infra_hold",
                model_called=False,
                reason="quiet_gate",
            )
        )
        is True
    )
    assert (
        _is_batch_row(
            _decision(
                "STALE-ARMED",
                reason="stale_market_data",
                decision_source="infra",
                model_called=False,
                source="armed_plan",
            )
        )
        is False
    )
    assert _is_batch_row(_decision("F", decision_source="llm", model_called=True)) is False


def test_is_batch_row_rejects_non_quiet_infra_rows() -> None:
    infra = {"decision_source": "infra", "model_called": False}
    assert _is_batch_row(_decision("Q", reason="quiet_gate", **infra)) is True
    for reason in (
        "stale_market_data",
        "risk:gross_exposure",
        "blocked_no_stop",
        "no_decision_in_batch",
        "model_call_budget_exhausted",
        "hold",
        "",
        "unknown_reason",
    ):
        assert (
            _is_batch_row(_decision("X", reason=reason, **infra)) is False
        ), reason
    missing = _decision("MISSING", **infra)
    del missing["reason"]
    assert _is_batch_row(missing) is False


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
        _decision(
            f"BATCH-{idx}",
            decision_source="infra",
            model_called=False,
            cycle_ts=ts,
            reason="quiet_gate",
        )
        for idx in range(3)
    ]
    rows.append(_decision("LLM-1", cycle_ts=ts))

    grouped = _group_into_ledger_rows(rows)

    assert grouped[0] == {
        "_is_batch_summary": True,
        "summary_kind": "automatic_cycle",
        "cycle_ts": ts,
        "symbol": "— batch",
        "action": "HOLD",
        "decision_source": "infra",
        "model_called": False,
        "reason": "3 due — 0 LLM calls, 3 quiet holds",
    }
    assert grouped[1]["symbol"] == "LLM-1"
    assert grouped[0]["decision_source"] != "llm"
    assert grouped[0]["model_called"] is not True


def test_small_batch_candidates_pass_through() -> None:
    rows = [
        _decision("BATCH-1", decision_source="infra", model_called=False, reason="quiet_gate"),
        _decision("BATCH-2", decision_source="infra", model_called=False, reason="quiet_gate"),
    ]

    assert _group_into_ledger_rows(rows) == rows


def test_group_keeps_non_quiet_infra_rows_individually_visible() -> None:
    ts = "2026-07-06T01:00:00+00:00"
    cases = (
        "stale_market_data",
        "risk:gross_exposure",
        "blocked_no_stop",
        "no_decision_in_batch",
        "model_call_budget_exhausted",
        "unknown_reason",
    )
    for reason in cases:
        rows = [
            _decision(
                f"{reason}-{idx}",
                decision_source="infra",
                model_called=False,
                cycle_ts=ts,
                reason=reason,
            )
            for idx in range(3)
        ]
        grouped = _group_into_ledger_rows(rows)
        assert grouped == rows
        assert not any(row.get("_is_batch_summary") for row in grouped)

    quiet = [
        _decision(
            f"Q-{idx}",
            decision_source="infra",
            model_called=False,
            cycle_ts=ts,
            reason="quiet_gate",
        )
        for idx in range(3)
    ]
    stale = [
        _decision(
            f"S-{idx}",
            decision_source="infra",
            model_called=False,
            cycle_ts=ts,
            reason="stale_market_data",
        )
        for idx in range(3)
    ]
    mixed = _group_into_ledger_rows(quiet + stale)
    assert mixed[0]["_is_batch_summary"] is True
    assert mixed[0]["summary_kind"] == "automatic_cycle"
    assert mixed[1:] == stale


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


def _load_desktop_bridge_api():
    path = Path(__file__).resolve().parents[2] / "desktop" / "bridge" / "api.py"
    spec = importlib.util.spec_from_file_location("desktop_bridge_api", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_handle_decisions_forwards_automatic_cycle_summary_kind(monkeypatch) -> None:
    ts = "2026-07-06T01:00:00+00:00"
    state = {
        "recent_decisions": [
            _decision(
                f"BATCH-{idx}",
                decision_source="infra",
                model_called=False,
                cycle_ts=ts,
                reason="quiet_gate",
            )
            for idx in range(3)
        ]
    }
    bridge_api = _load_desktop_bridge_api()
    monkeypatch.setattr(bridge_api, "load_state", lambda: state)

    payload = bridge_api.handle_decisions(
        SimpleNamespace(filter="all", limit=80, symbol="")
    )

    assert payload["rows"]
    summary = payload["rows"][0]
    assert summary["is_batch"] is True
    assert summary["summary_kind"] == "automatic_cycle"
    assert summary["decision_source"] == "infra"
    assert summary["model_called"] is False


_INTERNAL_IDS = {
    "process_instance_id": "proc-secret",
    "attempt_id": "att-secret",
    "decision_id": "dec-secret",
    "queue_task_id": "task-secret",
    "order_id": "order-secret",
    "fill_id": "fill-secret",
    "task_id": "task-id-secret",
    "process_id": "process-secret",
}

_FORBIDDEN_PUBLIC_KEYS = {
    "effect_status",
    "queue_fill_verified",
    "queue_terminal",
    "filled_at",
    "position_quantity",
    "process_instance_id",
    "attempt_id",
    "decision_id",
    "queue_task_id",
    "order_id",
    "fill_id",
    "task_id",
    "process_id",
    "broker_fill",
    "portfolio_readback",
    "effect_refs",
    "execution_proof",
    "verified",
    "cash",
    "equity",
}


def _verified_broker_fill(**overrides: object) -> dict:
    fill = {
        "symbol": "AAPL",
        "process_instance_id": _INTERNAL_IDS["process_instance_id"],
        "attempt_id": _INTERNAL_IDS["attempt_id"],
        "decision_id": _INTERNAL_IDS["decision_id"],
        "order_id": _INTERNAL_IDS["order_id"],
        "fill_id": _INTERNAL_IDS["fill_id"],
        "ts": "2026-08-22T10:15:00+00:00",
    }
    fill.update(overrides)
    return fill


def _verified_portfolio_readback(**overrides: object) -> dict:
    readback = {
        "cash": 88_000.0,
        "equity": 101_250.0,
        "position_quantity": 12.0,
    }
    readback.update(overrides)
    return readback


def _verified_nested_decision(**overrides: object) -> dict:
    nested = {
        "effect_status": "verified",
        "queue_fill_verified": True,
        "queue_terminal": "done",
        "process_instance_id": _INTERNAL_IDS["process_instance_id"],
        "attempt_id": _INTERNAL_IDS["attempt_id"],
        "decision_id": _INTERNAL_IDS["decision_id"],
        "queue_task_id": _INTERNAL_IDS["queue_task_id"],
        "task_id": _INTERNAL_IDS["task_id"],
        "process_id": _INTERNAL_IDS["process_id"],
        "effect_refs": {
            "broker_fill": _verified_broker_fill(),
            "portfolio_readback": _verified_portfolio_readback(),
        },
    }
    nested.update(overrides)
    return nested


def _executed_row(**overrides: object) -> dict:
    row = _decision(
        "AAPL",
        "BUY",
        executed=True,
        qty=12,
        price=187.5,
        reason="ok",
        decision_id=_INTERNAL_IDS["decision_id"],
    )
    row.update(overrides)
    return row


def _decisions_payload(monkeypatch, rows: list[dict]) -> dict:
    bridge_api = _load_desktop_bridge_api()
    monkeypatch.setattr(bridge_api, "load_state", lambda: {"recent_decisions": rows})
    return bridge_api.handle_decisions(
        SimpleNamespace(filter="all", limit=80, symbol="")
    )


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys |= _walk_keys(nested)
    elif isinstance(value, list):
        for item in value:
            keys |= _walk_keys(item)
    return keys


def _assert_public_execution_contract(
    payload: object,
    *,
    execution_status: str | None,
) -> None:
    assert isinstance(payload, dict)
    row = payload["rows"][0]
    assert isinstance(row, dict)
    public_execution_keys = {key for key in row if key.startswith("execution_")}
    if execution_status is None:
        assert row.get("execution_status") is None
        assert public_execution_keys <= {"execution_status"}
    else:
        assert row["execution_status"] == execution_status
        assert public_execution_keys == {"execution_status"}
    leaked = _walk_keys(payload) & _FORBIDDEN_PUBLIC_KEYS
    assert not leaked, leaked
    blob = json.dumps(payload)
    for token in _INTERNAL_IDS.values():
        assert token not in blob
    for key in ("broker_fill", "portfolio_readback", "effect_refs", "execution_proof"):
        assert key not in row


def test_handle_decisions_projects_confirmed_execution_status(monkeypatch) -> None:
    payload = _decisions_payload(
        monkeypatch,
        [_executed_row(decision=_verified_nested_decision())],
    )

    assert payload["rows"][0]["executed"] is True
    _assert_public_execution_contract(payload, execution_status="confirmed")


def test_handle_decisions_incomplete_execution_is_recorded(monkeypatch) -> None:
    cases = {
        "missing_portfolio_readback": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={"broker_fill": _verified_broker_fill()}
            )
        ),
        "queue_fill_not_verified": _executed_row(
            decision=_verified_nested_decision(queue_fill_verified=False)
        ),
        "queue_fill_malformed": _executed_row(
            decision=_verified_nested_decision(queue_fill_verified="true")
        ),
        "effect_status_not_verified": _executed_row(
            decision=_verified_nested_decision(effect_status="unknown")
        ),
        "effect_status_malformed": _executed_row(
            decision=_verified_nested_decision(effect_status=True)
        ),
        "effect_refs_malformed": _executed_row(
            decision=_verified_nested_decision(effect_refs=["broker_fill"])
        ),
        "broker_fill_malformed": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": "filled",
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "portfolio_readback_malformed": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": 12.0,
                }
            )
        ),
        "empty_receipts": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={"broker_fill": {}, "portfolio_readback": {}}
            )
        ),
        "empty_ts": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(ts=""),
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "queue_terminal_not_done": _executed_row(
            decision=_verified_nested_decision(queue_terminal="timeout")
        ),
        "wrong_symbol": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(symbol="MSFT"),
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "mismatched_nested_decision_id": _executed_row(
            decision=_verified_nested_decision(decision_id="other-dec")
        ),
        "mismatched_source_decision_id": _executed_row(
            decision=_verified_nested_decision(),
            decision_id="other-source-dec",
        ),
        "mismatched_fill_decision_id": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(decision_id="other-dec"),
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "mismatched_fill_process_instance_id": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(
                        process_instance_id="other-proc"
                    ),
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "mismatched_fill_attempt_id": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(attempt_id="other-att"),
                    "portfolio_readback": _verified_portfolio_readback(),
                }
            )
        ),
        "mismatched_nested_process_instance_id": _executed_row(
            decision=_verified_nested_decision(process_instance_id="other-proc")
        ),
        "mismatched_nested_attempt_id": _executed_row(
            decision=_verified_nested_decision(attempt_id="other-att")
        ),
        "missing_cash": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": {
                        "equity": 101_250.0,
                        "position_quantity": 12.0,
                    },
                }
            )
        ),
        "missing_equity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": {
                        "cash": 88_000.0,
                        "position_quantity": 12.0,
                    },
                }
            )
        ),
        "missing_position_quantity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": {
                        "cash": 88_000.0,
                        "equity": 101_250.0,
                    },
                }
            )
        ),
        "nonfinite_cash": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(
                        cash=float("nan")
                    ),
                }
            )
        ),
        "nonfinite_equity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(
                        equity=float("inf")
                    ),
                }
            )
        ),
        "nonfinite_position_quantity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(
                        position_quantity=float("-inf")
                    ),
                }
            )
        ),
        "bool_cash": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(cash=True),
                }
            )
        ),
        "bool_equity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(equity=False),
                }
            )
        ),
        "bool_position_quantity": _executed_row(
            decision=_verified_nested_decision(
                effect_refs={
                    "broker_fill": _verified_broker_fill(),
                    "portfolio_readback": _verified_portfolio_readback(
                        position_quantity=True
                    ),
                }
            )
        ),
    }

    for label, row in cases.items():
        payload = _decisions_payload(monkeypatch, [row])
        assert payload["rows"][0]["executed"] is True, label
        _assert_public_execution_contract(payload, execution_status="recorded")


def test_handle_decisions_legacy_executed_row_is_recorded(monkeypatch) -> None:
    payload = _decisions_payload(monkeypatch, [_executed_row()])

    assert payload["rows"][0]["executed"] is True
    _assert_public_execution_contract(payload, execution_status="recorded")


def test_handle_decisions_non_executed_row_omits_execution_status(monkeypatch) -> None:
    payload = _decisions_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                executed=False,
                decision=_verified_nested_decision(),
            )
        ],
    )

    assert payload["rows"][0].get("executed") is not True
    _assert_public_execution_contract(payload, execution_status=None)


def _symbol_state(rows: list[dict], symbol: str = "AAPL") -> dict:
    return {
        "recent_decisions": rows,
        "company_map": {symbol: "Apple"},
        "portfolio": {"holdings": []},
        "trade_plans": [],
        "indicator_watches": [],
        "armed_plans": [],
        "symbol_wakes": {},
        "stale_market_data": {},
    }


_FORBIDDEN_SYMBOL_PUBLIC_KEYS = _FORBIDDEN_PUBLIC_KEYS | {
    "armed_plan_id",
    "data_source",
    "rationale",
    "reason",
    "reject_reason",
    "runtime",
}


def _symbol_payload(
    monkeypatch,
    rows: list[dict],
    symbol: str = "AAPL",
    *,
    ledger_path: Path | None = None,
) -> dict:
    bridge_api = _load_desktop_bridge_api()
    monkeypatch.setattr(bridge_api, "load_state", lambda: _symbol_state(rows, symbol))
    monkeypatch.setattr(
        bridge_api,
        "DECISIONS_LEDGER_PATH",
        ledger_path if ledger_path is not None else Path("/nonexistent/casys-decisions.jsonl"),
    )
    payload = bridge_api.handle_symbol(SimpleNamespace(symbol=symbol))
    assert isinstance(payload, dict)
    return payload


def _write_decisions_ledger(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _assert_symbol_public_surface(payload: dict) -> None:
    for key in ("decisions", "why"):
        blob = payload.get(key)
        leaked = _walk_keys(blob) & _FORBIDDEN_SYMBOL_PUBLIC_KEYS
        assert not leaked, (key, leaked)
    for token in ("armed_plan_id", "execution.enabled", "sticky_unmandated", "chart_breakout"):
        packed = json.dumps({"decisions": payload.get("decisions"), "why": payload.get("why")})
        assert token not in packed


def _assert_symbol_why_is_llm(payload: dict, **expected: object) -> dict:
    why = payload["why"]
    assert isinstance(why, dict)
    assert str(why.get("decision_source") or "").strip().lower() == "llm"
    assert why.get("model_called") is True
    for key, value in expected.items():
        assert why.get(key) == value, (key, why.get(key), value)
    return why


def test_handle_symbol_newer_infra_does_not_hide_latest_strict_llm(monkeypatch) -> None:
    payload = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                rationale="Keep the position after review.",
                cycle_ts="2026-07-01T01:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "HOLD",
                decision_source="infra",
                model_called=False,
                reason="quiet_gate",
                rationale="",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
        ],
    )

    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    assert "rationale" not in why
    assert "reason" not in why
    assert why["review_summary"] == (
        "Casys reviewed the company and kept the portfolio unchanged."
    )
    assert payload["decisions"][0]["decision_source"] == "infra"
    assert "quiet_gate" not in json.dumps(payload["decisions"][0])
    _assert_symbol_public_surface(payload)


def test_handle_symbol_excludes_llm_without_model_called(monkeypatch) -> None:
    payload = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                rationale="Authoritative review.",
                cycle_ts="2026-07-01T01:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "HOLD",
                model_called=False,
                rationale="Cached leftover text.",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
        ],
    )

    why = _assert_symbol_why_is_llm(
        payload,
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    assert why["review_summary"] == (
        "Casys reviewed the company and kept the portfolio unchanged."
    )
    assert "Cached leftover text." not in json.dumps(why)
    assert "Authoritative review." not in json.dumps(why)
    _assert_symbol_public_surface(payload)


def test_handle_symbol_empty_llm_rationale_does_not_borrow_old_text(monkeypatch) -> None:
    payload = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                rationale="Older written review.",
                cycle_ts="2026-07-01T01:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "BUY",
                rationale="",
                reason="hold",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
        ],
    )

    why = _assert_symbol_why_is_llm(
        payload,
        action="BUY",
        cycle_ts="2026-07-01T02:00:00+00:00",
    )
    assert "rationale" not in why
    assert "reason" not in why
    assert "Older written review." not in json.dumps(why)
    assert "kept the portfolio unchanged" not in why["review_summary"]
    assert why["review_summary"] == (
        "Casys decided to buy. No execution is claimed here."
    )
    _assert_symbol_public_surface(payload)


def test_handle_symbol_projects_confirmed_and_recorded_without_proof_leak(
    monkeypatch,
) -> None:
    confirmed = _symbol_payload(
        monkeypatch,
        [_executed_row(decision=_verified_nested_decision())],
    )
    recorded = _symbol_payload(
        monkeypatch,
        [_executed_row()],
    )

    assert confirmed["decisions"][0]["executed"] is True
    assert confirmed["decisions"][0]["execution_status"] == "confirmed"
    assert recorded["decisions"][0]["executed"] is True
    assert recorded["decisions"][0]["execution_status"] == "recorded"
    for payload in (confirmed, recorded):
        _assert_public_execution_contract(
            {"rows": payload["decisions"]},
            execution_status=payload["decisions"][0]["execution_status"],
        )
        leaked = _walk_keys(payload["decisions"]) & _FORBIDDEN_SYMBOL_PUBLIC_KEYS
        assert not leaked, leaked
        leaked_why = _walk_keys(payload.get("why") or {}) & _FORBIDDEN_SYMBOL_PUBLIC_KEYS
        assert not leaked_why, leaked_why
        blob = json.dumps({"decisions": payload["decisions"], "why": payload.get("why")})
        for token in _INTERNAL_IDS.values():
            assert token not in blob
        _assert_symbol_public_surface(payload)


def test_handle_symbol_why_scan_continues_after_display_cap(monkeypatch) -> None:
    rows = [
        _decision(
            "AAPL",
            "HOLD",
            rationale="Keep after the full review.",
            cycle_ts="2026-07-01T00:00:00+00:00",
        )
    ]
    for index in range(20):
        rows.append(
            _decision(
                "AAPL",
                "HOLD",
                decision_source="infra",
                model_called=False,
                reason="quiet_gate",
                rationale="",
                cycle_ts=f"2026-07-02T00:{index:02d}:00+00:00",
            )
        )

    payload = _symbol_payload(monkeypatch, rows)

    assert len(payload["decisions"]) == 20
    assert all(row.get("decision_source") == "infra" for row in payload["decisions"])
    why = _assert_symbol_why_is_llm(
        payload,
        cycle_ts="2026-07-01T00:00:00+00:00",
    )
    assert "Keep after the full review." not in json.dumps(why)
    assert why["review_summary"] == (
        "Casys reviewed the company and kept the portfolio unchanged."
    )
    _assert_symbol_public_surface(payload)


def test_handle_symbol_watch_and_trade_summaries_omit_raw_fields(monkeypatch) -> None:
    watch = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                rationale="chart_breakout == -1 @4h range_pos execution.enabled=false",
                reason="hold",
                runtime={
                    "indicator_watch_created": True,
                    "armed_plan_id": "plan-secret",
                    "data_source": "ib",
                    "reject_reason": "sticky_unmandated",
                },
            )
        ],
    )
    buy = _symbol_payload(
        monkeypatch,
        [_decision("AAPL", "BUY", rationale="ER/RS/SL24 plan-secret", reason="ok")],
    )
    sell = _symbol_payload(
        monkeypatch,
        [_decision("AAPL", "SELL", rationale="session_closed", reason="ok")],
    )
    close = _symbol_payload(
        monkeypatch,
        [_decision("AAPL", "CLOSE", rationale="sticky_unmandated", reason="ok")],
    )
    stale = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                decision_reason_code="DATA_STALE",
                reason="stale_market_data",
                rationale="stale_market_data",
            )
        ],
    )
    closed = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                decision_reason_code="MARKET_CLOSED",
                reason="session_closed",
                rationale="marché fermé",
            )
        ],
    )

    assert watch["why"]["review_summary"] == (
        "Casys kept the portfolio unchanged and recorded another market condition to watch."
    )
    assert buy["why"]["review_summary"] == (
        "Casys decided to buy. No execution is claimed here."
    )
    assert sell["why"]["review_summary"] == (
        "Casys decided to sell. No execution is claimed here."
    )
    assert close["why"]["review_summary"] == (
        "Casys decided to close the position. No execution is claimed here."
    )
    assert stale["why"]["review_summary"] == (
        "Casys waited because the available market data was too old."
    )
    assert closed["why"]["review_summary"] == (
        "Casys reviewed the company while the market was closed."
    )
    for payload in (watch, buy, sell, close, stale, closed):
        _assert_symbol_public_surface(payload)
        assert payload["why"].get("runtime") is None
        assert "plan-secret" not in json.dumps(payload["why"])


def test_handle_symbol_reads_strict_review_from_durable_ledger(
    monkeypatch, tmp_path: Path
) -> None:
    ledger = _write_decisions_ledger(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                "MSFT",
                "HOLD",
                rationale="Other company.",
                cycle_ts="2026-07-01T03:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "HOLD",
                rationale="Keep after a durable review.",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "HOLD",
                decision_source="infra",
                model_called=False,
                reason="quiet_gate",
                cycle_ts="2026-07-01T04:00:00+00:00",
            ),
        ],
    )
    tail = [
        _decision(
            "MSFT",
            "HOLD",
            cycle_ts="2026-07-02T00:00:00+00:00",
        ),
        _decision(
            "AAPL",
            "HOLD",
            decision_source="infra",
            model_called=False,
            reason="quiet_gate",
            cycle_ts="2026-07-02T01:00:00+00:00",
        ),
    ]
    payload = _symbol_payload(monkeypatch, tail, ledger_path=ledger)

    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T02:00:00+00:00",
    )
    assert why["review_summary"] == (
        "Casys reviewed the company and kept the portfolio unchanged."
    )
    assert "Keep after a durable review." not in json.dumps(payload)
    assert payload["decisions"][0]["decision_source"] == "infra"
    assert all(row.get("cycle_ts") != "2026-07-01T02:00:00+00:00" for row in payload["decisions"])
    _assert_symbol_public_surface(payload)


def test_handle_symbol_durable_newest_matching_row_wins(
    monkeypatch, tmp_path: Path
) -> None:
    ledger = _write_decisions_ledger(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                "AAPL",
                "HOLD",
                cycle_ts="2026-07-01T01:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "BUY",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
            _decision(
                "MSFT",
                "SELL",
                cycle_ts="2026-07-01T03:00:00+00:00",
            ),
        ],
    )
    payload = _symbol_payload(monkeypatch, [], ledger_path=ledger)
    why = _assert_symbol_why_is_llm(
        payload,
        action="BUY",
        cycle_ts="2026-07-01T02:00:00+00:00",
    )
    assert why["review_summary"].startswith("Casys decided to buy.")
    _assert_symbol_public_surface(payload)


def test_handle_symbol_durable_long_line_crossing_blocks(
    monkeypatch, tmp_path: Path
) -> None:
    api = _load_desktop_bridge_api()
    monkeypatch.setattr(api, "_DECISIONS_SCAN_BLOCK_SIZE", 64)
    long_row = _decision(
        "AAPL",
        "HOLD",
        rationale="L" * 400,
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    ledger = _write_decisions_ledger(
        tmp_path / "decisions.jsonl",
        [
            _decision("MSFT", "HOLD", cycle_ts="2026-07-01T00:00:00+00:00"),
            long_row,
            _decision("MSFT", "BUY", cycle_ts="2026-07-01T02:00:00+00:00"),
        ],
    )
    monkeypatch.setattr(api, "DECISIONS_LEDGER_PATH", ledger)
    monkeypatch.setattr(api, "load_state", lambda: _symbol_state([], "AAPL"))
    payload = api.handle_symbol(SimpleNamespace(symbol="AAPL"))
    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    packed = json.dumps(why)
    assert "L" * 20 not in packed
    assert why["review_summary"] == (
        "Casys reviewed the company and kept the portfolio unchanged."
    )


def test_handle_symbol_durable_malformed_lines_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "decisions.jsonl"
    good = _decision(
        "AAPL",
        "HOLD",
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps(["not", "an", "object"]),
                json.dumps(good),
                "{still-broken",
                json.dumps(_decision("MSFT", "BUY")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload = _symbol_payload(monkeypatch, [], ledger_path=path)
    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    assert why["review_summary"].startswith("Casys reviewed the company")
    _assert_symbol_public_surface(payload)


def test_handle_symbol_durable_excludes_llm_without_model_called(
    monkeypatch, tmp_path: Path
) -> None:
    ledger = _write_decisions_ledger(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                "AAPL",
                "HOLD",
                rationale="Authoritative durable review.",
                cycle_ts="2026-07-01T01:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "BUY",
                model_called=False,
                rationale="Cached leftover.",
                cycle_ts="2026-07-01T02:00:00+00:00",
            ),
            _decision(
                "AAPL",
                "SELL",
                decision_source="infra",
                model_called=True,
                cycle_ts="2026-07-01T03:00:00+00:00",
            ),
        ],
    )
    payload = _symbol_payload(monkeypatch, [], ledger_path=ledger)
    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T01:00:00+00:00",
    )
    assert why["review_summary"].startswith("Casys reviewed the company")
    assert "Cached leftover." not in json.dumps(payload)
    _assert_symbol_public_surface(payload)


def test_handle_symbol_memory_strict_review_skips_durable_ledger(
    monkeypatch, tmp_path: Path
) -> None:
    ledger = _write_decisions_ledger(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                "AAPL",
                "BUY",
                cycle_ts="2026-07-02T00:00:00+00:00",
            )
        ],
    )
    payload = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                cycle_ts="2026-07-01T00:00:00+00:00",
            )
        ],
        ledger_path=ledger,
    )
    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        cycle_ts="2026-07-01T00:00:00+00:00",
    )
    assert why["review_summary"].startswith("Casys reviewed the company")
    assert payload["why"]["action"] != "BUY"


def test_handle_symbol_hold_risk_limit_does_not_claim_a_stopped_trade(
    monkeypatch,
) -> None:
    payload = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                decision_reason_code="RISK_LIMIT",
                reason="risk:gross_exposure",
                executed=False,
                rationale="risk:gross_exposure would leak",
            )
        ],
    )
    why = _assert_symbol_why_is_llm(
        payload,
        action="HOLD",
        executed=False,
    )
    assert why["review_summary"] == (
        "Casys kept the portfolio unchanged because risk limits left no room "
        "for a new position."
    )
    assert "stopped a trade" not in why["review_summary"]
    assert "before execution" not in why["review_summary"]
    packed = json.dumps(payload)
    assert "risk:gross_exposure would leak" not in packed
    assert "gross_exposure" not in json.dumps(why)
    _assert_symbol_public_surface(payload)

    blocked = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "BUY",
                decision_reason_code="RISK_LIMIT",
                reason="risk:order_value_exceeded",
                executed=False,
                runtime={"blocked": True},
            )
        ],
    )
    blocked_why = _assert_symbol_why_is_llm(blocked, action="BUY", executed=False)
    assert blocked_why["review_summary"] == (
        "Risk controls stopped a trade before execution."
    )
    _assert_symbol_public_surface(blocked)

    recorded = _symbol_payload(
        monkeypatch,
        [
            _decision(
                "AAPL",
                "HOLD",
                decision_source="armed_plan",
                model_called=False,
                decision_reason_code="RISK_LIMIT",
                reason="risk:gross_exposure",
                executed=False,
            )
        ],
    )
    assert recorded["why"] is None
    assert all(
        "stopped a trade" not in json.dumps(row.get("review_summary") or "")
        for row in recorded["decisions"]
    )
    _assert_symbol_public_surface(recorded)


def test_handle_symbol_durable_io_failure_fails_closed(monkeypatch) -> None:
    api = _load_desktop_bridge_api()
    monkeypatch.setattr(
        api,
        "DECISIONS_LEDGER_PATH",
        SimpleNamespace(
            is_file=lambda: True,
            open=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk gone")),
        ),
    )
    monkeypatch.setattr(api, "load_state", lambda: _symbol_state([], "AAPL"))
    payload = api.handle_symbol(SimpleNamespace(symbol="AAPL"))
    assert payload["why"] is None
    assert payload["decisions"] == []
