"""T4 — build_decide_tool_services : services du tour d'outils construits au boot."""
from pathlib import Path

from trader.application.decide_one import ToolRoundServices
from trader.runtime.queue_runtime import build_decide_tool_services


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


def test_get_bars_suit_le_handle(tmp_path):
    # get_bars est bien l'indirection : suit la ref courante du getter injecté.
    class _DS:
        def get_bars(self, symbol, lookback="5d", interval="1h"):
            return [("bar", symbol)]

    current = {"ds": None}
    services = _build(tmp_path, get_data_source=lambda: current["ds"])
    current["ds"] = _DS()
    assert services.get_bars("SPY") == [("bar", "SPY")]


def test_get_open_plans_provider_est_propage(tmp_path):
    def get_open_plans() -> list:
        return [{"id": "plan-spy", "symbol": "SPY"}]

    def get_open_plans_as_of() -> str:
        return "2026-07-05T08:00:00+00:00"

    services = _build(
        tmp_path,
        get_open_plans=get_open_plans,
        get_open_plans_as_of=get_open_plans_as_of,
    )

    assert services.open_plans_provider is get_open_plans
    assert services.open_plans_as_of_provider is get_open_plans_as_of


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
