from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.application.plan_review import (
    last_review_by_symbol,
    llm_review_verdict,
    persist_last_llm_review,
)


@dataclass(frozen=True)
class DecisionStub:
    intent: str
    action: str
    llm_provider: str | None = None
    llm_model: str | None = None


@dataclass(frozen=True)
class PlanStub:
    symbol: str
    last_llm_review: dict | None = None


class PlanStoreStub:
    def __init__(self, plans: list[PlanStub]) -> None:
        self._plans = plans
        self.upserts: list[PlanStub] = []

    def open_plans(self) -> list[PlanStub]:
        return list(self._plans)

    def upsert(self, plan: PlanStub) -> None:
        self.upserts.append(plan)


def test_llm_review_verdict_maps_intents_to_thesis_status() -> None:
    assert llm_review_verdict("HOLD") == "intact"
    assert llm_review_verdict("REDUCE") == "fragile"
    assert llm_review_verdict("CLOSE") == "invalidated"
    assert llm_review_verdict("REVERSE") == "invalidated"
    assert llm_review_verdict("OPEN_LONG") == "fragile"


def test_persist_last_llm_review_skips_decisions_without_llm_identity() -> None:
    store = PlanStoreStub([PlanStub("SPY")])

    persist_last_llm_review(
        plan_store=store,
        symbol="SPY",
        now=datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc),
        decision=DecisionStub(intent="HOLD", action="HOLD"),
    )

    assert store.upserts == []


def test_persist_last_llm_review_updates_only_matching_open_plan() -> None:
    store = PlanStoreStub([PlanStub("SPY"), PlanStub("QQQ")])
    now = datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc)

    persist_last_llm_review(
        plan_store=store,
        symbol="SPY",
        now=now,
        decision=DecisionStub(
            intent="HOLD",
            action="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
        ),
    )

    assert store.upserts == [
        PlanStub(
            "SPY",
            {
                "ts": "2026-06-05T12:15:00+00:00",
                "verdict": "intact",
                "action": "HOLD",
                "intent": "HOLD",
                "llm_provider": "acpx",
                "llm_model": "gpt-5.5/medium",
            },
        )
    ]


def test_persist_last_llm_review_normalizes_review_timestamp_to_utc() -> None:
    store = PlanStoreStub([PlanStub("SPY")])

    persist_last_llm_review(
        plan_store=store,
        symbol="SPY",
        now=datetime(2026, 6, 5, 20, 15, tzinfo=timezone(timedelta(hours=8))),
        decision=DecisionStub(
            intent="HOLD",
            action="HOLD",
            llm_provider="acpx",
        ),
    )

    assert store.upserts[0].last_llm_review["ts"] == "2026-06-05T12:15:00+00:00"


def test_last_review_by_symbol_filters_missing_reviews_and_scope() -> None:
    review = {"ts": "2026-06-05T12:15:00+00:00", "verdict": "intact"}
    store = PlanStoreStub(
        [
            PlanStub("SPY", review),
            PlanStub("QQQ"),
            PlanStub("IWM", review),
        ]
    )

    assert last_review_by_symbol(store, ["SPY", "QQQ"]) == {"SPY": review}
