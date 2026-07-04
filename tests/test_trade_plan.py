from dataclasses import replace
from datetime import datetime, timezone

import pytest

import trader.planning.exit_engine as exit_engine
from trader.market.market_data import Bar
from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    TradePlanStore,
    create_trade_plan,
    resolve_exit_plan,
    trade_plan_from_dict,
    validate_exit_plan,
)


def _bar(
    ts: str,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
) -> Bar:
    return Bar(
        ts=ts,
        open=close,
        high=high if high is not None else close + 1.0,
        low=low if low is not None else close - 1.0,
        close=close,
        volume=volume,
    )


def _exit_plan() -> dict:
    return {
        "hard_stop": {"type": "price", "price": 95.0},
        "take_profits": [
            {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "move_stop_to_breakeven"},
            {"name": "tp2", "price": 110.0, "fraction": 0.5, "after_fill": "close"},
        ],
        "trailing_stop": {
            "enabled_after": "tp1",
            "trail_type": "price",
            "trail_value": 2.0,
        },
        "max_hold_minutes": 45,
    }


def test_create_trade_plan_normalise_exit_plan_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    assert plan.symbol == "SPY"
    assert plan.remaining_quantity == 10.0
    assert plan.hard_stop_price == 95.0
    assert plan.take_profits[0].name == "tp1"
    assert plan.take_profits[0].quantity == 5.0
    assert plan.trailing_stop is not None
    assert plan.max_hold_minutes == 45.0
    assert plan.profit_protection is None


def test_create_trade_plan_accepte_hard_stop_prix_direct() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )

    assert plan.hard_stop_price == 95.0
    assert plan.profit_protection is None


def test_create_trade_plan_accepte_profit_protection_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "profit_protection": {
                "arm_at_r": 0.8,
                "trigger_on_giveback_pct": 0.25,
                "close_fraction": 0.5,
                "move_stop_to": "breakeven",
                "min_hold_minutes": 15,
            },
        },
    )

    assert plan.profit_protection is not None
    assert plan.profit_protection.arm_at_r == 0.8
    assert plan.profit_protection.trigger_on_giveback_pct == 0.25
    assert plan.profit_protection.close_fraction == 0.5


def test_create_trade_plan_normalise_profit_protection_compacte_en_r() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "profit_protection": {
                "enabled_after_r": 1.0,
                "giveback": 0.35,
                "protect_r": 0.25,
            },
        },
    )

    assert plan.profit_protection is not None
    assert plan.profit_protection.arm_at_r == 1.0
    assert plan.profit_protection.trigger_on_giveback_pct == 0.35
    assert plan.profit_protection.lock_r == 0.25


def test_create_trade_plan_accepte_exit_watch_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": {
                "ttl_minutes": 90,
                "cooldown_minutes": 12,
                "logic": "any",
                "on_trigger": "ORDER",
                "conditions": [
                    {"indicator": "trend_slope", "op": "<", "value": 0, "timeframe": "15m"},
                ],
            },
        },
    )

    assert plan.exit_watch is not None
    assert plan.exit_watch["symbol"] == "SPY"
    assert plan.exit_watch["logic"] == "any"
    assert plan.exit_watch["on_trigger"] == "WAKE"
    assert plan.exit_watch["cooldown_minutes"] == 12.0
    assert plan.exit_watch["conditions"][0]["indicator"] == "trend_slope"


def test_create_trade_plan_normalise_take_profits_prix_directs_et_trailing_numeric() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="SHORT",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 103.0,
            "take_profits": [98.0, 96.0],
            "trailing_stop": 0.5,
            "max_hold_minutes": 30,
        },
    )

    assert [tp.price for tp in plan.take_profits] == [98.0, 96.0]
    assert [tp.fraction for tp in plan.take_profits] == [0.5, 0.5]
    assert [tp.quantity for tp in plan.take_profits] == [5.0, 5.0]
    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_value == 0.5


def test_validate_exit_plan_ignore_trailing_stop_inexploitable_si_autres_sorties_valides() -> None:
    validate_exit_plan(
        {
            "hard_stop": 95.0,
            "take_profits": [{"price": 105.0, "fraction": 1.0}],
            "trailing_stop": {"enabled_after": "tp1"},
        }
    )


def test_validate_exit_plan_accepte_hard_stop_percent_non_resolu_si_autorise() -> None:
    validate_exit_plan(
        {
            "hard_stop": {"type": "percent", "percent": 0.04, "min_pct": 0.02, "max_pct": 0.08},
            "take_profits": [{"type": "risk_multiple", "r": 1.5, "fraction": 0.5}],
        },
        allow_unresolved=True,
    )


def test_validate_exit_plan_accepte_hard_stop_volatilite_non_resolu_sans_reference_si_autorise() -> None:
    validate_exit_plan(
        {
            "hard_stop": {
                "type": "volatility_multiple",
                "multiple": 2.0,
                "source": "atr",
                "timeframe": "5m",
                "window": 14,
                "min_pct": 0.01,
                "max_pct": 0.05,
            },
            "take_profits": [{"price": 105.0, "fraction": 1.0}],
        },
        allow_unresolved=True,
    )


def test_validate_exit_plan_accepte_hard_stop_structural_non_resolu_si_autorise() -> None:
    validate_exit_plan(
        {
            "hard_stop": {
                "type": "structural",
                "anchor": "swing_low",
                "window": 20,
                "timeframe": "15m",
                "buffer_pct": 0.001,
                "min_pct": 0.005,
                "max_pct": 0.03,
            },
            "take_profits": [{"price": 105.0, "fraction": 1.0}],
        },
        allow_unresolved=True,
    )


def test_validate_exit_plan_garde_contrat_resolu_par_defaut() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_type_unsupported"):
        validate_exit_plan({"hard_stop": {"type": "percent", "percent": 0.04}})

    with pytest.raises(InvalidExitPlanError, match="hard_stop_type_unsupported"):
        validate_exit_plan(
            {"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 20}}
        )

    with pytest.raises(InvalidExitPlanError, match="take_profits_0_price_required"):
        validate_exit_plan({"take_profits": [{"type": "risk_multiple", "r": 1.5}]})


@pytest.mark.parametrize(
    ("exit_plan", "code"),
    [
        (
            {"hard_stop": {"type": "percent", "percent": 0}},
            "hard_stop_percent_must_be_positive",
        ),
        (
            {"hard_stop": {"type": "percent", "percent": 1.01}},
            "hard_stop_percent_must_be_lte_1",
        ),
        (
            {"hard_stop": {"type": "percent", "percent": 0.04, "min_pct": 0.06, "max_pct": 0.05}},
            "hard_stop_min_pct_gt_max_pct",
        ),
        (
            {"hard_stop": {"type": "volatility_multiple", "multiple": 0}},
            "hard_stop_multiple_must_be_positive",
        ),
        (
            {"hard_stop": {"type": "structural", "anchor": "pivot_low", "window": 20}},
            "hard_stop_anchor_unsupported",
        ),
        (
            {"hard_stop": {"type": "structural", "anchor": "swing_low"}},
            "hard_stop_window_must_be_positive",
        ),
        (
            {"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 0}},
            "hard_stop_window_must_be_positive",
        ),
        (
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 20,
                    "buffer_pct": 0,
                }
            },
            "hard_stop_buffer_pct_must_be_positive",
        ),
        (
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 20,
                    "buffer_atr": 0,
                }
            },
            "hard_stop_buffer_atr_must_be_positive",
        ),
        (
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 20,
                    "buffer_pct": 0.001,
                    "buffer_atr": 0.5,
                }
            },
            "hard_stop_buffer_ambiguous",
        ),
        (
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 20,
                    "min_pct": 0.06,
                    "max_pct": 0.05,
                }
            },
            "hard_stop_min_pct_gt_max_pct",
        ),
        (
            {"take_profits": [{"type": "risk_multiple", "r": 0}]},
            "take_profit_r_must_be_positive",
        ),
    ],
)
def test_validate_exit_plan_refuse_specs_non_resolues_invalides_si_autorisees(
    exit_plan: dict,
    code: str,
) -> None:
    with pytest.raises(InvalidExitPlanError, match=code):
        validate_exit_plan(exit_plan, allow_unresolved=True)


def test_resolve_exit_plan_none_retourne_trace_vide() -> None:
    resolved, trace = resolve_exit_plan(None, entry_price=100.0, side="LONG")

    assert resolved is None
    assert trace == {}


@pytest.mark.parametrize(
    ("side", "expected_stop"),
    [
        ("LONG", 97.0),
        ("SHORT", 103.0),
    ],
)
def test_resolve_exit_plan_hard_stop_volatility_multiple_par_side(
    side: str,
    expected_stop: float,
) -> None:
    resolved, trace = resolve_exit_plan(
        {"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}},
        entry_price=100.0,
        side=side,  # type: ignore[arg-type]
        reference_volatility=2.0,
    )

    assert resolved is not None
    assert resolved["hard_stop"] == {"type": "price", "price": expected_stop}
    validate_exit_plan(resolved)
    assert trace["hard_stop"] == {
        "spec_type": "volatility_multiple",
        "multiple": 1.5,
        "reference_volatility": 2.0,
        "distance": 3.0,
        "clamped": False,
        "resolved_price": expected_stop,
    }


@pytest.mark.parametrize(
    ("side", "expected_stop"),
    [
        ("LONG", 95.0),
        ("SHORT", 105.0),
    ],
)
def test_resolve_exit_plan_hard_stop_percent_par_side(side: str, expected_stop: float) -> None:
    resolved, trace = resolve_exit_plan(
        {"hard_stop": {"type": "percent", "percent": 0.05}},
        entry_price=100.0,
        side=side,  # type: ignore[arg-type]
    )

    assert resolved is not None
    assert resolved["hard_stop"] == {"type": "price", "price": expected_stop}
    validate_exit_plan(resolved)
    assert trace["hard_stop"]["spec_type"] == "percent"
    assert trace["hard_stop"]["percent"] == 0.05
    assert trace["hard_stop"]["distance"] == 5.0
    assert trace["hard_stop"]["clamped"] is False


def test_resolve_exit_plan_normalise_alias_hard_stop_avant_resolution() -> None:
    resolved, trace = resolve_exit_plan(
        {"stop_loss": {"type": "percent", "percent": 0.05}},
        entry_price=100.0,
        side="LONG",
    )

    assert resolved is not None
    assert resolved["hard_stop"] == {"type": "price", "price": 95.0}
    assert trace["hard_stop"]["spec_type"] == "percent"
    validate_exit_plan(resolved)


@pytest.mark.parametrize(
    ("hard_stop", "expected_error"),
    [
        ({"type": "percent", "percent": 0.01, "min_pct": 0.03}, "hard_stop_below_min_pct"),
        ({"type": "percent", "percent": 0.10, "max_pct": 0.04}, "hard_stop_above_max_pct"),
    ],
)
def test_resolve_exit_plan_refuse_hard_stop_percent_hors_bornes_sans_clamp(
    hard_stop: dict,
    expected_error: str,
) -> None:
    with pytest.raises(InvalidExitPlanError, match=expected_error):
        resolve_exit_plan(
            {"hard_stop": hard_stop},
            entry_price=100.0,
            side="LONG",
        )


@pytest.mark.parametrize(
    ("side", "anchor", "expected_stop", "expected_distance"),
    [
        ("LONG", "swing_low", 91.0, 9.0),
        ("SHORT", "swing_high", 110.0, 10.0),
    ],
)
def test_resolve_exit_plan_hard_stop_structural_par_side(
    side: str,
    anchor: str,
    expected_stop: float,
    expected_distance: float,
) -> None:
    bars = [
        _bar("t1", 100.0, high=105.0, low=96.0),
        _bar("t2", 101.0, high=108.0, low=93.0),
        _bar("t3", 102.0, high=104.0, low=92.0),
        _bar("t4", 103.0, high=107.0, low=94.0),
    ]

    resolved, trace = resolve_exit_plan(
        {
            "hard_stop": {
                "type": "structural",
                "anchor": anchor,
                "window": 3,
                "timeframe": "1h",
                "buffer_pct": 0.01 if side == "LONG" else 0.02,
            },
            "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 0.5}],
        },
        entry_price=100.0,
        side=side,  # type: ignore[arg-type]
        bars=bars,
    )

    assert resolved is not None
    assert resolved["hard_stop"] == {"type": "price", "price": expected_stop}
    expected_tp = (
        100.0 + expected_distance * 2.0
        if side == "LONG"
        else 100.0 - expected_distance * 2.0
    )
    assert resolved["take_profits"][0]["price"] == expected_tp
    validate_exit_plan(resolved)
    assert trace["hard_stop"] == {
        "spec_type": "structural",
        "anchor": anchor,
        "window": 3,
        "level": 92.0 if side == "LONG" else 108.0,
        "buffer": 1.0 if side == "LONG" else 2.0,
        "distance": expected_distance,
        "clamped": False,
        "resolved_price": expected_stop,
    }


def test_resolve_exit_plan_hard_stop_structural_buffer_atr() -> None:
    resolved, trace = resolve_exit_plan(
        {
            "hard_stop": {
                "type": "structural",
                "anchor": "swing_low",
                "window": 2,
                "buffer_atr": 0.5,
            }
        },
        entry_price=100.0,
        side="LONG",
        reference_volatility=4.0,
        bars=[
            _bar("t1", 100.0, high=102.0, low=96.0),
            _bar("t2", 100.0, high=101.0, low=94.0),
        ],
    )

    assert resolved is not None
    assert resolved["hard_stop"] == {"type": "price", "price": 92.0}
    validate_exit_plan(resolved)
    assert trace["hard_stop"]["buffer"] == 2.0
    assert trace["hard_stop"]["reference_volatility"] == 4.0


@pytest.mark.parametrize(
    ("hard_stop", "expected_error"),
    [
        (
            {"type": "structural", "anchor": "swing_low", "window": 1, "min_pct": 0.05},
            "hard_stop_below_min_pct",
        ),
        (
            {"type": "structural", "anchor": "swing_low", "window": 1, "max_pct": 0.08},
            "hard_stop_above_max_pct",
        ),
    ],
)
def test_resolve_exit_plan_refuse_hard_stop_structural_hors_bornes_sans_clamp(
    hard_stop: dict,
    expected_error: str,
) -> None:
    structural_low = 98.0 if "min_pct" in hard_stop else 80.0
    bars = [_bar("t1", 100.0, high=101.0, low=structural_low)]

    with pytest.raises(InvalidExitPlanError, match=expected_error):
        resolve_exit_plan(
            {"hard_stop": hard_stop},
            entry_price=100.0,
            side="LONG",
            bars=bars,
        )


def test_resolve_exit_plan_refuse_structural_mauvais_cote() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_structural_wrong_side"):
        resolve_exit_plan(
            {"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 2}},
            entry_price=100.0,
            side="LONG",
            bars=[
                _bar("t1", 103.0, high=105.0, low=101.0),
                _bar("t2", 104.0, high=106.0, low=102.0),
            ],
        )


@pytest.mark.parametrize(
    ("anchor", "bars"),
    [
        (
            "swing_low",
            [
                _bar("t1", 103.0, high=105.0, low=101.0),
                _bar("t2", 104.0, high=106.0, low=102.0),
            ],
        ),
        (
            "vwap",
            [
                _bar("t1", 102.0, high=103.0, low=101.0),
                _bar("t2", 102.0, high=103.0, low=101.0),
            ],
        ),
    ],
)
def test_resolve_exit_plan_refuse_structural_niveau_mauvais_cote_avant_buffer(
    anchor: str,
    bars: list[Bar],
) -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_structural_wrong_side"):
        resolve_exit_plan(
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": anchor,
                    "window": 2,
                    "buffer_pct": 0.03,
                }
            },
            entry_price=100.0,
            side="LONG",
            bars=bars,
        )


def test_resolve_exit_plan_refuse_structural_sans_barres() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_bars_unavailable"):
        resolve_exit_plan(
            {"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 2}},
            entry_price=100.0,
            side="LONG",
        )


def test_resolve_exit_plan_refuse_structural_level_indisponible() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_level_unavailable"):
        resolve_exit_plan(
            {"hard_stop": {"type": "structural", "anchor": "vwap", "window": 2}},
            entry_price=100.0,
            side="LONG",
            bars=[
                _bar("t1", 95.0, high=96.0, low=94.0, volume=0.0),
                _bar("t2", 96.0, high=97.0, low=95.0, volume=0.0),
            ],
        )


def test_resolve_exit_plan_refuse_structural_buffer_atr_sans_volatilite_reference() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_buffer_volatility_unavailable"):
        resolve_exit_plan(
            {
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 2,
                    "buffer_atr": 0.5,
                }
            },
            entry_price=100.0,
            side="LONG",
            bars=[_bar("t1", 100.0, high=102.0, low=94.0)],
        )


def test_resolve_exit_plan_trace_volatility_multiple_conserve_champs_spec() -> None:
    resolved, trace = resolve_exit_plan(
        {
            "hard_stop": {
                "type": "volatility_multiple",
                "multiple": 2.0,
                "source": "atr",
                "timeframe": "15m",
                "window": 14,
                "min_pct": 0.01,
                "max_pct": 0.10,
            }
        },
        entry_price=100.0,
        side="LONG",
        reference_volatility=2.0,
    )

    assert resolved is not None
    validate_exit_plan(resolved)
    assert trace["hard_stop"] == {
        "spec_type": "volatility_multiple",
        "multiple": 2.0,
        "reference_volatility": 2.0,
        "source": "atr",
        "timeframe": "15m",
        "window": 14,
        "min_pct": 0.01,
        "max_pct": 0.10,
        "distance": 4.0,
        "clamped": False,
        "resolved_price": 96.0,
    }


def test_resolve_exit_plan_refuse_volatility_multiple_sans_volatilite_reference() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_volatility_unavailable"):
        resolve_exit_plan(
            {"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}},
            entry_price=100.0,
            side="LONG",
        )


@pytest.mark.parametrize(
    ("side", "expected_tp"),
    [
        ("LONG", 106.0),
        ("SHORT", 94.0),
    ],
)
def test_resolve_exit_plan_take_profit_risk_multiple_depuis_distance_stop(
    side: str,
    expected_tp: float,
) -> None:
    resolved, trace = resolve_exit_plan(
        {
            "hard_stop": {"type": "percent", "percent": 0.03},
            "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 0.5}],
        },
        entry_price=100.0,
        side=side,  # type: ignore[arg-type]
    )

    assert resolved is not None
    assert resolved["take_profits"][0]["type"] == "price"
    assert resolved["take_profits"][0]["r"] == 2.0
    assert resolved["take_profits"][0]["fraction"] == 0.5
    assert resolved["take_profits"][0]["price"] == expected_tp
    validate_exit_plan(resolved)
    assert trace["take_profits"] == [
        {"spec_type": "risk_multiple", "r": 2.0, "resolved_price": expected_tp},
    ]


def test_resolve_exit_plan_refuse_risk_multiple_sans_stop_resolvable() -> None:
    with pytest.raises(InvalidExitPlanError, match="take_profit_risk_multiple_requires_stop"):
        resolve_exit_plan(
            {"take_profits": [{"type": "risk_multiple", "r": 1.0}]},
            entry_price=100.0,
            side="LONG",
        )


def test_resolve_exit_plan_refuse_take_profit_risk_multiple_resolu_non_positif() -> None:
    with pytest.raises(InvalidExitPlanError, match="take_profit_resolved_non_positive"):
        resolve_exit_plan(
            {
                "hard_stop": {"type": "percent", "percent": 0.20},
                "take_profits": [{"type": "risk_multiple", "r": 6.0}],
            },
            entry_price=100.0,
            side="SHORT",
        )


def test_resolve_exit_plan_passthrough_prix_et_trace_price() -> None:
    raw = {
        "hard_stop": {"type": "price", "price": 95.0},
        "take_profits": [{"name": "tp1", "price": 105.0, "fraction": 1.0}],
        "trailing_stop": {"trail_type": "price", "trail_value": 1.0},
        "max_hold_minutes": 30,
    }

    resolved, trace = resolve_exit_plan(raw, entry_price=100.0, side="LONG")

    assert resolved == raw
    validate_exit_plan(resolved)
    assert trace["hard_stop"] == {
        "spec_type": "price",
        "distance": 5.0,
        "resolved_price": 95.0,
    }
    assert trace["take_profits"] == [
        {"spec_type": "price", "resolved_price": 105.0},
    ]


def test_resolve_exit_plan_refuse_hard_stop_resolu_non_positif() -> None:
    with pytest.raises(InvalidExitPlanError, match="hard_stop_resolved_non_positive"):
        resolve_exit_plan(
            {"hard_stop": {"type": "percent", "percent": 1.0}},
            entry_price=100.0,
            side="LONG",
        )


def test_create_trade_plan_refuse_volatility_multiple_sans_volatilite_reference() -> None:
    with pytest.raises(InvalidExitPlanError, match="trailing_volatility_unavailable"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
            },
        )


def test_create_trade_plan_refuse_trailing_stop_non_fini() -> None:
    with pytest.raises(InvalidExitPlanError, match="trailing_stop_trail_value_must_be_finite"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": float("inf")},
            },
        )


def test_create_trade_plan_refuse_reference_volatility_non_finie() -> None:
    with pytest.raises(InvalidExitPlanError, match="reference_volatility_must_be_finite"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
            },
            reference_volatility=float("nan"),
        )


def test_trade_plan_from_dict_ignore_les_non_finis_au_reload() -> None:
    plan = trade_plan_from_dict(
        {
            "id": "SPY-nonfinite",
            "symbol": "SPY",
            "side": "LONG",
            "quantity": 10.0,
            "remaining_quantity": 10.0,
            "entry_price": 100.0,
            "opened_at": "2026-06-05T12:00:00+00:00",
            "reference_volatility": float("nan"),
            "trailing_stop": {
                "enabled_after": None,
                "trail_type": "price",
                "trail_value": float("inf"),
            },
        }
    )

    assert plan.reference_volatility is None
    assert plan.trailing_stop is None


def test_trade_plan_store_persiste_reference_volatility(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
        },
        reference_volatility=1.25,
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.reference_volatility == pytest.approx(1.25)
    assert reloaded == plan


def test_create_trade_plan_clampe_trailing_percent_sous_plancher_volatilite() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "percent", "trail_value": 0.005},
        },
        reference_volatility=1.0,
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is True
    assert plan.trailing_stop.trail_value == pytest.approx(0.01)
    assert exit_engine._trail_amount(plan) == pytest.approx(1.0)


def test_create_trade_plan_refuse_entry_price_zero_avant_clamp_trailing_percent() -> None:
    with pytest.raises(InvalidExitPlanError, match="entry_price_must_be_positive"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=0.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "percent", "trail_value": 0.01},
            },
            reference_volatility=1.0,
        )


def test_create_trade_plan_ne_clampe_pas_trailing_au_dessus_du_plancher() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "percent", "trail_value": 0.02},
        },
        reference_volatility=1.0,
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is False
    assert plan.trailing_stop.trail_value == pytest.approx(0.02)
    assert exit_engine._trail_amount(plan) == pytest.approx(2.0)


def test_create_trade_plan_ne_clampe_pas_sans_volatilite_reference() -> None:
    plan = create_trade_plan(
        symbol="EURUSD=X",
        side="LONG",
        quantity=10.0,
        entry_price=1.10,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "price", "trail_value": 0.0007},
        },
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is False
    assert plan.trailing_stop.trail_value == pytest.approx(0.0007)


def test_trade_plan_store_persiste_les_plans_ouverts(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()
    assert reloaded == [plan]


def test_trade_plan_store_persiste_profit_protection(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0, "profit_protection": True},
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.profit_protection == plan.profit_protection


def test_trade_plan_store_persiste_exit_watch(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": {
                "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
            },
        },
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.exit_watch == plan.exit_watch


def test_trade_plan_store_persiste_last_llm_review(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )
    plan = replace(
        plan,
        last_llm_review={
            "ts": "2026-06-05T12:15:00+00:00",
            "verdict": "intact",
            "action": "HOLD",
            "intent": "HOLD",
        },
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.last_llm_review == plan.last_llm_review


def test_trade_plan_store_persiste_le_contexte_d_entree(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )
    plan = replace(
        plan,
        entry_thesis="cassure du range haut sur volume",
        entry_decision_id="2026-06-05T12:00:00+00:00|0|SPY",
        entry_context={
            "price": 100.0,
            "runtime_interval": "15m",
            "data_age_m": 3,
            "session": {"open": True},
            "daily_as_of": "2026-06-04",
        },
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.entry_thesis == "cassure du range haut sur volume"
    assert reloaded.entry_decision_id == "2026-06-05T12:00:00+00:00|0|SPY"
    assert reloaded.entry_context == plan.entry_context


def test_trade_plan_store_cloture_un_plan(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = TradePlan(
        id="SPY-2026",
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        remaining_quantity=10.0,
        entry_price=100.0,
        opened_at=datetime(2026, 6, 5, 12, tzinfo=timezone.utc).isoformat(),
    )
    store.upsert(plan)

    store.close(plan.id)

    assert store.open_plans() == []
