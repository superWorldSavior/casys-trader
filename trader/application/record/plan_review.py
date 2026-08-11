"""Application helpers for persisting LLM trade-plan reviews."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Protocol


class DecisionLike(Protocol):
    intent: str
    action: str
    llm_provider: str | None
    llm_model: str | None


class TradePlanLike(Protocol):
    symbol: str
    last_llm_review: dict | None

    def model_copy(self, *, update: dict) -> "TradePlanLike": ...


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
    decision_id: str | None = None,
) -> dict | None:
    """Persist the latest real LLM review on matching open plans."""
    if not (decision.llm_provider or decision.llm_model):
        return None
    review = {
        "ts": now.astimezone(timezone.utc).isoformat(),
        "verdict": llm_review_verdict(decision.intent),
        "action": decision.action,
        "intent": decision.intent,
        "llm_provider": decision.llm_provider,
        "llm_model": decision.llm_model,
        **({"decision_id": decision_id} if decision_id else {}),
    }
    persisted = False
    for plan in plan_store.open_plans():
        if plan.symbol == symbol:
            plan_store.upsert(plan.model_copy(update={"last_llm_review": review}))
            persisted = True
    return dict(review) if persisted else None


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


def annotate_learning_feedback(
    reviews_by_symbol: Mapping[str, dict],
    feedback_by_decision_id: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict]:
    """Attach outcome feedback while keeping only compact cockpit-facing fields."""

    result: dict[str, dict] = {}
    for symbol, review in reviews_by_symbol.items():
        copied = dict(review)
        decision_id = str(copied.pop("decision_id", "") or "")
        feedback = (feedback_by_decision_id or {}).get(decision_id)
        if isinstance(feedback, Mapping):
            compact = {"status": str(feedback.get("status") or "pending")}
            for field in ("verdict", "forward_return", "outcome_score"):
                if feedback.get(field) is not None:
                    compact[field] = feedback[field]
            copied["feedback"] = compact
        result[symbol] = copied
    return result
