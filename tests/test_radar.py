import pytest

from trader.radar import daily_components, is_eligible, score_symbol
from trader.radar_config import RadarParams
from trader.tools.market import Bar


def test_excluded_symbol_not_eligible() -> None:
    assert not is_eligible(
        "CL=F",
        amplitude=0.10,
        hard_exclusions={"CL=F"},
        atr_floor=0.01,
    )


def test_too_calm_not_eligible() -> None:
    assert not is_eligible(
        "SPY",
        amplitude=0.002,
        hard_exclusions=set(),
        atr_floor=0.01,
    )


def test_missing_amplitude_not_eligible() -> None:
    assert not is_eligible(
        "SPY",
        amplitude=None,
        hard_exclusions=set(),
        atr_floor=0.01,
    )


def test_tradable_is_eligible() -> None:
    assert is_eligible(
        "SPY",
        amplitude=0.03,
        hard_exclusions=set(),
        atr_floor=0.01,
    )


def _base_score_inputs() -> dict:
    return {
        "efficiency_ratio": 0.5,
        "ret": 0.02,
        "benchmark_ret": 0.0,
        "amplitude": 0.02,
        "tilt": 0.0,
        "amplitude_cap": 0.05,
        "w_trend": 1.0,
        "w_rs": 1.0,
        "w_amp": 1.0,
    }


def test_cleaner_trend_scores_higher() -> None:
    low = score_symbol(**{**_base_score_inputs(), "efficiency_ratio": 0.2})
    high = score_symbol(**{**_base_score_inputs(), "efficiency_ratio": 0.9})

    assert high > low


def test_beating_benchmark_scores_higher() -> None:
    lag = score_symbol(
        **{**_base_score_inputs(), "ret": 0.00, "benchmark_ret": 0.02}
    )
    lead = score_symbol(
        **{**_base_score_inputs(), "ret": 0.04, "benchmark_ret": 0.02}
    )

    assert lead > lag


def test_amplitude_rewarded_but_saturates() -> None:
    calm = score_symbol(**{**_base_score_inputs(), "amplitude": 0.02})
    loud = score_symbol(**{**_base_score_inputs(), "amplitude": 0.04})
    capped = score_symbol(**{**_base_score_inputs(), "amplitude": 0.05})
    beyond_cap = score_symbol(**{**_base_score_inputs(), "amplitude": 0.20})

    assert loud > calm
    assert beyond_cap == pytest.approx(capped)


def test_positive_tilt_increases_score() -> None:
    assert score_symbol(**{**_base_score_inputs(), "tilt": 0.30}) > score_symbol(
        **_base_score_inputs()
    )


def test_sign_follows_direction() -> None:
    up = score_symbol(**{**_base_score_inputs(), "ret": 0.03})
    down = score_symbol(**{**_base_score_inputs(), "ret": -0.03})

    assert up > 0 > down


def test_daily_components_adapte_compute_indicator_values_sur_de_vraies_barres() -> None:
    bars = [
        Bar(
            ts=f"2026-06-{day:02d}T00:00:00+00:00",
            open=100.0 + day * 2.0,
            high=110.0 + day * 2.0,
            low=96.0 + day * 2.0,
            close=101.0 + day * 2.5,
            volume=1_000.0 + day,
        )
        for day in range(1, 8)
    ]

    components = daily_components("SPY", bars, RadarParams())

    assert set(components) == {"efficiency_ratio", "ret", "amplitude"}
    assert components["efficiency_ratio"] is not None
    assert components["ret"] > 0
    assert components["amplitude"] > 0
