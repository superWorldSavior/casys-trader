"""T4 — build_decide_tool_services : services du tour d'outils construits au boot."""
from pathlib import Path

from trader.application.decide_one import ToolRoundServices
from trader.runtime import queue_runtime
from trader.runtime.queue_runtime import build_decide_tool_services
from trader.runtime.worker_cycle_context import WorkerCycleContextHandle


def _build(tmp_path: Path, **over):
    base = dict(
        get_data_source=lambda: None,
        learnings_db_path=tmp_path / "learnings.db",
        max_context_requests_per_symbol=2,
        max_indicators_per_request=4,
    )
    base.update(over)
    return build_decide_tool_services(**base)


def test_services_construits_sans_learnings_db(tmp_path):
    # learnings.db absent au boot → recall indisponible, mais services utilisables.
    services = _build(tmp_path)
    assert isinstance(services, ToolRoundServices)
    assert services.learnings_recall_provider is None
    assert not hasattr(services, "max_rounds")
    assert not hasattr(services, "open_plans_provider")
    assert not hasattr(services, "open_plans_as_of_provider")


def test_get_bars_suit_le_handle(tmp_path):
    # get_bars est bien l'indirection : suit la ref courante du getter injecté.
    class _DS:
        def get_bars(self, symbol, lookback="5d", interval="1h"):
            return [("bar", symbol)]

    current = {"ds": None}
    services = _build(tmp_path, get_data_source=lambda: current["ds"])
    current["ds"] = _DS()
    assert services.get_bars("SPY") == [("bar", "SPY")]


def test_services_recoivent_le_worker_cycle_context_unique(tmp_path):
    handle = WorkerCycleContextHandle()

    services = _build(tmp_path, worker_cycle_context=handle)

    assert services.worker_cycle_context is handle


def test_action_validator_absent_sans_worker_cycle_context(tmp_path):
    services = _build(tmp_path)

    assert services.action_validator is None


def test_build_decide_tool_services_n_accepte_plus_les_vieux_handles_de_cycle(tmp_path):
    for old_arg, value in {
        "get_open_plans": lambda: [],
        "get_open_raw_plans": lambda: [],
        "get_open_plans_as_of": lambda: None,
        "exit_validation_snapshot": object(),
    }.items():
        try:
            _build(tmp_path, **{old_arg: value})
        except TypeError as exc:
            assert old_arg in str(exc)
        else:
            raise AssertionError(f"build_decide_tool_services ne doit plus accepter {old_arg}")


def test_queue_runtime_ne_contient_plus_snapshot_plan_store() -> None:
    assert not hasattr(queue_runtime, "_SnapshotPlanStore")


def test_services_n_acceptent_plus_max_rounds(tmp_path):
    try:
        _build(tmp_path, max_rounds=0)
    except TypeError as exc:
        assert "max_rounds" in str(exc)
    else:
        raise AssertionError("build_decide_tool_services ne doit plus accepter max_rounds")


def test_tool_limits_grain_1(tmp_path):
    # Bornes grain-1 : 8 calls/symbole (vs 3 calibré batch), 24 total.
    limits = _build(tmp_path).tool_limits()
    assert limits.max_calls_per_symbol == 8
    assert limits.max_total_calls == 24
