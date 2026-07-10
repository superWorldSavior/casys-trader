from __future__ import annotations

import pytest

from trader.domain.market_data import Bar
from trader.reporting.bench.radar_score import (
    evaluate_forward_performance,
    evaluate_selection_stability,
)


def _bar(day: int, close: float) -> Bar:
    return Bar(
        ts=f"2026-07-{day:02d}T00:00:00+00:00",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000.0,
    )


def test_forward_performance_respects_long_and_short_direction() -> None:
    snapshots = [
        {
            "venues": {
                "US": {
                    "production": [
                        {"symbol": "LONG", "bias": "long", "family": "us_tech"}
                    ],
                    "shadow": [
                        {"symbol": "SHORT", "bias": "short", "family": "us_tech"}
                    ],
                }
            },
            "base_ts_by_symbol": {
                "LONG": "2026-07-01T00:00:00+00:00",
                "SHORT": "2026-07-01T00:00:00+00:00",
            },
        }
    ]
    bars = {
        "LONG": [_bar(1, 100.0), _bar(2, 110.0), _bar(3, 121.0)],
        "SHORT": [_bar(1, 100.0), _bar(2, 90.0), _bar(3, 81.0)],
    }

    result = evaluate_forward_performance(snapshots, bars_by_symbol=bars, horizons=(1, 2))

    assert result["production"]["US"]["1"]["mean_directional_return"] == pytest.approx(
        0.10
    )
    assert result["shadow"]["US"]["1"]["mean_directional_return"] == pytest.approx(0.10)
    assert result["shadow"]["US"]["2"]["mean_directional_return"] == pytest.approx(0.19)
    assert result["shadow"]["US"]["2"]["hit_rate"] == 1.0


def test_selection_stability_measures_turnover_and_family_concentration() -> None:
    snapshots = [
        {
            "venues": {
                "US": {
                    "production": [
                        {"symbol": "A", "bias": "long", "family": "tech"},
                        {"symbol": "B", "bias": "short", "family": "tech"},
                    ],
                    "shadow": [
                        {"symbol": "A", "bias": "long", "family": "tech"},
                        {"symbol": "C", "bias": "long", "family": "health"},
                    ],
                }
            }
        },
        {
            "venues": {
                "US": {
                    "production": [
                        {"symbol": "A", "bias": "long", "family": "tech"},
                        {"symbol": "D", "bias": "long", "family": "health"},
                    ],
                    "shadow": [
                        {"symbol": "A", "bias": "long", "family": "tech"},
                        {"symbol": "C", "bias": "long", "family": "health"},
                    ],
                }
            }
        },
    ]

    result = evaluate_selection_stability(snapshots)

    assert result["production"]["US"]["mean_turnover"] == pytest.approx(0.5)
    assert result["shadow"]["US"]["mean_turnover"] == 0.0
    assert result["production"]["US"]["mean_long_share"] == pytest.approx(0.75)
    assert result["production"]["US"]["mean_largest_family_share"] == pytest.approx(
        0.75
    )
