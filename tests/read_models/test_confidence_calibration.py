import pytest

from trader.reporting.read_models.confidence_calibration import (
    build_confidence_calibration_from_rows,
    compact_confidence_calibration,
)


def _trip(decision_id: str, confidence: float, pnl: float, *, side: str = "LONG"):
    return {
        "entry_decision_id": decision_id,
        "entry_decision_ids": [decision_id],
        "entry_confidence": confidence,
        "pnl": pnl,
        "side": side,
    }


def _decision(
    decision_id: str,
    *,
    setup: str = "breakout",
    regime: str | None = "trend",
    side: str = "long",
    horizon: str = "swing",
):
    dimensions = {
        "setup": setup,
        "side": side,
        "horizon": horizon,
    }
    if regime is not None:
        dimensions["regime"] = regime
    return {
        "decision_id": decision_id,
        "entry_dimensions": dimensions,
        "thesis": {"setup": setup, "horizon": horizon},
    }


def test_calibration_metrics_and_fine_to_broad_fallback() -> None:
    result = build_confidence_calibration_from_rows(
        [
            _trip("d1", 0.6, 100.0),
            _trip("d2", 0.8, -100.0),
            _trip("d3", 0.7, 50.0, side="SHORT"),
        ],
        [
            _decision("d1"),
            _decision("d2"),
            _decision("d3", setup="reversal", regime="range", side="short"),
        ],
        min_cohort_size=2,
    )

    fine = next(
        row
        for row in result["cohorts"]
        if row["level"] == "setup+regime+side+horizon"
        and row["dimensions"]["setup"] == "breakout"
    )
    assert fine["n"] == 2
    assert fine["mean_confidence"] == 0.7
    assert fine["win_rate"] == 0.5
    assert fine["calibration_gap"] == pytest.approx(-0.2)
    assert fine["avg_pnl"] == 0.0
    short_fallback = next(
        row
        for row in result["fallbacks"]
        if row["requested"]["side"] == "short"
    )
    assert short_fallback["selected_level"] == "all"
    assert short_fallback["selected"]["n"] == 3


def test_missing_historical_regime_stays_unknown() -> None:
    result = build_confidence_calibration_from_rows(
        [_trip("legacy", 0.55, 10.0)],
        [_decision("legacy", regime=None)],
        min_cohort_size=1,
    )

    fine = next(
        row
        for row in result["cohorts"]
        if row["level"] == "setup+regime+side+horizon"
    )
    assert fine["dimensions"]["regime"] == "unknown"


def test_partial_exits_form_one_flat_to_flat_calibration_observation() -> None:
    trips = [
        {
            **_trip("d1", 0.8, 10.0),
            "quantity": 1.0,
            "position_cycle_id": "SPY:1",
            "position_cycle_closed": False,
        },
        {
            **_trip("d1", 0.8, -20.0),
            "quantity": 9.0,
            "position_cycle_id": "SPY:1",
            "position_cycle_closed": True,
        },
        {
            **_trip("d2", 0.7, 5.0),
            "quantity": 2.0,
            "position_cycle_id": "SPY:2",
            "position_cycle_closed": False,
        },
    ]

    result = build_confidence_calibration_from_rows(
        trips,
        [_decision("d1"), _decision("d2")],
        min_cohort_size=1,
    )

    all_cohort = next(
        row for row in result["cohorts"] if row["level"] == "all"
    )
    assert result["n"] == 1
    assert all_cohort["n"] == 1
    assert all_cohort["win_rate"] == 0.0
    assert all_cohort["avg_pnl"] == -10.0
    assert all_cohort["mean_confidence"] == pytest.approx(0.8)


def test_compact_projection_is_bounded() -> None:
    result = build_confidence_calibration_from_rows(
        [_trip("d1", 0.6, 10.0), _trip("d2", 0.8, -5.0)],
        [_decision("d1"), _decision("d2")],
        min_cohort_size=1,
    )

    compact = compact_confidence_calibration(result, max_cohorts=2)

    assert compact["n"] == 2
    assert len(compact["cohorts"]) == 2
    assert compact["detail_scope"] == "confidence_calibration"
