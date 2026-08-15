"""Build the agent-visible context for one daemon cycle."""

from __future__ import annotations

import logging
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
_LAST_LLM_REVIEW_KEYS = (
    "ts",
    "verdict",
    "action",
    "intent",
    "llm_provider",
    "llm_model",
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


def _bounded_plan_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    return str(value)[:max_chars]


def _compact_last_llm_review(review: dict | None) -> dict | None:
    if not isinstance(review, dict):
        return None
    compact = {
        key: review[key]
        for key in _LAST_LLM_REVIEW_KEYS
        if key in review and review[key] is None or isinstance(review.get(key), str)
    }
    return compact or None


def plan_to_context_dict(plan: TradePlan) -> dict:
    """Project one trade plan into the bounded agent-context representation."""

    return {
        "id": plan.id,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry_price": plan.entry_price,
        "hard_stop_price": plan.hard_stop_price,
        "take_profits": [
            take_profit.model_dump() for take_profit in plan.take_profits
        ],
        "remaining_quantity": plan.remaining_quantity,
        "last_llm_review": _compact_last_llm_review(plan.last_llm_review),
        "entry_thesis": _bounded_plan_text(
            plan.entry_thesis,
            max_chars=_PLAN_ENTRY_THESIS_MAX_CHARS,
        ),
    }


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
) -> dict:
    """Assemble the complete payload exposed to the decision agent."""

    return {
        "now": cycle_id,
        "now_human": market.human_clock(now),
        "market_clocks": market.market_clocks(now, symbols),
        "portfolio": snap.as_context(fee_estimator=portfolio_fee_estimator),
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
