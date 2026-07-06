from __future__ import annotations

from dataclasses import replace

from trader.application.fill_plan_effects import apply_filled_plan_effects
from trader.execution.contracts import Position
from trader.planning.trade_plan import TradePlan, create_trade_plan


class _Broker:
    def __init__(self, positions: dict[str, Position] | None = None) -> None:
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)


class _PlanStore:
    def __init__(self, plans: list[TradePlan] | None = None) -> None:
        self.plans = list(plans or [])
        self.closed_symbols: list[str] = []
        self.synced_quantities: list[tuple[str, float]] = []
        self.upserts: list[TradePlan] = []

    def open_plans(self) -> list[TradePlan]:
        return list(self.plans)

    def upsert(self, plan: TradePlan) -> None:
        self.upserts.append(plan)
        self.plans = [existing for existing in self.plans if existing.id != plan.id]
        self.plans.append(plan)

    def close_symbol(self, symbol: str) -> None:
        self.closed_symbols.append(symbol)
        self.plans = [plan for plan in self.plans if plan.symbol != symbol]

    def sync_symbol_quantity(self, symbol: str, quantity: float) -> None:
        self.synced_quantities.append((symbol, quantity))


def _exit_plan() -> dict:
    return {
        "hard_stop": {"type": "price", "price": 95.0},
        "take_profits": [{"price": 110.0, "fraction": 1.0}],
    }


def _entry_context() -> dict:
    return {
        "price": 100.0,
        "runtime_interval": "15m",
        "data_age_m": 2,
        "session_open": True,
        "daily_as_of": "2026-07-05",
    }


def _plan(symbol: str = "SPY", *, review: dict | None = None) -> TradePlan:
    plan = create_trade_plan(
        symbol=symbol,
        side="LONG",
        quantity=3.0,
        entry_price=90.0,
        opened_at="2026-07-05T07:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )
    return plan if review is None else replace(plan, last_llm_review=review)


def _apply(**overrides) -> tuple[dict, _PlanStore]:
    entry = overrides.pop("entry", {"symbol": "SPY", "trade_plan_created": False})
    store = overrides.pop("plan_store", _PlanStore())
    params = {
        "entry": entry,
        "broker": _Broker({"SPY": Position("SPY", quantity=2.0, avg_price=100.0)}),
        "plan_store": store,
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "quantity": 2.0,
        "price": 100.0,
        "opened_at": "2026-07-05T08:00:00+00:00",
        "runtime_exit_plan": _exit_plan(),
        "reference_volatility": 1.25,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5/medium",
        "llm_fallback_reason": None,
        "llm_confidence": 0.9,
        "queue_execute_enabled": False,
        "entry_thesis": "breakout",
        "entry_context": _entry_context(),
    }
    params.update(overrides)
    apply_filled_plan_effects(**params)
    return entry, store


def test_apply_filled_plan_effects_closes_plan_for_sync_close() -> None:
    entry, store = _apply(
        plan_store=_PlanStore([_plan()]),
        action="SELL",
        intent="CLOSE",
        runtime_exit_plan=None,
    )

    assert store.closed_symbols == ["SPY"]
    assert store.upserts == []
    assert entry["trade_plan_created"] is False


def test_apply_filled_plan_effects_syncs_reduce_remaining_quantity() -> None:
    _, store = _apply(
        intent="REDUCE",
        action="SELL",
        runtime_exit_plan=None,
        broker=_Broker({"SPY": Position("SPY", quantity=1.25, avg_price=100.0)}),
    )

    assert store.synced_quantities == [("SPY", 1.25)]


def test_apply_filled_plan_effects_creates_open_plan_and_entry_snapshot() -> None:
    entry, store = _apply()

    assert len(store.upserts) == 1
    plan = store.upserts[0]
    assert plan.symbol == "SPY"
    assert plan.side == "LONG"
    assert plan.quantity == 2.0
    assert plan.entry_thesis == "breakout"
    assert plan.entry_context == _entry_context()
    assert entry["trade_plan_created"] is True
    assert entry["trade_plan"]["symbol"] == "SPY"
    assert entry["trade_plan"]["quantity"] == 2.0


def test_apply_filled_plan_effects_preserves_review_when_sync_add_replaces_plan() -> None:
    review = {"ts": "2026-07-05T07:55:00+00:00", "action": "HOLD"}
    entry, store = _apply(
        plan_store=_PlanStore([_plan(review=review)]),
        intent="SCALE_IN",
        action="BUY",
        quantity=2.0,
        broker=_Broker({"SPY": Position("SPY", quantity=5.0, avg_price=96.0)}),
    )

    assert store.closed_symbols == ["SPY"]
    assert len(store.upserts) == 1
    plan = store.upserts[0]
    assert plan.quantity == 5.0
    assert plan.entry_price == 96.0
    assert plan.last_llm_review == review
    assert entry["trade_plan"]["quantity"] == 5.0


def test_apply_filled_plan_effects_sync_reverse_creates_plan_from_final_position() -> None:
    entry, store = _apply(
        plan_store=_PlanStore([_plan()]),
        intent="FLIP",
        action="SELL",
        quantity=7.0,
        price=101.0,
        broker=_Broker({"SPY": Position("SPY", quantity=-3.0, avg_price=101.0)}),
    )

    assert store.closed_symbols == ["SPY"]
    assert len(store.upserts) == 1
    plan = store.upserts[0]
    assert plan.side == "SHORT"
    assert plan.quantity == 3.0
    assert plan.entry_price == 101.0
    assert entry["trade_plan_created"] is True
    assert entry["trade_plan"]["side"] == "SHORT"


def test_apply_filled_plan_effects_queue_reverse_reports_existing_atomic_plan() -> None:
    queued_plan = create_trade_plan(
        symbol="SPY",
        side="SHORT",
        quantity=3.0,
        entry_price=101.0,
        opened_at="2026-07-05T08:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    entry, store = _apply(
        plan_store=_PlanStore([queued_plan]),
        intent="FLIP",
        action="SELL",
        queue_execute_enabled=True,
        broker=_Broker({"SPY": Position("SPY", quantity=-3.0, avg_price=101.0)}),
    )

    assert store.closed_symbols == []
    assert store.upserts == []
    assert entry["trade_plan_created"] is True
    assert entry["trade_plan"]["id"] == queued_plan.id


def test_apply_filled_plan_effects_queue_open_reports_plan_without_local_upsert() -> None:
    entry, store = _apply(queue_execute_enabled=True)

    assert store.closed_symbols == []
    assert store.upserts == []
    assert entry["trade_plan_created"] is True
    assert entry["trade_plan"]["symbol"] == "SPY"
    assert entry["trade_plan"]["quantity"] == 2.0


def test_apply_filled_plan_effects_queue_add_reports_plan_without_local_close_or_upsert() -> None:
    queued_plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=5.0,
        entry_price=96.0,
        opened_at="2026-07-05T08:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    entry, store = _apply(
        plan_store=_PlanStore([queued_plan]),
        intent="SCALE_IN",
        action="BUY",
        quantity=2.0,
        queue_execute_enabled=True,
        broker=_Broker({"SPY": Position("SPY", quantity=5.0, avg_price=96.0)}),
    )

    assert store.closed_symbols == []
    assert store.upserts == []
    assert entry["trade_plan_created"] is True
    assert entry["trade_plan"]["quantity"] == 5.0
    assert entry["trade_plan"]["entry_price"] == 96.0
