from __future__ import annotations

import pytest

from trader.market.radar_config import RadarParams
from trader.market.radar_shadow import (
    build_score_audit,
    build_shadow_rankings,
    percentile_ranks,
)


def _venue_for(symbol: str) -> str:
    return symbol.split("_", 1)[0]


def _family_for(symbol: str) -> str:
    return f"{_venue_for(symbol).lower()}_family"


def test_percentile_ranks_are_deterministic_and_average_ties() -> None:
    ranks = percentile_ranks({"B": 2.0, "A": 1.0, "D": 2.0, "C": 4.0})

    assert ranks == {
        "A": 0.0,
        "B": pytest.approx(0.5),
        "D": pytest.approx(0.5),
        "C": 1.0,
    }


def test_shadow_ranking_balances_trend_and_relative_strength_by_venue() -> None:
    current = [
        {"symbol": "US_TREND", "attractiveness": 0.9, "bias": "long"},
        {"symbol": "US_RS", "attractiveness": 0.5, "bias": "long"},
        {"symbol": "US_SHORT", "attractiveness": 0.4, "bias": "short"},
    ]
    components = {
        "US_TREND": {"efficiency_ratio": 0.9, "ret": 0.02, "amplitude": 0.02},
        "US_RS": {"efficiency_ratio": 0.1, "ret": 0.30, "amplitude": 0.02},
        "US_SHORT": {"efficiency_ratio": 0.5, "ret": -0.20, "amplitude": 0.02},
    }

    ranked = build_shadow_rankings(
        current_ranked=current,
        components_by_symbol=components,
        benchmark_return_by_symbol={symbol: 0.0 for symbol in components},
        tilt_by_symbol={},
        venue_for=_venue_for,
        family_for=_family_for,
    )["US"]

    assert {row["symbol"] for row in ranked} == set(components)
    by_symbol = {row["symbol"]: row for row in ranked}
    assert by_symbol["US_TREND"]["components"]["trend_percentile"] == 1.0
    assert by_symbol["US_RS"]["components"]["relative_strength_percentile"] == 1.0
    assert by_symbol["US_SHORT"]["bias"] == "short"
    assert by_symbol["US_SHORT"]["directional_score"] < 0


def test_shadow_amplitude_is_eligibility_only() -> None:
    current = [
        {"symbol": "US_LOW_AMP", "attractiveness": 0.5, "bias": "long"},
        {"symbol": "US_HIGH_AMP", "attractiveness": 0.6, "bias": "long"},
    ]
    components = {
        "US_LOW_AMP": {"efficiency_ratio": 0.5, "ret": 0.05, "amplitude": 0.01},
        "US_HIGH_AMP": {"efficiency_ratio": 0.5, "ret": 0.05, "amplitude": 0.20},
    }

    ranked = build_shadow_rankings(
        current_ranked=current,
        components_by_symbol=components,
        benchmark_return_by_symbol={},
        tilt_by_symbol={},
        venue_for=_venue_for,
        family_for=_family_for,
    )["US"]

    assert ranked[0]["attractiveness"] == pytest.approx(ranked[1]["attractiveness"])
    assert {
        row["components"]["amplitude_role"] for row in ranked
    } == {"eligibility_only"}


def test_score_audit_exposes_trend_dominance_and_short_sign_mismatch() -> None:
    current = [
        {"symbol": "US_LONG", "attractiveness": 0.9, "bias": "long"},
        {"symbol": "US_SHORT", "attractiveness": 0.8, "bias": "short"},
    ]
    components = {
        "US_LONG": {"efficiency_ratio": 0.9, "ret": 0.04, "amplitude": 0.02},
        "US_SHORT": {"efficiency_ratio": 0.8, "ret": -0.10, "amplitude": 0.03},
    }

    audit = build_score_audit(
        as_of="2026-07-10T00:00:00+00:00",
        current_ranked=current,
        components_by_symbol=components,
        benchmark_return_by_symbol={"US_LONG": 0.01, "US_SHORT": 0.02},
        tilt_by_symbol={},
        venue_for=_venue_for,
        family_for=_family_for,
        params=RadarParams(),
        top_k=1,
    )

    balance = audit["venues"]["US"]["component_balance"]
    assert audit["status"] == "shadow_only"
    assert audit["selection_effect"] == "none"
    assert balance["median_abs_trend"] > balance["median_abs_relative_strength"]
    assert balance["short_count"] == 1
    assert balance["short_relative_strength_offset_count"] == 1
    assert audit["venues"]["US"]["current_top"] == ["US_LONG"]
    assert len(audit["venues"]["US"]["shadow_top"]) == 1


def test_shadow_weights_reject_an_empty_combination() -> None:
    with pytest.raises(ValueError, match="shadow_weights"):
        build_shadow_rankings(
            current_ranked=[],
            components_by_symbol={},
            benchmark_return_by_symbol={},
            tilt_by_symbol={},
            venue_for=_venue_for,
            family_for=_family_for,
            trend_weight=0.0,
            relative_strength_weight=0.0,
        )
