from __future__ import annotations

from trader.runtime import daemon


_DEAD_DAEMON_PROXY_NAMES = {
    "_active_watch_summaries_by_symbol",
    "_apply_decision_schedule",
    "_apply_exit_update",
    "_apply_planned_exits",
    "_bar_ts_after_plan_open",
    "_batch_decide",
    "_bounded_wake_minutes",
    "_build_execution_eligibility",
    "_clamp_exit_quantity",
    "_cockpit_vol_fraction",
    "_context_request_summary",
    "_counts_as_llm_review",
    "_earliest_active_watch_expiry_iso",
    "_ensure_default_wake",
    "_execution_blocked_reason",
    "_exit_watch_cooldown_elapsed",
    "_feature_vol_fraction",
    "_fetch_5m_bars_for_open_plans",
    "_finite_positive",
    "_gross_exposure",
    "_hard_stop_price",
    "_hard_stop_wrong_side",
    "_invalid_intent_reason",
    "_is_valid_5m_bar",
    "_last_review_by_symbol",
    "_llm_exit_reason_for_intent",
    "_llm_review_verdict",
    "_merge_gate_feedback",
    "_persist_last_llm_review",
    "_plan_snapshot",
    "_positive_finite_float",
    "_reference_volatility_for_symbol",
    "_resolve_position_aware_decision",
    "_resolve_wake_event",
    "_risk_capacity_context",
    "_runtime_tool_audit_fields",
    "_run_tool_round",
    "_scan_exit_watches",
    "_scan_indicator_watches",
    "_select_due_symbols",
    "_side_capacity_usd",
    "_stale_backoff_wake_minutes",
}


def test_daemon_does_not_reexport_dead_application_service_proxies() -> None:
    leaked = sorted(name for name in _DEAD_DAEMON_PROXY_NAMES if hasattr(daemon, name))

    assert leaked == []
