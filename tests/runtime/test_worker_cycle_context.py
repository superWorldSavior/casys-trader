from __future__ import annotations

import pytest

from trader.runtime.worker_cycle_context import (
    CycleContextUnavailable,
    ExitValidationInputs,
    OpenPlansSnapshot,
    SnapshotTradePlanStore,
    WorkerCycleContext,
    WorkerCycleContextHandle,
)


def test_worker_cycle_context_handle_publie_un_contexte_atomique_et_borne_par_cycle() -> None:
    handle = WorkerCycleContextHandle()

    assert handle.current() is None
    assert handle.get_open_plan_rows() == []
    assert handle.get_raw_open_plans() == []
    assert handle.get_open_plans_as_of() is None
    assert handle.get_attribution() == {}
    assert handle.get_exit_validation_bars("SPY") is None
    assert handle.get_exit_validation_price("SPY") is None

    raw_plan = object()
    bars = [{"close": 100.0}]
    first = WorkerCycleContext(
        cycle_id="cycle-1",
        as_of="cycle-1",
        open_plans=OpenPlansSnapshot(
            rows=({"id": "plan-spy", "symbol": "SPY"},),
            raw_plans=(raw_plan,),
        ),
        exit_validation=ExitValidationInputs(
            bars_by_symbol={"SPY": bars},
            prices_by_symbol={"SPY": 101.25},
        ),
        attribution={"n_closed_trades": 4},
    )
    handle.publish(first)

    assert handle.current() is first
    assert handle.current_for_cycle("cycle-1") is first
    assert handle.get_open_plan_rows("cycle-1") == [{"id": "plan-spy", "symbol": "SPY"}]
    assert handle.get_raw_open_plans("cycle-1") == [raw_plan]
    assert handle.get_open_plans_as_of("cycle-1") == "cycle-1"
    assert handle.get_attribution("cycle-1") == {"n_closed_trades": 4}
    assert handle.get_exit_validation_bars("SPY", "cycle-1") == bars
    assert handle.get_exit_validation_price("SPY", "cycle-1") == 101.25

    handle.publish_attribution(
        cycle_id="cycle-1",
        attribution={"n_closed_trades": 5, "realized_pnl": 12.0},
    )
    assert handle.get_attribution("cycle-1") == {
        "n_closed_trades": 5,
        "realized_pnl": 12.0,
    }

    second = WorkerCycleContext(
        cycle_id="cycle-2",
        as_of="cycle-2",
        open_plans=OpenPlansSnapshot(rows=({"id": "plan-qqq", "symbol": "QQQ"},)),
        exit_validation=ExitValidationInputs(),
    )
    handle.publish(second)

    assert handle.current() is second
    assert handle.get_open_plan_rows() == [{"id": "plan-qqq", "symbol": "QQQ"}]
    with pytest.raises(CycleContextUnavailable):
        handle.current_for_cycle("cycle-1")
    with pytest.raises(CycleContextUnavailable):
        handle.get_open_plan_rows("cycle-1")


def test_snapshot_trade_plan_store_expose_les_plans_sans_mutation() -> None:
    raw_plan = object()
    store = SnapshotTradePlanStore([raw_plan])

    assert store.open_plans() == [raw_plan]
    store.upsert(object())
    assert store.open_plans() == [raw_plan]
