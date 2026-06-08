"""Pure derivation of agent tool-channel traces from decision ledger rows."""

from __future__ import annotations

from typing import Any

TOOLS = ["context_request", "indicator_watch", "next_wake", "order", "learning"]


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

    return {
        "tool": "order",
        "invoked": True,
        "args": {"action": action, "intent": row.get("intent")},
        "outcome": "executed" if row.get("executed") else "blocked",
        "detail": {"reason": row.get("reason")},
    }


def _learning_trace(row: dict) -> dict:
    if not row.get("learning"):
        return {"tool": "learning", "invoked": False}
    return {"tool": "learning", "invoked": True, "outcome": "applied"}


def summarize_tools(row: dict) -> dict:
    runtime = _runtime(row)
    context_request = runtime.get("context_request")
    rounds = (
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
    ]
    tools_used = [item["tool"] for item in trace if item["invoked"]]
    return {
        "tools_used": tools_used,
        "tools_skipped": [tool for tool in TOOLS if tool not in tools_used],
        "rounds": rounds,
        "trace": trace,
    }
