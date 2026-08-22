"""Pure decision ledger filtering and grouping helpers."""

from __future__ import annotations


def _safe_str(value: object) -> str:
    return str(value or "")


def is_risk_row(row: dict) -> bool:
    reason = _safe_str(row.get("reason"))
    return reason.startswith("risk:") or reason.startswith("blocked_")


def is_stale_row(state: dict, row: dict) -> bool:  # noqa: ARG001 - stable signature
    """Stale is a property of the decision reason, not current market state."""
    return _safe_str(row.get("reason")).startswith("stale")


def is_batch_row(row: dict) -> bool:
    """Quiet infra HOLD row without an LLM call, eligible for batch grouping."""
    return (
        _safe_str(row.get("decision_source")) in ("infra", "infra_hold")
        and not row.get("model_called", True)
        and _safe_str(row.get("reason")) == "quiet_gate"
    )


def filter_rows(rows: list[dict], active_filter: str, state: dict) -> list[dict]:
    if active_filter == "buy":
        return [row for row in rows if _safe_str(row.get("action")).upper() == "BUY"]
    if active_filter == "sell":
        return [row for row in rows if _safe_str(row.get("action")).upper() == "SELL"]
    if active_filter == "hold":
        return [row for row in rows if _safe_str(row.get("action")).upper() == "HOLD"]
    if active_filter == "risk":
        return [row for row in rows if is_risk_row(row)]
    if active_filter == "stale":
        return [row for row in rows if is_stale_row(state, row)]
    return rows


def count_filters(rows: list[dict], state: dict) -> dict[str, int]:
    return {
        "all": len(rows),
        "buy": sum(1 for row in rows if _safe_str(row.get("action")).upper() == "BUY"),
        "sell": sum(1 for row in rows if _safe_str(row.get("action")).upper() == "SELL"),
        "hold": sum(1 for row in rows if _safe_str(row.get("action")).upper() == "HOLD"),
        "risk": sum(1 for row in rows if is_risk_row(row)),
        "stale": sum(1 for row in rows if is_stale_row(state, row)),
    }


def group_into_ledger_rows(rows: list[dict]) -> list[dict]:
    """Group consecutive infra hold rows from one cycle into one summary row."""
    if not rows:
        return []

    result: list[dict] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if not is_batch_row(row):
            result.append(row)
            i += 1
            continue

        cycle_ts = row.get("cycle_ts") or row.get("ts", "")
        batch: list[dict] = [row]
        j = i + 1
        while j < len(rows):
            next_row = rows[j]
            next_ts = next_row.get("cycle_ts") or next_row.get("ts", "")
            if is_batch_row(next_row) and next_ts == cycle_ts:
                batch.append(next_row)
                j += 1
            else:
                break

        if len(batch) >= 3:
            llm_n = sum(1 for batch_row in batch if batch_row.get("model_called", False))
            quiet_n = len(batch) - llm_n
            result.append(
                {
                    "_is_batch_summary": True,
                    "summary_kind": "automatic_cycle",
                    "cycle_ts": cycle_ts,
                    "symbol": "— batch",
                    "action": "HOLD",
                    "decision_source": "infra",
                    "model_called": False,
                    "reason": (
                        f"{len(batch)} due — {llm_n} LLM calls, {quiet_n} quiet holds"
                    ),
                }
            )
        else:
            result.extend(batch)
        i = j

    return result


# Historical private names remain import-compatible while consumers migrate.
_count_filters = count_filters
_filter_rows = filter_rows
_group_into_ledger_rows = group_into_ledger_rows
_is_batch_row = is_batch_row
_is_risk_row = is_risk_row
_is_stale_row = is_stale_row


__all__ = [
    "count_filters",
    "filter_rows",
    "group_into_ledger_rows",
    "is_batch_row",
    "is_risk_row",
    "is_stale_row",
]
