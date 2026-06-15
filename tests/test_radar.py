import pytest
import json

from trader.radar import (
    build_radar_snapshot,
    daily_components,
    is_eligible,
    scan_and_rank,
    score_symbol,
    write_snapshot,
)
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


def test_scan_and_rank_records_ineligible_symbols() -> None:
    bars_by_symbol = {"SPY": "barsA", "ZZZ": "barsB", "CL=F": "barsC"}

    def indicators_fn(symbol: str, bars: object) -> dict:
        return {
            "SPY": {"efficiency_ratio": 0.8, "ret": 0.03, "amplitude": 0.03},
            "ZZZ": {"efficiency_ratio": 0.9, "ret": 0.05, "amplitude": 0.001},
            "CL=F": {"efficiency_ratio": 0.7, "ret": 0.04, "amplitude": 0.05},
        }[symbol]

    result = scan_and_rank(
        bars_by_symbol,
        indicators_fn=indicators_fn,
        benchmark_ret_for={"SPY": 0.0, "ZZZ": 0.0, "CL=F": 0.0},
        tilt_for=lambda symbol: 0.0,
        hard_exclusions={"CL=F"},
        atr_floor=0.01,
        amplitude_cap=0.05,
        w_trend=1.0,
        w_rs=1.0,
        w_amp=1.0,
    )

    assert [row["symbol"] for row in result["ranked"]] == ["SPY"]
    assert {row["symbol"] for row in result["ineligible"]} == {"ZZZ", "CL=F"}


def test_scan_and_rank_orders_by_attractiveness_not_raw_signed_score() -> None:
    bars_by_symbol = {"STRONG_SHORT": "barsA", "WEAK_LONG": "barsB"}

    def indicators_fn(symbol: str, bars: object) -> dict:
        return {
            "STRONG_SHORT": {
                "efficiency_ratio": 0.95,
                "ret": -0.10,
                "amplitude": 0.04,
            },
            "WEAK_LONG": {
                "efficiency_ratio": 0.10,
                "ret": 0.01,
                "amplitude": 0.02,
            },
        }[symbol]

    result = scan_and_rank(
        bars_by_symbol,
        indicators_fn=indicators_fn,
        benchmark_ret_for={"STRONG_SHORT": 0.0, "WEAK_LONG": 0.0},
        tilt_for=lambda symbol: 0.0,
        hard_exclusions=set(),
        atr_floor=0.01,
        amplitude_cap=0.05,
        w_trend=1.0,
        w_rs=1.0,
        w_amp=1.0,
    )

    ranked = result["ranked"]
    assert [row["symbol"] for row in ranked] == ["STRONG_SHORT", "WEAK_LONG"]
    assert ranked[0]["directional_score"] < 0
    assert ranked[0]["bias"] == "short"
    assert ranked[0]["attractiveness"] == pytest.approx(
        abs(ranked[0]["directional_score"])
    )
    assert ranked[1]["bias"] == "long"


def test_build_radar_snapshot_and_write_snapshot(tmp_path) -> None:
    ranked = [
        {
            "symbol": "SPY",
            "directional_score": 1.2,
            "attractiveness": 1.2,
            "bias": "long",
        }
    ]
    ineligible = [{"symbol": "CL=F", "reason": "hard_exclusion"}]
    components_by_symbol = {
        "SPY": {"efficiency_ratio": 0.8, "ret": 0.03, "amplitude": 0.02},
        "CL=F": {"efficiency_ratio": 0.7, "ret": 0.04, "amplitude": 0.05},
    }

    snapshot = build_radar_snapshot(
        ranked,
        ineligible,
        as_of="2026-06-15",
        components_by_symbol=components_by_symbol,
    )

    assert snapshot["as_of"] == "2026-06-15"
    assert snapshot["ranked"] == ranked
    assert snapshot["ineligible"] == ineligible
    assert snapshot["components_by_symbol"] == components_by_symbol

    write_snapshot(tmp_path, snapshot)

    assert json.loads((tmp_path / "radar_snapshot.json").read_text()) == snapshot
