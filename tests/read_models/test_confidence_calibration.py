import gzip
import json

import pytest

from trader.reporting.read_models import confidence_calibration
from trader.reporting.read_models.confidence_calibration import (
    build_confidence_calibration,
    build_confidence_calibration_from_rows,
    compact_confidence_calibration,
)


def _trip(decision_id: str, confidence: float, pnl: float, *, side: str = "LONG"):
    return {
        "entry_decision_id": decision_id,
        "entry_decision_ids": [decision_id],
        "entry_confidence": confidence,
        "gross_pnl": pnl,
        "commission": 0.0,
        "pnl": pnl,
        "commission_quality": {
            "status": "available",
            "reason": None,
            "models": [],
            "reasons": [],
        },
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
    assert compact["commission_quality"]["status"] == "available"
    assert compact["detail_scope"] == "confidence_calibration"


def test_incomplete_commission_cycle_is_excluded_with_explicit_quality() -> None:
    unavailable = {
        **_trip("d2", 0.9, -20.0),
        "pnl": None,
        "commission": None,
        "commission_quality": {
            "status": "unavailable",
            "reason": "commission_not_modeled",
            "models": ["none"],
            "reasons": ["commission_not_modeled"],
        },
    }

    result = build_confidence_calibration_from_rows(
        [_trip("d1", 0.6, 10.0), unavailable],
        [_decision("d1"), _decision("d2")],
        min_cohort_size=1,
    )

    assert result["n"] == 1
    assert result["commission_quality"] == {
        "status": "unavailable",
        "reason": "commission_not_modeled",
        "counts": {"total": 2, "available": 1, "unavailable": 1},
        "models": ["none"],
        "model_counts": {"none": 1},
        "reasons": ["commission_not_modeled"],
        "reason_counts": {"commission_not_modeled": 1},
    }
    assert result["quality"]["status"] == "partial"


@pytest.mark.parametrize("failure", ["directory", "invalid_utf8"])
def test_decision_history_read_errors_are_advisory_and_explicit(
    tmp_path, failure: str, monkeypatch
) -> None:
    decisions_path = tmp_path / "decisions.jsonl"
    if failure == "directory":
        decisions_path.mkdir()
    else:
        decisions_path.write_bytes(b"\xff\xfe")

    monkeypatch.setattr(
        confidence_calibration,
        "compute_round_trips",
        lambda _: [_trip("d1", 0.6, 10.0)],
    )

    result = build_confidence_calibration(tmp_path, min_cohort_size=1)

    assert result["n"] == 1
    assert result["quality"]["status"] == "unavailable"
    assert result["decision_rows_quality"]["status"] == "unavailable"
    assert result["decision_rows_quality"]["reason"] == (
        "decision_ledger_unreadable"
    )


def test_decision_history_reads_referenced_ids_from_gzip_archive(
    tmp_path, monkeypatch
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    with gzip.open(
        archive / "decisions-2026-06.jsonl.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(_decision("d1", setup="archived_breakout")) + "\n")
    monkeypatch.setattr(
        confidence_calibration,
        "compute_round_trips",
        lambda _: [_trip("d1", 0.6, 10.0)],
    )

    result = build_confidence_calibration(tmp_path, min_cohort_size=1)

    fine = next(
        row
        for row in result["cohorts"]
        if row["level"] == "setup+regime+side+horizon"
    )
    assert fine["dimensions"]["setup"] == "archived_breakout"
    assert result["decision_rows_quality"]["status"] == "available"
    assert result["decision_rows_quality"]["rows_found"] == 1


def test_live_decision_deduplicates_and_supersedes_archived_copy(
    tmp_path, monkeypatch
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    with gzip.open(
        archive / "decisions-2026-06.jsonl.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(_decision("d1", setup="old_setup")) + "\n")
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps(_decision("d1", setup="live_setup")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        confidence_calibration,
        "compute_round_trips",
        lambda _: [_trip("d1", 0.6, 10.0)],
    )

    result = build_confidence_calibration(tmp_path, min_cohort_size=1)

    fine = next(
        row
        for row in result["cohorts"]
        if row["level"] == "setup+regime+side+horizon"
    )
    assert fine["dimensions"]["setup"] == "live_setup"
    assert result["decision_rows_quality"]["duplicate_rows"] == 1
