"""Pure projection of runtime decisions into durable audit rows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from trader.domain import brain_trace, decision_reason
from trader.domain.decision_identity import decision_id
from trader.support.metadata import experiment as experiment_metadata

# Additive optional fields (thesis, mandate_ref, …) stay on this version.
# Readers must tolerate a missing key on historical rows.
SCHEMA_VERSION = 1
UNKNOWN_CODE_VERSION = {
    "schema_version": 1,
    "source": "unknown",
    "git_commit": None,
    "git_commit_short": None,
    "git_branch": None,
    "git_dirty": None,
    "git_tracked_dirty": None,
    "git_dirty_files": [],
}

__all__ = [
    "SCHEMA_VERSION",
    "UNKNOWN_CODE_VERSION",
    "build_decision_row",
    "collect_session_by_symbol",
    "decision_row_brain_trace",
    "decision_row_mandate_ref",
]


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_mandate_ref(value: Any) -> dict | None:
    return dict(value) if isinstance(value, dict) else None


def decision_row_mandate_ref(row: Any) -> dict | None:
    """Read ``mandate_ref`` at root, then legacy ``decision.mandate_ref``."""
    if not isinstance(row, dict):
        return None
    found = _optional_mandate_ref(row.get("mandate_ref"))
    if found is not None:
        return found
    nested = row.get("decision")
    if isinstance(nested, dict):
        return _optional_mandate_ref(nested.get("mandate_ref"))
    return None


def decision_row_brain_trace(row: Any) -> dict | None:
    """Read the additive Brain trace block; missing on historical rows."""
    if not isinstance(row, dict):
        return None
    found = row.get("brain_trace")
    return dict(found) if isinstance(found, dict) else None


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _brain_trace_projection(
    report: dict,
    decision: dict,
    *,
    resolved_decision_id: str,
    mandate_ref: dict | None,
) -> dict[str, object]:
    nested_decision_process = _mapping(decision.get("process"))
    nested_report_process = _mapping(report.get("process"))
    task_id = brain_trace.unique_identity(
        decision.get("task_id"),
        report.get("task_id"),
        nested_decision_process.get("task_id"),
        nested_report_process.get("task_id"),
    )
    process_instance_id = brain_trace.unique_identity(
        decision.get("process_instance_id"),
        report.get("process_instance_id"),
        nested_decision_process.get("process_instance_id"),
        nested_report_process.get("process_instance_id"),
    )
    attempt_id = brain_trace.unique_identity(
        decision.get("attempt_id"),
        report.get("attempt_id"),
        nested_decision_process.get("attempt_id"),
        nested_report_process.get("attempt_id"),
    )
    observation_source = decision.get("observation_source") or report.get("observation_source")
    return {
        "episode_id": brain_trace.episode_id(
            task_id=task_id,
            process_instance_id=process_instance_id,
            decision_source=decision.get("decision_source"),
        ),
        "task_id": task_id,
        "process_instance_id": process_instance_id,
        "attempt_id": attempt_id,
        "decision_id": resolved_decision_id,
        "mandate_id": brain_trace.mandate_id_from_ref(mandate_ref),
        "observation": brain_trace.observation_ref(
            ts=decision.get("observation_ts") or report.get("observation_ts"),
            source=observation_source,
        ),
        "post_effect_snapshot": brain_trace.post_effect_snapshot_ref(
            snapshot_id=decision.get("post_effect_snapshot_id")
            or report.get("post_effect_snapshot_id")
        ),
    }


def collect_session_by_symbol(
    symbols: Iterable[str],
    *,
    snapshot: Callable[[str], Any],
) -> dict[str, dict]:
    """Copy already-computed session snapshots onto the cycle report.

    Observability only: a snapshot failure omits that symbol instead of
    raising, so the order path never depends on this collection.
    """
    collected: dict[str, dict] = {}
    for symbol in symbols:
        name = str(symbol)
        if not name:
            continue
        try:
            value = snapshot(name)
        except Exception:  # noqa: BLE001 - observability must never block an order
            continue
        if isinstance(value, dict):
            collected[name] = value
    return collected


def _session_window_fields(report: dict, symbol: str) -> dict[str, int | str | None]:
    session = _as_dict(_as_dict(report.get("session_by_symbol")).get(symbol))
    return {
        "since_open_m": _optional_int(session.get("since_open_m")),
        "to_close_m": _optional_int(session.get("to_close_m")),
        "venue": _optional_text(session.get("venue")),
    }


def _process_projection(report: dict, decision: dict, *, resolved_decision_id: str) -> dict | None:
    """Expose pilot correlation only when the runtime actually supplied it."""
    process_instance_id = _optional_text(decision.get("process_instance_id") or report.get("process_instance_id"))
    attempt_id = _optional_text(decision.get("attempt_id") or report.get("attempt_id"))
    runtime_run_id = _optional_text(decision.get("runtime_run_id") or report.get("runtime_run_id"))
    governance_version = _as_dict(decision.get("governance_version")) or _as_dict(report.get("governance_version"))
    if process_instance_id is None and attempt_id is None and runtime_run_id is None and not governance_version:
        return None
    return {
        "process_instance_id": process_instance_id,
        "attempt_id": attempt_id,
        "runtime_run_id": runtime_run_id,
        "decision_id": resolved_decision_id,
        "governance_version": governance_version or None,
    }


def build_decision_row(
    report: dict,
    decision: dict,
    *,
    sequence: int,
    source: str = "daemon",
) -> dict:
    """Project one runtime decision into the versioned audit schema."""

    cycle_ts = str(report.get("ts") or decision.get("ts") or "")
    symbol = str(decision.get("symbol") or "")
    if not cycle_ts or not symbol:
        raise ValueError("decision row requires report ts and decision symbol")

    prices = _as_dict(report.get("prices"))
    stale_market_data = _as_dict(report.get("stale_market_data"))
    price = _optional_float(decision.get("price"))
    if price is None:
        price = _optional_float(prices.get(symbol))

    original_decision = dict(decision)
    reason_code = decision_reason.infer_reason_code({**decision, "decision": original_decision})
    code_version = (
        _as_dict(decision.get("code_version")) or _as_dict(report.get("code_version")) or dict(UNKNOWN_CODE_VERSION)
    )
    resolved_experiment = experiment_metadata.inherited_experiment(
        decision.get("experiment")
    ) or experiment_metadata.decision_experiment(
        _as_dict(report.get("experiment_context")),
        provider=decision.get("llm_provider"),
        model=decision.get("llm_model"),
    )
    indicator_watch = _as_dict(decision.get("indicator_watch"))
    trade_evaluation = _as_dict(decision.get("trade_plan_evaluation"))
    trade_economics = _as_dict(trade_evaluation.get("economics"))
    resolved_decision_id = str(decision.get("decision_id") or decision_id(cycle_ts, sequence, symbol))
    row = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": resolved_decision_id,
        "cycle_ts": cycle_ts,
        "sequence": sequence,
        "source": source,
        "code_version": code_version,
        "experiment_id": resolved_experiment["experiment_id"],
        "experiment_components": resolved_experiment["components"],
        "experiment_status": {
            "schema_version": resolved_experiment["schema_version"],
            "decision_grade": resolved_experiment["decision_grade"],
            "issues": resolved_experiment["issues"],
        },
        "symbol": symbol,
        "action": decision.get("action"),
        "intent": decision.get("intent"),
        "qty": _optional_float(decision.get("qty")),
        "confidence": _optional_float(decision.get("confidence")),
        "rationale": decision.get("rationale"),
        "opportunity_side": decision.get("opportunity_side"),
        "next_wake_in_minutes": _optional_float(decision.get("next_wake_in_minutes")),
        "executed": decision.get("executed"),
        "reason": decision.get("reason"),
        "context": decision.get("context"),
        "decision_reason_code": reason_code,
        "decision_source": decision.get("decision_source"),
        "model_called": _optional_bool(decision.get("model_called")),
        "price": price,
        "llm_provider": decision.get("llm_provider"),
        "llm_model": decision.get("llm_model"),
        "llm_fallback_reason": decision.get("llm_fallback_reason"),
        "llm_error": decision.get("llm_error"),
        "learning": decision.get("learning"),
        "applied_learning_ids": [
            rule_id for rule_id in _as_list(decision.get("applied_learning_ids")) if isinstance(rule_id, str)
        ],
        "thesis": decision.get("thesis") if isinstance(decision.get("thesis"), dict) else None,
        "entry_dimensions": (
            dict(decision["entry_dimensions"])
            if isinstance(decision.get("entry_dimensions"), dict)
            else None
        ),
        "trade_evaluation_id": _optional_text(
            decision.get("trade_evaluation_id")
            or trade_evaluation.get("evaluation_id")
        ),
        "trade_evaluation_as_of": _optional_text(trade_evaluation.get("as_of")),
        "trade_economics_status": _optional_text(trade_economics.get("status")),
        "trade_evaluation_rejection": _optional_text(
            decision.get("trade_evaluation_rejection")
        ),
        "trade_plan_evaluation": trade_evaluation or None,
        "mandate_ref": _optional_mandate_ref(decision.get("mandate_ref")),
        "decision": original_decision,
        "market_snapshot": {
            "price": price,
            "stale_market_data": stale_market_data.get(symbol),
            "symbols_due": _as_list(report.get("symbols_due")),
            "model_calls_used": report.get("model_calls_used"),
            **_session_window_fields(report, symbol),
        },
        "portfolio_snapshot": _as_dict(report.get("portfolio")),
        "runtime": {
            "dry_run": report.get("dry_run"),
            "trade_plan_created": decision.get("trade_plan_created"),
            "indicator_watch_created": decision.get("indicator_watch_created"),
            "indicator_watch_requested": decision.get("indicator_watch_requested"),
            "indicator_watch_rejections": decision.get("indicator_watch_rejections"),
            "indicator_watch_order": indicator_watch.get("order"),
            "armed_plan_id": decision.get("armed_plan_id"),
            "armed_plan_order": decision.get("armed_plan_order"),
            "context_request": decision.get("context_request"),
            "next_wake_requested": decision.get("next_wake_requested"),
            "next_wake_event": decision.get("next_wake_event"),
            "next_wake_event_iso": decision.get("next_wake_event_iso"),
            "exit_plan": decision.get("exit_plan"),
            "exit_plan_trace": decision.get("exit_plan_trace"),
            "exit_plan_warnings": decision.get("exit_plan_warnings"),
            "risk_pct": decision.get("risk_pct"),
            "risk_pct_target": decision.get("risk_pct_target"),
            "risk_qty_derived": decision.get("risk_qty_derived"),
            "stop_distance": decision.get("stop_distance"),
            "risk_clamped": decision.get("risk_clamped"),
            "risk_unbounded_no_stop": decision.get("risk_unbounded_no_stop"),
            "risk_warnings": decision.get("risk_warnings"),
            "exit_update": bool(decision.get("exit_update")),
            "exit_update_applied": decision.get("exit_update_applied"),
            "exit_update_reason": decision.get("exit_update_reason"),
            "exit_update_trace": decision.get("exit_update_trace"),
            "exit_update_warnings": decision.get("exit_update_warnings"),
            "data_source": decision.get("data_source"),
            "tool_rounds": decision.get("tool_rounds"),
            "tool_calls": decision.get("tool_calls"),
            "tool_normalizations": decision.get("tool_normalizations"),
            "cancel_watch_results": decision.get("cancel_watch_results"),
            "trade_evaluation_id": decision.get("trade_evaluation_id"),
            "trade_plan_evaluation": trade_evaluation or None,
            "trade_evaluation_rejection": decision.get(
                "trade_evaluation_rejection"
            ),
        },
        "news": _as_dict(decision.get("news")),
        "labels": {},
        "brain_trace": _brain_trace_projection(
            report,
            decision,
            resolved_decision_id=resolved_decision_id,
            mandate_ref=_optional_mandate_ref(decision.get("mandate_ref")),
        ),
    }
    process = _process_projection(report, decision, resolved_decision_id=resolved_decision_id)
    if process is not None:
        row["process"] = process
    return row
