"""Read model for non-blocking and blocking decision flags."""

from __future__ import annotations

_WARNING_FIELDS = (
    "field",
    "risk_qty",
    "max_qty",
    "risk_pct",
    "limit",
    "distance",
    "distance_pct",
    "limit_distance",
    "limit_pct",
    "context",
)


def _runtime(row: dict) -> dict:
    runtime = row.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _default_order_outcome(row: dict) -> str:
    return "executed" if row.get("executed") else "blocked"


def _decision_context(row: dict) -> object | None:
    if row.get("context") is not None:
        return row.get("context")
    decision = row.get("decision")
    if isinstance(decision, dict):
        return decision.get("context")
    return None


def _reason_flag_code(reason: object) -> str | None:
    if reason is None:
        return None
    text = str(reason)
    if text in {"", "ok", "hold"}:
        return None
    return text.split(":", 1)[1] if ":" in text else text


def _flag_tool_for_row(row: dict) -> str:
    return "propose_order" if row.get("action") in {"BUY", "SELL"} else "decision"


def _flag_key(flag: dict) -> tuple[object, ...]:
    return (
        flag.get("decision_id"),
        flag.get("tool"),
        flag.get("outcome"),
        flag.get("code"),
        flag.get("field"),
        flag.get("context"),
    )


def _add_warning(
    flags: list[dict],
    seen: set[tuple[object, ...]],
    *,
    row: dict,
    tool: str,
    outcome: object,
    source: str,
    warning: object,
) -> None:
    if not isinstance(warning, dict):
        return
    code = warning.get("code")
    if code is None:
        return
    flag = {
        "decision_id": row.get("decision_id"),
        "cycle_ts": row.get("cycle_ts"),
        "symbol": row.get("symbol"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "executed": row.get("executed"),
        "reason": row.get("reason"),
        "tool": tool,
        "outcome": outcome,
        "source": source,
        "code": code,
    }
    for field in _WARNING_FIELDS:
        if warning.get(field) is not None:
            flag[field] = warning[field]
    key = _flag_key(flag)
    if key in seen:
        return
    seen.add(key)
    flags.append(flag)


def extract_flags(row: dict) -> list[dict]:
    """Return normalized flags for one decision ledger row."""
    runtime = _runtime(row)
    flags: list[dict] = []
    seen: set[tuple[object, ...]] = set()

    for call in runtime.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        detail = call.get("detail")
        if not isinstance(detail, dict):
            continue
        for warning in detail.get("warnings") or []:
            _add_warning(
                flags,
                seen,
                row=row,
                tool=str(call.get("tool") or "tool"),
                outcome=call.get("outcome"),
                source="tool_call",
                warning=warning,
            )

    for warning in runtime.get("risk_warnings") or []:
        _add_warning(
            flags,
            seen,
            row=row,
            tool="propose_order",
            outcome=_default_order_outcome(row),
            source="risk_warnings",
            warning=warning,
        )

    for warning in runtime.get("exit_plan_warnings") or []:
        _add_warning(
            flags,
            seen,
            row=row,
            tool="propose_order",
            outcome=_default_order_outcome(row),
            source="exit_plan_warnings",
            warning=warning,
        )

    if not row.get("executed"):
        code = _reason_flag_code(row.get("reason"))
        if code is not None and row.get("action") in {"BUY", "SELL"}:
            _add_warning(
                flags,
                seen,
                row=row,
                tool=_flag_tool_for_row(row),
                outcome="blocked",
                source="decision_reason",
                warning={"code": code, "context": _decision_context(row)},
            )

    return flags


def collect_flags(
    rows: list[dict],
    *,
    symbol: str | None = None,
    code: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Collect flags from ledger rows, preserving ledger order then applying a tail limit."""
    flags: list[dict] = []
    for row in rows:
        if symbol is not None and row.get("symbol") != symbol:
            continue
        for flag in extract_flags(row):
            if code is not None and flag.get("code") != code:
                continue
            flags.append(flag)
    if limit is not None and limit >= 0:
        return flags[-limit:] if limit > 0 else []
    return flags
