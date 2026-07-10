"""Pure projections for cockpit exit plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trader.interfaces.cockpit import format as f
from trader.support.coercion import dict_list, finite_float

ReviewKind = Literal["rejected", "reviewed", "none"]


@dataclass(frozen=True)
class ExitPlanRow:
    """Semantic data shared by the full and compact exit-plan renderers."""

    symbol: str
    side: str
    quantity: float | None
    entry_price: float | None
    stop_price: float | None
    stop_left_pct: float | None
    stop_entry_risk_pct: float | None
    take_profit_label: str
    protect_label: str
    review_label: str
    review_kind: ReviewKind
    exit_update_rejected: bool
    stop_is_near: bool


@dataclass(frozen=True)
class ExitPlansProjection:
    """Ordered exit-plan rows and the latest rejected-update symbol set."""

    rows: list[ExitPlanRow]
    rejected_symbols: frozenset[str]


def format_price(value: float | None) -> str:
    """Format a price with no decimals when round, otherwise two decimals."""

    if value is None:
        return "—"
    if value == round(value):
        return f.fmt_compact(value, decimals=0)
    return f.fmt_compact(value, decimals=2)


def stop_pct(plan: dict, reference: float | None) -> float | None:
    """Return the signed raw stop position relative to a reference price."""

    return f.stop_distance_pct(plan, reference)


def take_profit_label(plan: dict) -> str:
    """Return the first one or two take-profit prices as a compact label."""

    prices = [
        price
        for take_profit in dict_list(plan.get("take_profits"))
        if (price := finite_float(take_profit.get("price"), default=None))
        is not None
    ]
    if not prices:
        return "—"
    if len(prices) == 1:
        return format_price(prices[0])
    return f"{format_price(prices[0])} → {format_price(prices[1])}"


def exit_update_rejected_symbols(state: dict) -> set[str]:
    """Return symbols whose latest recorded strategy-exit update was rejected."""

    latest: dict[str, tuple[str, str]] = {}
    for row in dict_list(state.get("recent_decisions")):
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        cycle_timestamp = str(row.get("cycle_ts") or row.get("ts") or "")
        runtime = f.safe_dict(row.get("runtime"))
        for call in dict_list(runtime.get("tool_calls")):
            if str(call.get("tool") or "") != "strategy_exit":
                continue
            outcome = str(call.get("outcome") or "")
            existing = latest.get(symbol)
            if existing is None or cycle_timestamp >= existing[0]:
                latest[symbol] = (cycle_timestamp, outcome)
    return {
        symbol
        for symbol, (_, outcome) in latest.items()
        if outcome == "rejected"
    }


def reviewed_status(
    plan: dict,
    rejected_symbols: set[str] | frozenset[str],
) -> tuple[str, ReviewKind]:
    """Return the review label and semantic status for one exit plan."""

    symbol = str(plan.get("symbol") or "")
    if symbol in rejected_symbols:
        return "▲ exit update rejected", "rejected"
    review = f.safe_dict(plan.get("last_llm_review"))
    timestamp = review.get("ts")
    if timestamp:
        return f"✓ {f.hhmm(timestamp)}", "reviewed"
    return "—", "none"


def stop_distance_sort_key(plan: dict, state: dict) -> float:
    """Sort nearest stops first and plans without a resolved stop last."""

    symbol = str(plan.get("symbol") or "")
    reference = f.price_for_symbol(state, symbol) or finite_float(
        plan.get("entry_price"),
        default=None,
    )
    distance = stop_pct(plan, reference)
    return abs(distance) if distance is not None else 999.0


def project_exit_plans(state: dict) -> ExitPlansProjection:
    """Build the ordered semantic exit-plan projection once per refresh."""

    plans = sorted(
        dict_list(state.get("trade_plans")),
        key=lambda plan: stop_distance_sort_key(plan, state),
    )
    rejected_symbols = frozenset(exit_update_rejected_symbols(state))
    rows: list[ExitPlanRow] = []

    for plan in plans:
        symbol = str(plan.get("symbol") or "—")
        side = str(plan.get("side") or "LONG").upper()
        side_char = "S" if side == "SHORT" else "L"
        quantity = finite_float(
            plan.get("remaining_quantity") or plan.get("quantity"),
            default=None,
        )
        entry_price = finite_float(plan.get("entry_price"), default=None)
        reference = f.price_for_symbol(state, symbol) or entry_price
        stop_left = f.stop_left_pct(plan, reference)
        review_label, review_kind = reviewed_status(plan, rejected_symbols)

        rows.append(
            ExitPlanRow(
                symbol=symbol,
                side=side_char,
                quantity=quantity,
                entry_price=entry_price,
                stop_price=finite_float(
                    plan.get("hard_stop_price"),
                    default=None,
                ),
                stop_left_pct=stop_left,
                stop_entry_risk_pct=f.stop_entry_risk_pct(plan),
                take_profit_label=take_profit_label(plan),
                protect_label=f.protect_label(plan),
                review_label=review_label,
                review_kind=review_kind,
                exit_update_rejected=symbol in rejected_symbols,
                stop_is_near=stop_left is not None and stop_left <= 3.0,
            )
        )

    return ExitPlansProjection(rows=rows, rejected_symbols=rejected_symbols)


__all__ = [
    "ExitPlanRow",
    "ExitPlansProjection",
    "ReviewKind",
    "exit_update_rejected_symbols",
    "format_price",
    "project_exit_plans",
    "reviewed_status",
    "stop_distance_sort_key",
    "stop_pct",
    "take_profit_label",
]
