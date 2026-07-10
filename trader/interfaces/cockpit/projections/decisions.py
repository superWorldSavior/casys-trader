"""Pure projections for the cockpit decision ledger."""

from __future__ import annotations

from dataclasses import dataclass

from trader.interfaces.cockpit import format as f
from trader.reporting.read_models.decision_filters import (
    count_filters,
    filter_rows,
    group_into_ledger_rows,
)
from trader.support.coercion import dict_list, finite_float


@dataclass(frozen=True)
class DecisionLedgerProjection:
    all_rows: list[dict]
    filtered_rows: list[dict]
    grouped_rows: list[dict]
    filter_counts: dict[str, int]


@dataclass(frozen=True)
class DecisionLedgerRow:
    row_key: str
    source_row: dict
    is_batch: bool
    utc_text: str
    symbol: str
    action: str
    action_text: str
    confidence_text: str
    source_text: str
    effect_text: str
    effect_kind: str
    has_detail: bool


def format_confidence(confidence: object) -> str:
    """Format a normalized confidence for the compact ledger column."""

    value = finite_float(confidence, default=None)
    if value is None:
        return "—"
    if value >= 1.0:
        return "1.0"
    return f"{value:.2f}".lstrip("0") or ".00"


def has_decision_detail(row: dict) -> bool:
    """Return whether a ledger row exposes rationale or tool-call detail."""

    if str(row.get("rationale") or "").strip():
        return True
    runtime = f.safe_dict(row.get("runtime"))
    decision = f.safe_dict(row.get("decision"))
    return bool(dict_list(runtime.get("tool_calls") or decision.get("tool_calls")))


def project_decision_ledger(
    state: dict,
    active_filter: str,
) -> DecisionLedgerProjection:
    """Filter, count and group the current decision ledger once per refresh."""

    all_rows = dict_list(state.get("recent_decisions"))
    filtered_rows = filter_rows(all_rows, active_filter, state)
    return DecisionLedgerProjection(
        all_rows=all_rows,
        filtered_rows=filtered_rows,
        grouped_rows=group_into_ledger_rows(filtered_rows),
        filter_counts=count_filters(all_rows, state),
    )


def build_ledger_rows(rows: list[dict]) -> list[DecisionLedgerRow]:
    """Project raw/grouped decisions into semantic, presentation-ready cells."""

    projected: list[DecisionLedgerRow] = []
    previous_date: str | None = None
    for index, row in enumerate(rows):
        is_batch = bool(row.get("_is_batch_summary"))
        cycle_timestamp = row.get("cycle_ts") or row.get("ts")
        parsed = f.parse_ts(cycle_timestamp)
        if parsed:
            date_text = parsed.strftime("%Y-%m-%d")
            time_text = parsed.strftime("%H:%M")
            utc_text = (
                f"{parsed.strftime('%a')} {time_text}"
                if previous_date and date_text != previous_date
                else time_text
            )
            previous_date = date_text
        else:
            utc_text = "—"

        symbol = str(row.get("symbol") or "—")
        action = str(row.get("action") or "").upper()
        if is_batch:
            action_text = "···"
            confidence_text = ""
            source_text = "heuristic"
            effect_text = str(row.get("reason") or "")
            effect_kind = "batch"
            detail_available = False
        else:
            action_text = action or "—"
            confidence_text = format_confidence(row.get("confidence"))
            source_text = f.decision_source_label(row) or "—"
            effect, effect_kind = f.decision_effect(row)
            detail_available = has_decision_detail(row)
            clipped_effect = f.clip(effect, limit=44)
            effect_text = (
                f"{clipped_effect} ▾" if detail_available else clipped_effect
            )

        projected.append(
            DecisionLedgerRow(
                row_key=f"{cycle_timestamp or ''}|{symbol}|{index}",
                source_row=row,
                is_batch=is_batch,
                utc_text=utc_text,
                symbol=symbol,
                action=action,
                action_text=action_text,
                confidence_text=confidence_text,
                source_text=source_text,
                effect_text=effect_text,
                effect_kind=effect_kind,
                has_detail=detail_available,
            )
        )
    return projected


__all__ = [
    "DecisionLedgerProjection",
    "DecisionLedgerRow",
    "build_ledger_rows",
    "format_confidence",
    "has_decision_detail",
    "project_decision_ledger",
]
