import pytest
import json
import math

from trader.market.radar import (
    build_radar_snapshot,
    daily_components,
    is_eligible,
    scan_and_rank,
    score_symbol,
    write_snapshot,
)
from trader.market.radar_config import RadarParams
from trader.market.market_data import Bar


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


def test_daily_components_window_est_score_window_et_decouple_de_dwell() -> None:
    # 20 barres, close = 100 + jour : fenetre de scoring verifiable a la main.
    bars = [
        Bar(
            ts=f"2026-06-{d:02d}T00:00:00+00:00",
            open=100.0 + d,
            high=101.0 + d,
            low=99.0 + d,
            close=100.0 + d,
            volume=1_000.0,
        )
        for d in range(20)
    ]
    # close[-1]=119 ; fenetre de 10 barres -> closes 110..119 -> ret = 119/110 - 1.
    c10 = daily_components("SPY", bars, RadarParams(score_window_bars=10, dwell_days=3))
    assert c10["ret"] == pytest.approx(119 / 110 - 1, rel=1e-3)
    # Decouplage : dwell_days (hysteresis) ne doit PAS bouger le score.
    c10_bis = daily_components("SPY", bars, RadarParams(score_window_bars=10, dwell_days=8))
    assert c10_bis["ret"] == pytest.approx(c10["ret"], rel=1e-9)
    # Une fenetre plus courte change le rendement -> c'est bien score_window qui pilote.
    c5 = daily_components("SPY", bars, RadarParams(score_window_bars=5))
    assert c5["ret"] == pytest.approx(119 / 115 - 1, rel=1e-3)


def test_daily_components_ignores_trailing_nan_close() -> None:
    bars = [
        Bar(
            ts=f"2026-06-{day:02d}T00:00:00+00:00",
            open=100.0 + day,
            high=102.0 + day,
            low=99.0 + day,
            close=100.0 + day,
            volume=1_000.0,
        )
        for day in range(1, 8)
    ]
    bars.append(
        Bar(
            ts="2026-06-08T00:00:00+00:00",
            open=108.0,
            high=109.0,
            low=107.0,
            close=float("nan"),
            volume=0.0,
        )
    )

    components = daily_components("SPY", bars, RadarParams(score_window_bars=5))

    assert components["ret"] == pytest.approx(107 / 103 - 1, rel=1e-3)
    assert math.isfinite(components["efficiency_ratio"])
    assert math.isfinite(components["ret"])
    assert math.isfinite(components["amplitude"])


def test_daily_components_all_nan_closes_never_returns_nan() -> None:
    bars = [
        Bar(
            ts=f"2026-06-{day:02d}T00:00:00+00:00",
            open=100.0,
            high=101.0,
            low=99.0,
            close=float("nan"),
            volume=0.0,
        )
        for day in range(1, 4)
    ]

    components = daily_components("SPY", bars, RadarParams(score_window_bars=3))

    assert components == {"efficiency_ratio": None, "ret": None, "amplitude": None}


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


def test_hard_exclusion_skips_indicators_fn() -> None:
    """A. indicators_fn NE DOIT PAS être appelé sur un symbole hard_exclusion."""
    bars_by_symbol = {"SPY": "barsA", "CL=F": "barsB"}

    def indicators_fn(symbol: str, bars: object) -> dict:
        if symbol == "CL=F":
            raise RuntimeError("indicators_fn appelé sur un symbole exclu !")
        return {"efficiency_ratio": 0.8, "ret": 0.03, "amplitude": 0.03}

    result = scan_and_rank(
        bars_by_symbol,
        indicators_fn=indicators_fn,
        benchmark_ret_for={"SPY": 0.0},
        tilt_for=lambda symbol: 0.0,
        hard_exclusions={"CL=F"},
        atr_floor=0.01,
        amplitude_cap=0.05,
        w_trend=1.0,
        w_rs=1.0,
        w_amp=1.0,
    )

    # CL=F doit apparaître dans ineligible avec reason="hard_exclusion"
    ineligible_syms = {row["symbol"]: row["reason"] for row in result["ineligible"]}
    assert "CL=F" in ineligible_syms
    assert ineligible_syms["CL=F"] == "hard_exclusion"
    # SPY reste dans ranked
    assert any(row["symbol"] == "SPY" for row in result["ranked"])


def test_partial_data_none_ret_is_ineligible_missing_data() -> None:
    """B. indicators_fn retournant ret=None → inéligible missing_data, pas de crash."""
    bars_by_symbol = {"GOOD": "barsA", "BAD_DATA": "barsB"}

    def indicators_fn(symbol: str, bars: object) -> dict:
        if symbol == "BAD_DATA":
            return {"efficiency_ratio": 0.8, "ret": None, "amplitude": 0.05}
        return {"efficiency_ratio": 0.8, "ret": 0.03, "amplitude": 0.03}

    result = scan_and_rank(
        bars_by_symbol,
        indicators_fn=indicators_fn,
        benchmark_ret_for={"GOOD": 0.0, "BAD_DATA": 0.0},
        tilt_for=lambda symbol: 0.0,
        hard_exclusions=set(),
        atr_floor=0.01,
        amplitude_cap=0.05,
        w_trend=1.0,
        w_rs=1.0,
        w_amp=1.0,
    )

    ineligible_syms = {row["symbol"]: row["reason"] for row in result["ineligible"]}
    assert "BAD_DATA" in ineligible_syms
    assert ineligible_syms["BAD_DATA"] == "missing_data"
    # BAD_DATA absent du ranked
    assert not any(row["symbol"] == "BAD_DATA" for row in result["ranked"])
    # GOOD reste ranked
    assert any(row["symbol"] == "GOOD" for row in result["ranked"])


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
