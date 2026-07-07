"""T4 — build_decide_tool_services : services du tour d'outils construits au boot."""
from pathlib import Path

from trader.application.exit_update import ExitUpdateValidation
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


class _ExitValidationSnapshot:
    def __init__(self, *, bars_by_symbol=None, prices=None) -> None:
        self.bars_by_symbol = bars_by_symbol or {}
        self.prices = prices or {}

    def get_bars(self, symbol: str):
        return self.bars_by_symbol.get(symbol)

    def get_price(self, symbol: str):
        return self.prices.get(symbol)


def test_action_validator_valide_strategy_exit_sur_snapshot_runtime_sans_refetch(tmp_path, monkeypatch):
    raw_plan = object()
    runtime_bars = [("runtime", "SPY")]
    validation = ExitUpdateValidation(would_apply=False, reason="resolve_failed:test", warnings=[])
    calls: dict[str, object] = {}

    class _DS:
        def get_bars(self, symbol, lookback="5d", interval="1h"):
            raise AssertionError("action_validator must not refetch bars")

    def fake_validate_exit_update(**kwargs):
        calls["validate"] = kwargs
        return validation

    monkeypatch.setattr("trader.application.exit_update.validate_exit_update", fake_validate_exit_update)

    services = _build(
        tmp_path,
        get_data_source=lambda: _DS(),
        get_open_raw_plans=lambda: [raw_plan],
        exit_validation_snapshot=_ExitValidationSnapshot(
            bars_by_symbol={"SPY": runtime_bars},
            prices={"SPY": 432.1},
        ),
    )

    assert services.action_validator is not None
    result = services.action_validator("SPY", {"hard_stop": {"mode": "structural"}})

    assert result is validation
    validate_call = calls["validate"]
    assert validate_call["plan_store"].open_plans() == [raw_plan]
    assert validate_call["symbol"] == "SPY"
    assert validate_call["exit_update"] == {"hard_stop": {"mode": "structural"}}
    assert validate_call["bars"] == runtime_bars
    assert validate_call["current_price"] == 432.1


def test_action_validator_rejete_si_bars_runtime_absentes_dans_snapshot(tmp_path, monkeypatch):
    validation = ExitUpdateValidation(
        would_apply=False,
        reason="resolve_failed:hard_stop_bars_unavailable",
        warnings=[],
    )

    class _DS:
        def get_bars(self, symbol, lookback="5d", interval="1h"):
            raise AssertionError("action_validator must not refetch bars")

    def fake_validate_exit_update(**kwargs):
        assert kwargs["bars"] is None
        return validation

    monkeypatch.setattr("trader.application.exit_update.validate_exit_update", fake_validate_exit_update)

    services = _build(
        tmp_path,
        get_data_source=lambda: _DS(),
        get_open_raw_plans=lambda: [object()],
        exit_validation_snapshot=_ExitValidationSnapshot(),
    )

    result = services.action_validator("SPY", {"hard_stop": {"type": "structural", "anchor": "swing_low"}})

    assert result is validation
    assert result.would_apply is False
    assert result.reason == "resolve_failed:hard_stop_bars_unavailable"
    assert result.warnings == []


def test_action_validator_garde_rejet_fiable_no_open_plan_meme_sans_bars_runtime(tmp_path, monkeypatch):
    validation = ExitUpdateValidation(would_apply=False, reason="no_open_plan", warnings=[])

    class _DS:
        def get_bars(self, symbol, lookback="5d", interval="1h"):
            raise AssertionError("action_validator must not refetch bars")

    def fake_validate_exit_update(**kwargs):
        assert kwargs["bars"] is None
        return validation

    monkeypatch.setattr("trader.application.exit_update.validate_exit_update", fake_validate_exit_update)

    services = _build(
        tmp_path,
        get_data_source=lambda: _DS(),
        get_open_raw_plans=lambda: [],
        exit_validation_snapshot=_ExitValidationSnapshot(),
    )

    result = services.action_validator("SPY", {"hard_stop": {"type": "structural", "anchor": "swing_low"}})

    assert result is validation
    assert result.would_apply is False
    assert result.reason == "no_open_plan"


def test_action_validator_absent_sans_snapshot_validation_exit(tmp_path):
    services = _build(
        tmp_path,
        get_open_raw_plans=lambda: [object()],
    )

    assert services.action_validator is None


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
