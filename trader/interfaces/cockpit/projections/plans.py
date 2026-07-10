"""Pure projections for cockpit exit plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import armed_watches, plain_watches
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


@dataclass(frozen=True)
class ArmedOrderRow:
    """Semantic data for one armed execute-order watch."""

    symbol: str
    action: str
    quantity: float | None
    conditions: tuple[str, ...]
    logic: str
    extra_condition_count: int
    stop_price: float | None
    stop_distance_pct: float | None
    countdown: str


@dataclass(frozen=True)
class ArmedOrdersProjection:
    rows: list[ArmedOrderRow]


@dataclass(frozen=True)
class ActiveWatchRow:
    symbol: str
    condition_label: str
    ttl_fraction: float
    countdown: str


@dataclass(frozen=True)
class ActiveWatchesProjection:
    rows: list[ActiveWatchRow]


@dataclass(frozen=True)
class ExitWatchRow:
    symbol: str
    condition_label: str
    countdown: str


@dataclass(frozen=True)
class ExitWatchesProjection:
    rows: list[ExitWatchRow]


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


def project_armed_orders(
    state: dict,
    *,
    now: datetime,
) -> ArmedOrdersProjection:
    """Project armed execute-order watches without presentation objects."""

    rows: list[ArmedOrderRow] = []
    for watch in armed_watches(state):
        symbol = str(watch.get("symbol") or "—")
        order = f.safe_dict(watch.get("order"))
        action = str(
            order.get("action") or order.get("intent") or "ORDER"
        ).upper()
        conditions = dict_list(watch.get("conditions"))
        stop_price = f.armed_stop_price(watch)
        reference = f.price_for_symbol(state, symbol)
        stop_distance = (
            (stop_price - reference) / reference * 100.0
            if stop_price is not None and reference
            else None
        )
        condition_labels = tuple(
            f"{condition.get('indicator') or '?'} "
            f"{condition.get('op') or '?'} "
            f"{condition.get('value')} "
            f"@{condition.get('timeframe') or condition.get('interval') or '?'}"
            for condition in conditions[:3]
        )
        rows.append(
            ArmedOrderRow(
                symbol=symbol,
                action=action,
                quantity=finite_float(order.get("qty"), default=None),
                conditions=condition_labels,
                logic=str(watch.get("logic") or "and"),
                extra_condition_count=max(0, len(conditions) - 3),
                stop_price=stop_price,
                stop_distance_pct=stop_distance,
                countdown=f.countdown(watch.get("expires_at"), now=now),
            )
        )
    return ArmedOrdersProjection(rows=rows)


def project_active_watches(
    state: dict,
    *,
    now: datetime,
) -> ActiveWatchesProjection:
    """Project non-armed watches that still have a valid future TTL."""

    rows: list[ActiveWatchRow] = []
    for watch in plain_watches(state):
        expires_at = f.parse_ts(watch.get("expires_at"))
        if expires_at is None or expires_at <= now:
            continue
        rows.append(
            ActiveWatchRow(
                symbol=str(watch.get("symbol") or "—"),
                condition_label=f.condition_summary(
                    watch.get("conditions"),
                    watch.get("logic"),
                    max_items=2,
                    limit=32,
                ),
                ttl_fraction=f.ttl_fraction(watch, now=now),
                countdown=f.countdown(expires_at, now=now),
            )
        )
    return ActiveWatchesProjection(rows=rows)


def project_exit_watches(
    state: dict,
    *,
    now: datetime,
) -> ExitWatchesProjection:
    """Project non-expired exit watches embedded in trade plans."""

    rows: list[ExitWatchRow] = []
    for plan in dict_list(state.get("trade_plans")):
        exit_watch = f.safe_dict(plan.get("exit_watch"))
        if not exit_watch:
            continue
        expires_at = f.parse_ts(exit_watch.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        rows.append(
            ExitWatchRow(
                symbol=str(
                    plan.get("symbol") or exit_watch.get("symbol") or "—"
                ),
                condition_label=f.condition_summary(
                    exit_watch.get("conditions"),
                    exit_watch.get("logic"),
                    max_items=3,
                    limit=38,
                ),
                countdown=f.countdown(expires_at, now=now),
            )
        )
    return ExitWatchesProjection(rows=rows)


__all__ = [
    "ActiveWatchRow",
    "ActiveWatchesProjection",
    "ArmedOrderRow",
    "ArmedOrdersProjection",
    "ExitPlanRow",
    "ExitPlansProjection",
    "ExitWatchRow",
    "ExitWatchesProjection",
    "ReviewKind",
    "exit_update_rejected_symbols",
    "format_price",
    "project_active_watches",
    "project_armed_orders",
    "project_exit_plans",
    "project_exit_watches",
    "reviewed_status",
    "stop_distance_sort_key",
    "stop_pct",
    "take_profit_label",
]
