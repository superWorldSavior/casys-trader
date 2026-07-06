"""Finalize per-symbol action tool outcomes before decision persistence."""

from __future__ import annotations

ACTION_TOOLS = frozenset(
    {
        "strategy_entry",
        "strategy_exit",
        "strategy_close",
        "set_next_wake",
        "propose_indicator_watch",
        "record_learning",
        "cancel_watch",
    }
)


def action_tool_outcome(tool: str, entry: dict) -> str | None:
    """Return the final persisted outcome for one action-tool call."""
    if tool in {"strategy_entry", "strategy_close"}:
        return "executed" if entry.get("executed") else "blocked"
    if tool == "set_next_wake":
        return "applied" if entry.get("next_wake_in_minutes") == entry.get("next_wake_requested") else "clamped"
    if tool == "propose_indicator_watch":
        return "created" if entry.get("indicator_watch_created") else "rejected"
    if tool == "record_learning":
        return "applied"
    if tool == "cancel_watch":
        results = entry.get("cancel_watch_results")
        if not results:
            return "noop"
        rejected = any(isinstance(result, dict) and result.get("outcome") == "not_owned" for result in results)
        return "rejected" if rejected else "cancelled"
    if tool == "strategy_exit":
        if entry.get("exit_update_applied"):
            return "applied"
        reason = str(entry.get("exit_update_reason") or "")
        if reason.startswith("resolve_failed"):
            return "rejected"
        return "noop"
    return None


def action_tool_detail(tool: str, entry: dict, call: dict) -> dict | None:
    """Return final persisted detail for one action-tool call."""
    if tool in {"strategy_entry", "strategy_close"}:
        warnings = []
        for field in ("risk_warnings", "exit_plan_warnings"):
            value = entry.get(field)
            if isinstance(value, list):
                warnings.extend(value)
        if not warnings:
            return None
        detail = dict(call.get("detail") or {}) if isinstance(call.get("detail"), dict) else {}
        detail["warnings"] = warnings
        return detail
    if tool == "strategy_exit":
        detail = dict(call.get("detail") or {}) if isinstance(call.get("detail"), dict) else {}
        reason = entry.get("exit_update_reason")
        if reason:
            detail["reason"] = reason
        if entry.get("exit_update_applied") is not None:
            detail["exit_update_applied"] = entry["exit_update_applied"]
        if entry.get("exit_update_warnings"):
            detail["warnings"] = entry["exit_update_warnings"]
        return detail or None
    return None


def finalize_action_tool_outcomes(entry: dict) -> list[dict] | None:
    """Rewrite final action-tool outcomes with actual decision results.

    Read-only domain tools keep their original outcome because recall and tool-round
    bookkeeping depend on those raw results.
    """
    calls = entry.get("tool_calls")
    if not isinstance(calls, list):
        return calls
    finalized: list[dict] = []
    for call in calls:
        if isinstance(call, dict) and call.get("tool") in ACTION_TOOLS:
            tool = call["tool"]
            updated = {**call, "outcome": action_tool_outcome(tool, entry)}
            detail = action_tool_detail(tool, entry, call)
            if detail is not None:
                updated["detail"] = detail
            finalized.append(updated)
        else:
            finalized.append(call)
    return finalized
