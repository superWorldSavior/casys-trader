"""Pure derivation of agent tool-channel traces from decision ledger rows."""

from __future__ import annotations

from typing import Any

from trader.application import tool_outcomes

TOOLS = ["context_request", "indicator_watch", "next_wake", "order", "learning"]
_ACTION_TOOLS = tool_outcomes.ACTION_TOOLS
finalize_action_tool_outcomes = tool_outcomes.finalize_action_tool_outcomes


def _runtime(row: dict) -> dict:
    runtime = row.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _context_request_trace(runtime: dict) -> dict:
    context_request = runtime.get("context_request")
    if context_request is None:
        return {"tool": "context_request", "invoked": False}

    data = context_request if isinstance(context_request, dict) else {}
    resolved = _as_int(data.get("resolved"))
    rounds = _as_int(data.get("rounds"))
    return {
        "tool": "context_request",
        "invoked": True,
        "args": {
            "requested": data.get("requested", []),
            "rounds": rounds,
        },
        "outcome": "resolved" if resolved > 0 else "noop",
        "detail": {"resolved": resolved},
    }


def _indicator_watch_trace(runtime: dict) -> dict:
    if not runtime.get("indicator_watch_requested"):
        return {"tool": "indicator_watch", "invoked": False}

    trace = {
        "tool": "indicator_watch",
        "invoked": True,
        "outcome": "created" if runtime.get("indicator_watch_created") else "rejected",
    }
    if runtime.get("indicator_watch_rejections") is not None:
        trace["detail"] = {"rejections": runtime.get("indicator_watch_rejections")}
    return trace


def _next_wake_trace(row: dict, runtime: dict) -> dict:
    requested = runtime.get("next_wake_requested")
    if requested is None:
        return {"tool": "next_wake", "invoked": False}

    effective = row.get("next_wake_in_minutes")
    return {
        "tool": "next_wake",
        "invoked": True,
        "args": {"requested_minutes": requested},
        "outcome": "applied" if effective == requested else "clamped",
        "detail": {"effective_minutes": effective},
    }


def _order_trace(row: dict) -> dict:
    action = row.get("action")
    if action not in {"BUY", "SELL"}:
        return {"tool": "order", "invoked": False}

    runtime = _runtime(row)
    detail = {"reason": row.get("reason")}
    for field in (
        "risk_pct",
        "stop_distance",
        "risk_clamped",
        "risk_unbounded_no_stop",
        "risk_warnings",
        "exit_plan_warnings",
    ):
        if field in runtime:
            detail[field] = runtime.get(field)

    return {
        "tool": "order",
        "invoked": True,
        "args": {"action": action, "intent": row.get("intent")},
        "outcome": "executed" if row.get("executed") else "blocked",
        "detail": detail,
    }


def _learning_trace(row: dict) -> dict:
    if not row.get("learning"):
        return {"tool": "learning", "invoked": False}
    return {"tool": "learning", "invoked": True, "outcome": "applied"}


def _cancel_watch_trace(runtime: dict) -> dict:
    """Résultat réel des annulations de veilles demandées par l'agent.

    Le daemon vérifie l'ownership (un symbole n'annule que SES veilles) et n'émet
    sinon qu'un event `watch_cancel_rejected` : sans cette pseudo-trace, le refus
    d'annulation resterait invisible dans l'audit des outils (cf. design §7.2/§8).
    """
    results = runtime.get("cancel_watch_results")
    if not results:
        return {"tool": "cancel_watch", "invoked": False}
    rejected = any(
        isinstance(r, dict) and r.get("outcome") == "not_owned" for r in results
    )
    return {
        "tool": "cancel_watch",
        "invoked": True,
        "outcome": "rejected" if rejected else "cancelled",
        "detail": {"results": results},
    }


def _domain_tool_traces(runtime: dict) -> list[dict]:
    """Traces des domain tools V0 persistées dans runtime.tool_calls."""
    calls = runtime.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [
        {
            "tool": str(call.get("tool") or "?"),
            "invoked": True,
            "args": call.get("args") or {},
            "outcome": call.get("outcome"),
            "detail": call.get("detail") or {},
        }
        for call in calls
        if isinstance(call, dict)
    ]


def _action_tool_outcome(tool: str, entry: dict) -> str | None:
    return tool_outcomes.action_tool_outcome(tool, entry)


def summarize_tools(row: dict) -> dict:
    runtime = _runtime(row)
    context_request = runtime.get("context_request")
    context_rounds = (
        _as_int(context_request.get("rounds"))
        if isinstance(context_request, dict)
        else 0
    )
    trace = [
        _context_request_trace(runtime),
        _indicator_watch_trace(runtime),
        _next_wake_trace(row, runtime),
        _order_trace(row),
        _learning_trace(row),
        _cancel_watch_trace(runtime),
        *_domain_tool_traces(runtime),
    ]
    tools_used = [item["tool"] for item in trace if item["invoked"]]
    return {
        "tools_used": tools_used,
        # compat : le taux d'usage legacy reste calculé sur les 5 pseudo-tools
        "tools_skipped": [tool for tool in TOOLS if tool not in tools_used],
        "rounds": max(context_rounds, _as_int(runtime.get("tool_rounds"))),
        "trace": trace,
    }
