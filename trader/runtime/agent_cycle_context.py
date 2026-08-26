"""Build the agent-visible context for one daemon cycle."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Callable

from trader.agent.learnings import consolidator
from trader.application.execute import risk_capacity
from trader.domain.risk import RiskLimits
from trader.domain.market import family_regime, fx
from trader.domain.market import sessions as market
from trader.domain.planning.indicator_watch import is_armed_plan
from trader.domain.planning.protocols import SchedulerLike
from trader.domain.trade_plan import TradePlan
from trader.reporting.read_models import live_kpis


log = logging.getLogger("casys-trader")

_PLAN_ENTRY_THESIS_MAX_CHARS = 500
_PLAN_REVIEW_VALUE_MAX_CHARS = 240
_PLAN_TRACKING_TEXT_MAX_CHARS = 240
_PLAN_MAX_TAKE_PROFITS = 12
_PLAN_MAX_FILLED_TAKE_PROFITS = 12
_PLAN_MAX_EXIT_WATCH_CONDITIONS = 12
_LAST_LLM_REVIEW_KEYS = (
    "ts",
    "verdict",
    "action",
    "intent",
    "llm_provider",
    "llm_model",
)
_EXIT_WATCH_KEYS = (
    "id",
    "created_at",
    "expires_at",
    "logic",
    "on_trigger",
    "source",
    "cooldown_minutes",
    "last_triggered_at",
)
_EXIT_WATCH_CONDITION_KEYS = (
    "type",
    "symbol",
    "indicator",
    "op",
    "value",
    "timeframe",
    "source_interval",
    "lookback",
    "window",
    "as_of",
)


def global_plans_summary(
    sched: SchedulerLike | None, now: datetime
) -> list[dict]:
    """Project active watches into the bounded global plan summary."""

    if sched is None:
        return []
    try:
        summaries: list[dict] = []
        for watch in sched.active_indicator_watches(now=now):
            armed = is_armed_plan(watch)
            item = {
                "symbol": watch.get("symbol"),
                "id": watch.get("id"),
                "kind": "armed" if armed else "wake",
            }
            if armed:
                order = watch.get("order")
                if isinstance(order, dict) and order.get("intent") is not None:
                    item["intent"] = order.get("intent")
            summaries.append(item)
        return summaries
    except Exception:
        log.warning(
            "[plans_summary] échec construction résumé global "
            "(conscience d'état dégradée)",
            exc_info=True,
        )
        return []


def _bounded_plan_text(value: object | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    return str(value)[:max_chars]


def _json_scalar(value: object, *, max_chars: int = _PLAN_TRACKING_TEXT_MAX_CHARS) -> object:
    """Return a strict-JSON scalar without copying arbitrary persisted objects."""

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_plan_text(value, max_chars=max_chars)
    return None


def _compact_last_llm_review(review: dict | None) -> dict | None:
    if not isinstance(review, dict):
        return None
    compact = {}
    for key in _LAST_LLM_REVIEW_KEYS:
        value = review.get(key)
        if key in review and (value is None or isinstance(value, str)):
            compact[key] = _bounded_plan_text(
                value,
                max_chars=_PLAN_REVIEW_VALUE_MAX_CHARS,
            )
    return compact or None


def _compact_take_profit(take_profit: object) -> dict:
    """Keep the executable TP rule while making numeric values strict JSON."""

    return {
        "name": _bounded_plan_text(
            getattr(take_profit, "name", None),
            max_chars=_PLAN_TRACKING_TEXT_MAX_CHARS,
        ),
        "price": _json_scalar(getattr(take_profit, "price", None)),
        "fraction": _json_scalar(getattr(take_profit, "fraction", None)),
        "quantity": _json_scalar(getattr(take_profit, "quantity", None)),
        "after_fill": _bounded_plan_text(
            getattr(take_profit, "after_fill", None),
            max_chars=_PLAN_TRACKING_TEXT_MAX_CHARS,
        )
        or "",
    }


def _compact_exit_watch_condition(condition: object) -> dict | None:
    if not isinstance(condition, Mapping):
        return None
    compact = {}
    for key in _EXIT_WATCH_CONDITION_KEYS:
        value = (
            condition.get("timeframe", condition.get("interval"))
            if key == "timeframe"
            else condition.get(key)
        )
        if value is not None:
            compact[key] = _json_scalar(value)
    return compact or None


def _compact_exit_watch(watch: dict | None) -> dict | None:
    """Expose the active watch rule and cooldown, never its raw auxiliary payload."""

    if not isinstance(watch, Mapping):
        return None
    compact = {}
    for key in _EXIT_WATCH_KEYS:
        if key in watch:
            compact[key] = _json_scalar(watch[key])
    raw_conditions = watch.get("conditions")
    conditions = [raw_conditions] if isinstance(raw_conditions, Mapping) else raw_conditions
    if isinstance(conditions, list):
        projected_conditions = [
            projected
            for condition in conditions
            if (projected := _compact_exit_watch_condition(condition)) is not None
        ]
        compact["conditions"] = projected_conditions[:_PLAN_MAX_EXIT_WATCH_CONDITIONS]
        if len(projected_conditions) > _PLAN_MAX_EXIT_WATCH_CONDITIONS:
            compact["conditions_truncated"] = True
    return compact or None


def plan_to_context_dict(plan: TradePlan) -> dict:
    """Project one open plan into its bounded, agent-visible operational state.

    This is deliberately not ``model_dump()``: it retains every rule the exit
    engine can enforce and the state needed to interpret it, while excluding
    the large entry snapshot and non-operational plan fields from the prompt.
    """

    take_profits = plan.take_profits[:_PLAN_MAX_TAKE_PROFITS]
    filled_take_profits = plan.filled_take_profits[:_PLAN_MAX_FILLED_TAKE_PROFITS]
    context = {
        "id": plan.id,
        "symbol": plan.symbol,
        "side": plan.side,
        "quantity": _json_scalar(plan.quantity),
        "remaining_quantity": _json_scalar(plan.remaining_quantity),
        "opened_at": _bounded_plan_text(
            plan.opened_at,
            max_chars=_PLAN_TRACKING_TEXT_MAX_CHARS,
        ),
        "entry_price": _json_scalar(plan.entry_price),
        "reference_volatility": _json_scalar(plan.reference_volatility),
        "hard_stop_price": _json_scalar(plan.hard_stop_price),
        "take_profits": [
            _compact_take_profit(take_profit) for take_profit in take_profits
        ],
        "trailing_stop": (
            None
            if plan.trailing_stop is None
            else {
                "enabled_after": _bounded_plan_text(
                    plan.trailing_stop.enabled_after,
                    max_chars=_PLAN_TRACKING_TEXT_MAX_CHARS,
                ),
                "trail_type": plan.trailing_stop.trail_type,
                "trail_value": _json_scalar(plan.trailing_stop.trail_value),
                "trail_floored": plan.trailing_stop.trail_floored,
            }
        ),
        "max_hold_minutes": _json_scalar(plan.max_hold_minutes),
        "high_watermark": _json_scalar(plan.high_watermark),
        "low_watermark": _json_scalar(plan.low_watermark),
        "filled_take_profits": [
            _bounded_plan_text(name, max_chars=_PLAN_TRACKING_TEXT_MAX_CHARS)
            for name in filled_take_profits
        ],
        "profit_protection": (
            None
            if plan.profit_protection is None
            else {
                "enabled": plan.profit_protection.enabled,
                "arm_at_r": _json_scalar(plan.profit_protection.arm_at_r),
                "trigger_on_giveback_pct": _json_scalar(
                    plan.profit_protection.trigger_on_giveback_pct
                ),
                "close_fraction": _json_scalar(plan.profit_protection.close_fraction),
                "move_stop_to": plan.profit_protection.move_stop_to,
                "min_hold_minutes": _json_scalar(plan.profit_protection.min_hold_minutes),
                "lock_r": _json_scalar(plan.profit_protection.lock_r),
                "triggered": plan.profit_protection.triggered,
            }
        ),
        "exit_watch": _compact_exit_watch(plan.exit_watch),
        "last_llm_review": _compact_last_llm_review(plan.last_llm_review),
        "entry_thesis": _bounded_plan_text(
            plan.entry_thesis,
            max_chars=_PLAN_ENTRY_THESIS_MAX_CHARS,
        ),
    }
    if len(plan.take_profits) > _PLAN_MAX_TAKE_PROFITS:
        context["take_profits_truncated"] = True
    if len(plan.filled_take_profits) > _PLAN_MAX_FILLED_TAKE_PROFITS:
        context["filled_take_profits_truncated"] = True
    return context


def _build_learnings_context(
    consolidated_learnings_store: object,
    learnings_store: object,
    *,
    max_learnings_in_context: int,
    root: Path,
    recall_store: object | None,
) -> dict:
    consolidated = consolidated_learnings_store.read()
    kwargs: dict = {
        "raw_recent": learnings_store.recent(limit=max_learnings_in_context),
        "guardrails": consolidator.load_guardrails(root / "mandate" / "guardrails.json"),
    }
    reader = getattr(recall_store, "global_rule_scores", None) if recall_store is not None else None
    if callable(reader):
        try:
            kwargs["rule_scores"] = reader(active_only=True)
        except Exception as exc:  # noqa: BLE001 - MemRL on the prompt is advisory
            log.warning("global rule citation scores unread (%s)", exc)
    return consolidator.build_context_learnings(consolidated, **kwargs)


def build_base_context(
    *,
    cycle_id: str,
    now: datetime,
    symbols: list[str],
    snap: object,
    portfolio_fee_estimator: Callable[
        [str, float, float, float], float | None
    ]
    | None,
    risk_cfg: dict,
    prices: dict[str, float],
    broker: object,
    gross: float,
    gate_limits: RiskLimits,
    rate_for_symbol: Callable[[str], float],
    cockpit: dict,
    stale_market_data: dict,
    sched: SchedulerLike | None,
    state_dir: Path,
    root: Path,
    attribution_payload: dict,
    meta_performance_payload: dict,
    consolidated_learnings_store: object,
    learnings_store: object,
    max_learnings_in_context: int,
    daily_bars_by_symbol: dict,
    active_families: object,
    requestable_indicator_ids: object,
    recall_store: object | None = None,
    portfolio_fee_estimator_cost_scope: str | None = None,
    portfolio_fee_estimator_is_all_in: bool = False,
) -> dict:
    """Assemble the complete payload exposed to the decision agent."""

    return {
        "now": cycle_id,
        "now_human": market.human_clock(now),
        "market_clocks": market.market_clocks(now, symbols),
        "portfolio": snap.as_context(
            fee_estimator=portfolio_fee_estimator,
            fee_estimator_cost_scope=portfolio_fee_estimator_cost_scope,
            fee_estimator_is_all_in=portfolio_fee_estimator_is_all_in,
        ),
        "risk_limits": risk_cfg,
        "risk_capacity": risk_capacity.risk_capacity_context(
            symbols=[symbol for symbol in symbols if symbol in prices],
            prices=prices,
            broker=broker,
            gross_exposure=gross,
            limits=gate_limits,
            equity=snap.equity,
            rate_of=rate_for_symbol,
            currency_of=fx.currency_for,
        ),
        "semantic": {
            "requestable_indicator_ids": requestable_indicator_ids,
        },
        "cockpit": cockpit,
        "stale_market_data": stale_market_data,
        "active_plans_summary": global_plans_summary(sched, now),
        "kpis": live_kpis.compute_live_kpis(state_dir),
        "attribution": attribution_payload,
        "meta_performance": meta_performance_payload,
        "learnings": _build_learnings_context(
            consolidated_learnings_store,
            learnings_store,
            max_learnings_in_context=max_learnings_in_context,
            root=root,
            recall_store=recall_store,
        ),
        "regime_families": family_regime.compute_family_bias(
            {
                symbol: family_regime.momentum_from_bars(bars)
                for symbol, bars in daily_bars_by_symbol.items()
            },
            active_families,
        ),
    }


__all__ = ["build_base_context", "global_plans_summary", "plan_to_context_dict"]
