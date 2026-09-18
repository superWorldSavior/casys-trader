from __future__ import annotations

import pytest

from trader.domain.market_data import Bar
from trader.reporting.bench.radar_score import (
    evaluate_forward_performance,
    evaluate_selection_stability,
    run_cache_bench,
    write_bench,
)
from trader.reporting.read_models.runtime_state import load_state


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


def test_bench_payload_schema_matches_runtime_state_reader(tmp_path) -> None:
    """The keys runtime_state/cockpit consume survive a write→load round trip."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "pool.yaml").write_text("symbols: [AAPL]\n", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    payload = run_cache_bench(config_dir=config_dir, cache_dir=cache_dir)

    assert payload["schema_version"] == 1
    assert payload["status"] == "insufficient_history"
    assert payload["production_switch_recommended"] is False
    assert payload["coverage"]["unique_snapshot_count"] == 0

    output = tmp_path / "bench.json"
    write_bench(output, payload)
    reloaded = load_state(output)
    assert isinstance(reloaded, dict)
    assert reloaded["status"] == "insufficient_history"
    assert reloaded["coverage"]["unique_snapshot_count"] == 0
