"""Finalize per-symbol action tool outcomes before decision persistence."""

from __future__ import annotations

ACTION_TOOLS = frozenset(
    {
        "propose_order",
        "set_next_wake",
        "propose_indicator_watch",
        "record_learning",
        "cancel_watch",
        "amend_exit",
    }
)


def action_tool_outcome(tool: str, entry: dict) -> str | None:
    """Return the final persisted outcome for one action-tool call."""
    if tool == "propose_order":
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
    if tool == "amend_exit":
        if entry.get("amend_exit_applied"):
            return "applied"
        reason = str(entry.get("amend_exit_reason") or "")
        if reason.startswith("resolve_failed"):
            return "rejected"
        return "noop"
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
            finalized.append({**call, "outcome": action_tool_outcome(call["tool"], entry)})
        else:
            finalized.append(call)
    return finalized
