"""Application helpers for persisting LLM trade-plan reviews."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol


class DecisionLike(Protocol):
    intent: str
    action: str
    llm_provider: str | None
    llm_model: str | None


class TradePlanLike(Protocol):
    symbol: str
    last_llm_review: dict | None


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlanLike]: ...

    def upsert(self, plan: TradePlanLike) -> None: ...


def llm_review_verdict(intent: str) -> str:
    """Return the persisted thesis verdict for an LLM decision intent."""
    if intent == "HOLD":
        return "intact"
    if intent == "REDUCE":
        return "fragile"
    if intent in {"CLOSE", "FLIP"}:
        return "invalidated"
    return "fragile"


def persist_last_llm_review(
    *,
    plan_store: TradePlanStoreLike,
    symbol: str,
    now: datetime,
    decision: DecisionLike,
) -> None:
    """Persist the latest real LLM review on matching open plans."""
    if not (decision.llm_provider or decision.llm_model):
        return
    review = {
        "ts": now.astimezone(timezone.utc).isoformat(),
        "verdict": llm_review_verdict(decision.intent),
        "action": decision.action,
        "intent": decision.intent,
        "llm_provider": decision.llm_provider,
        "llm_model": decision.llm_model,
    }
    for plan in plan_store.open_plans():
        if plan.symbol == symbol:
            plan_store.upsert(replace(plan, last_llm_review=review))


def last_review_by_symbol(
    plan_store: TradePlanStoreLike,
    symbols: list[str],
) -> dict[str, dict]:
    """Return latest LLM review by symbol for matching open plans only."""
    wanted = set(symbols)
    out: dict[str, dict] = {}
    for plan in plan_store.open_plans():
        if plan.symbol in wanted and plan.last_llm_review:
            out[plan.symbol] = plan.last_llm_review
    return out
