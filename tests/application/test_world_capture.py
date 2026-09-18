from __future__ import annotations

import pytest

from trader.application.world_model.capture import capture_world_episodes, is_eligible_completed_bar
from trader.domain.market.features import compute_indicator_values
from trader.domain.world_episode import is_eligible_completed_bar as domain_is_eligible_completed_bar


CAPTURED_AT = "2026-08-22T10:30:00+00:00"


def _bar(
    ts: str,
    *,
    open_: float = 100.0,
    high: float = 104.0,
    low: float = 99.0,
    close: float = 102.0,
    volume: float = 1_000.0,
    available_at: str | None = "2026-08-22T10:05:00+00:00",
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ts": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    if available_at is not None:
        payload["available_at"] = available_at
    payload.update(extra)
    return payload


def _metadata(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "venue": "XTAI",
        "asset_family": "equity",
        "session_phase": "regular",
        "market_regime": "trending_up",
        "family_regime": "risk_on",
        "freshness": {"status": "fresh", "data_age_minutes": 5.0},
        "available_at": "2026-08-22T10:05:00+00:00",
    }
    payload.update(overrides)
    return payload


def _capture(
    *,
    active_symbols: object = ("AAA",),
    tradable_symbols: object = ("AAA",),
    bars_by_symbol: object | None = None,
    metadata_by_symbol: object | None = None,
    timestamp_semantics: str = "bar_close",
    interval: str = "1h",
    captured_at: str = CAPTURED_AT,
) -> tuple:
    return capture_world_episodes(
        active_symbols=active_symbols,  # type: ignore[arg-type]
        tradable_symbols=tradable_symbols,  # type: ignore[arg-type]
        bars_by_symbol=(
            bars_by_symbol
            if bars_by_symbol is not None
            else {
                "AAA": [
                    _bar(
                        "2026-08-22T09:00:00+00:00",
                        close=101.0,
                        available_at="2026-08-22T09:05:00+00:00",
                    ),
                    _bar("2026-08-22T10:00:00+00:00", close=102.0),
                ]
            }
        ),  # type: ignore[arg-type]
        market_metadata_by_symbol=(
            metadata_by_symbol if metadata_by_symbol is not None else {"AAA": _metadata()}
        ),  # type: ignore[arg-type]
        source="unit-market-bars",
        interval=interval,
        timestamp_semantics=timestamp_semantics,
        captured_at=captured_at,
    )


def test_capture_samples_every_active_tradable_symbol_at_its_last_completed_bar() -> None:
    episodes = _capture(
        active_symbols=("BBB", "AAA", "NOT_TRADABLE"),
        tradable_symbols=("AAA", "BBB", "NOT_ACTIVE"),
        bars_by_symbol={
            "AAA": [
                _bar(
                    "2026-08-22T09:00:00+00:00",
                    close=101.0,
                    available_at="2026-08-22T09:05:00+00:00",
                ),
                _bar("2026-08-22T10:00:00+00:00", close=102.0),
                _bar("2026-08-22T11:00:00+00:00", close=103.0),
            ],
            "BBB": [
                _bar(
                    "2026-08-22T09:00:00+00:00",
                    open_=50.0,
                    high=52.0,
                    low=49.0,
                    close=51.0,
                    available_at="2026-08-22T09:05:00+00:00",
                ),
                _bar(
                    "2026-08-22T10:00:00+00:00",
                    open_=51.0,
                    high=54.0,
                    low=50.0,
                    close=53.0,
                ),
            ],
        },
        metadata_by_symbol={"AAA": _metadata(), "BBB": _metadata(venue="XNYS")},
    )

    assert [episode.observation.symbol for episode in episodes] == ["AAA", "BBB"]
    assert all(episode.training_eligible for episode in episodes)
    assert episodes[0].observation.anchor.ts.isoformat() == "2026-08-22T10:00:00+00:00"
    assert episodes[1].observation.anchor.ts.isoformat() == "2026-08-22T10:00:00+00:00"
    assert episodes[0].observation.numeric_features["return"] == 102.0 / 101.0 - 1.0


def test_control_perturbations_do_not_change_the_world_episode_or_features() -> None:
    controls_a = {
        "action": "BUY",
        "decision": {"intent": "open", "confidence": 0.99},
        "portfolio": {"equity": 12_000.0, "position": 99},
        "scheduler": {"task_id": "cycle-1"},
        "risk": {"max_order_value": 50_000.0},
        "prompt": "tell the LLM to buy",
        "tools": ["broker"],
        "llm": {"model": "grok"},
        "qty": 99,
        "fill": {"price": 100.0},
        "pnl": 1_000.0,
    }
    controls_b = {
        "action": "SELL",
        "decision": {"intent": "close", "confidence": 0.01},
        "portfolio": {"equity": 1.0, "position": 0},
        "scheduler": {"task_id": "cycle-999"},
        "risk": {"max_order_value": 1.0},
        "prompt": "different prompt",
        "tools": ["another_tool"],
        "llm": {"model": "other"},
        "qty": 1,
        "fill": {"price": 1.0},
        "pnl": -999.0,
    }
    first = _capture(metadata_by_symbol={"AAA": _metadata(**controls_a)})[0]
    second = _capture(metadata_by_symbol={"AAA": _metadata(**controls_b)})[0]

    assert first.episode_id == second.episode_id
    assert first.observation.feature_hash == second.observation.feature_hash
    assert first.payload_hash == second.payload_hash
    assert first.observation.numeric_features == second.observation.numeric_features
    assert first.observation.categorical_features == second.observation.categorical_features
    assert not _contains_forbidden_key(first.to_dict())


def test_stale_missing_and_ambiguous_time_evidence_are_retained_but_not_trainable() -> None:
    episodes = _capture(
        active_symbols=("STALE", "MISSING", "AMBIGUOUS"),
        tradable_symbols=("STALE", "MISSING", "AMBIGUOUS"),
        bars_by_symbol={
            symbol: [
                _bar(
                    "2026-08-22T09:00:00+00:00",
                    close=101.0,
                    available_at=(
                        None if symbol == "MISSING" else "2026-08-22T09:05:00+00:00"
                    ),
                ),
                _bar(
                    "2026-08-22T10:00:00+00:00",
                    close=102.0,
                    available_at=None if symbol == "MISSING" else "2026-08-22T10:05:00+00:00",
                ),
            ]
            for symbol in ("STALE", "MISSING", "AMBIGUOUS")
        },
        metadata_by_symbol={
            "STALE": _metadata(freshness={"status": "stale", "data_age_minutes": 120.0}),
            "MISSING": _metadata(available_at=None),
            "AMBIGUOUS": _metadata(timestamp_semantics="session_label"),
        },
    )
    by_symbol = {episode.observation.symbol: episode for episode in episodes}

    assert set(by_symbol) == {"STALE", "MISSING", "AMBIGUOUS"}
    assert by_symbol["STALE"].training_eligible is False
    assert by_symbol["STALE"].training_reason == "freshness_stale"
    assert by_symbol["MISSING"].training_eligible is False
    assert by_symbol["MISSING"].training_reason == "missing_available_at"
    assert by_symbol["AMBIGUOUS"].training_eligible is False
    assert by_symbol["AMBIGUOUS"].training_reason == "ambiguous_timestamp_semantics"
    assert by_symbol["AMBIGUOUS"].observation.anchor.timestamp_semantics == "unknown"


def test_bar_start_is_not_sampled_until_the_bar_is_completed() -> None:
    episodes = _capture(
        bars_by_symbol={
            "AAA": [
                _bar(
                    "2026-08-22T08:00:00+00:00",
                    close=100.0,
                    available_at="2026-08-22T09:01:00+00:00",
                ),
                _bar(
                    "2026-08-22T10:00:00+00:00",
                    close=102.0,
                    available_at="2026-08-22T10:05:00+00:00",
                ),
            ]
        },
        timestamp_semantics="bar_start",
    )

    assert len(episodes) == 1
    assert episodes[0].observation.anchor.ts.isoformat() == "2026-08-22T08:00:00+00:00"


def test_capture_does_not_use_a_bar_known_to_be_available_after_t0() -> None:
    episodes = _capture(
        bars_by_symbol={
            "AAA": [
                _bar(
                    "2026-08-22T09:00:00+00:00",
                    close=101.0,
                    available_at="2026-08-22T09:05:00+00:00",
                ),
                _bar(
                    "2026-08-22T10:00:00+00:00",
                    close=102.0,
                    available_at="2026-08-22T10:45:00+00:00",
                ),
            ]
        }
    )

    assert len(episodes) == 1
    assert episodes[0].observation.anchor.ts.isoformat() == "2026-08-22T09:00:00+00:00"


def test_trailing_quote_row_is_not_a_pseudo_anchor() -> None:
    aligned = [
        _bar(
            "2026-08-22T09:30:00+00:00",
            close=101.0,
            volume=800.0,
            available_at="2026-08-22T10:30:00+00:00",
        ),
        _bar(
            "2026-08-22T09:45:00+00:00",
            close=101.5,
            volume=900.0,
            available_at="2026-08-22T10:30:00+00:00",
        ),
        _bar(
            "2026-08-22T10:00:00+00:00",
            open_=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=0.0,
            available_at="2026-08-22T10:30:00+00:00",
        ),
    ]
    quote = _bar(
        "2026-08-22T10:07:00+00:00",
        open_=102.0,
        high=102.0,
        low=102.0,
        close=102.0,
        volume=0.0,
        available_at="2026-08-22T10:30:00+00:00",
    )

    episodes = _capture(
        bars_by_symbol={"AAA": [*aligned, quote]},
        timestamp_semantics="bar_start",
        interval="15m",
        captured_at="2026-08-22T10:30:00+00:00",
    )

    assert len(episodes) == 1
    assert episodes[0].observation.anchor.ts.isoformat() == "2026-08-22T10:00:00+00:00"
    assert episodes[0].observation.anchor.volume == 0.0
    assert episodes[0].observation.anchor.open == episodes[0].observation.anchor.close

    later_quote = _bar(
        "2026-08-22T10:12:00+00:00",
        open_=102.0,
        high=102.0,
        low=102.0,
        close=102.0,
        volume=0.0,
        available_at="2026-08-22T10:35:00+00:00",
    )
    later = _capture(
        bars_by_symbol={"AAA": [*aligned, later_quote]},
        timestamp_semantics="bar_start",
        interval="15m",
        captured_at="2026-08-22T10:35:00+00:00",
    )
    assert later[0].observation.anchor.ts == episodes[0].observation.anchor.ts
    assert later[0].episode_id == episodes[0].episode_id


def test_capture_and_labeler_bind_the_same_eligible_completed_bar_rule() -> None:
    from trader.application.world_model.labeler import is_eligible_completed_bar as labeler_rule

    assert is_eligible_completed_bar is domain_is_eligible_completed_bar
    assert labeler_rule is domain_is_eligible_completed_bar


OHLCV_KEYS = (
    "volatility",
    "z_score",
    "efficiency_ratio",
    "trend_slope",
    "ohlc_volatility",
    "atr_pct",
    "range_position",
)

_TREND_BARS = (
    # (ts, open, high, low, close, volume)
    ("2026-08-22T05:00:00+00:00", 100.0, 102.0, 99.0, 101.0, 800.0),
    ("2026-08-22T06:00:00+00:00", 101.0, 103.0, 100.0, 102.5, 900.0),
    ("2026-08-22T07:00:00+00:00", 102.5, 105.0, 101.0, 104.0, 1_100.0),
    ("2026-08-22T08:00:00+00:00", 104.0, 104.5, 102.0, 103.0, 950.0),
    ("2026-08-22T09:00:00+00:00", 103.0, 106.0, 102.5, 105.5, 1_200.0),
    ("2026-08-22T10:00:00+00:00", 105.5, 107.0, 104.0, 106.0, 1_050.0),
)


def _trend_history(*, available_at: str = "2026-08-22T10:05:00+00:00") -> list[dict[str, object]]:
    return [
        _bar(ts, open_=open_, high=high, low=low, close=close, volume=volume, available_at=available_at)
        for ts, open_, high, low, close, volume in _TREND_BARS
    ]


def test_ohlcv_formulas_match_domain_indicators() -> None:
    bars = _trend_history()
    episode = _capture(bars_by_symbol={"AAA": bars})[0]
    numeric = episode.observation.numeric_features

    assert episode.observation.anchor.ts.isoformat() == "2026-08-22T10:00:00+00:00"
    expected = compute_indicator_values(
        [
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
            for _, open_, high, low, close, volume in _TREND_BARS
        ],
        names=list(OHLCV_KEYS),
        window=len(_TREND_BARS),
    )
    for key in OHLCV_KEYS:
        assert expected[key] is not None
        assert numeric[key] == pytest.approx(expected[key], abs=1e-6)

    # One hand-computed anchor so the cross-check cannot share a wrong formula.
    closes = [close for _, _, _, _, close, _ in _TREND_BARS]
    path = sum(abs(closes[index] - closes[index - 1]) for index in range(1, len(closes)))
    assert numeric["efficiency_ratio"] == pytest.approx(abs(closes[-1] - closes[0]) / path)


@pytest.mark.parametrize("key", OHLCV_KEYS)
def test_ohlcv_key_ignores_bars_completing_after_the_anchor(key: str) -> None:
    baseline = _capture(bars_by_symbol={"AAA": _trend_history()})[0]
    future_bar = _bar(
        "2026-08-22T11:00:00+00:00",
        open_=106.0,
        high=120.0,
        low=90.0,
        close=115.0,
        volume=50_000.0,
        available_at="2026-08-22T11:05:00+00:00",
    )
    with_future = _capture(bars_by_symbol={"AAA": [*_trend_history(), future_bar]})[0]

    assert with_future.observation.anchor.ts == baseline.observation.anchor.ts
    assert with_future.observation.numeric_features[key] == baseline.observation.numeric_features[key]


@pytest.mark.parametrize("key", OHLCV_KEYS)
def test_ohlcv_key_ignores_bars_beyond_the_twenty_bar_window(key: str) -> None:
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bars = [
        _bar(
            (start + timedelta(hours=index)).isoformat(),
            open_=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=1_000.0,
            available_at="2026-08-22T10:05:00+00:00",
        )
        for index in range(25)
    ]
    outlier = dict(bars[0])
    outlier.update({"high": 10_000.0, "low": 0.5, "close": 5_000.0, "volume": 99_000_000.0})
    with_outlier = _capture(bars_by_symbol={"AAA": [outlier, *bars[1:]]})[0]
    without_outlier = _capture(bars_by_symbol={"AAA": bars[5:]})[0]

    assert with_outlier.observation.anchor.ts == without_outlier.observation.anchor.ts
    assert with_outlier.observation.numeric_features[key] == pytest.approx(
        without_outlier.observation.numeric_features[key]
    )
    # Pin the exact bound from below too: the 20th bar back still counts.
    twenty_one = _capture(bars_by_symbol={"AAA": [outlier, *bars[4:]]})[0]
    twenty = _capture(bars_by_symbol={"AAA": bars[5:]})[0]
    assert twenty_one.observation.numeric_features[key] == pytest.approx(
        twenty.observation.numeric_features[key]
    )


def test_ohlcv_keys_ignore_history_available_only_after_the_anchor() -> None:
    baseline = _capture(bars_by_symbol={"AAA": _trend_history()})[0]
    intruder = _bar(
        "2026-08-22T09:30:00+00:00",
        open_=103.0,
        high=110.0,
        low=95.0,
        close=108.0,
        volume=25_000.0,
        available_at="2026-08-22T10:45:00+00:00",
    )
    with_intruder = _capture(
        bars_by_symbol={"AAA": [*_trend_history(), intruder]},
        captured_at="2026-08-22T11:00:00+00:00",
    )[0]
    rebased = _capture(
        bars_by_symbol={"AAA": _trend_history()},
        captured_at="2026-08-22T11:00:00+00:00",
    )[0]

    assert with_intruder.observation.anchor.ts == baseline.observation.anchor.ts
    assert with_intruder.observation.numeric_features == rebased.observation.numeric_features
    for key in OHLCV_KEYS:
        assert key in with_intruder.observation.numeric_features


def test_ohlcv_formula_guards_on_degenerate_histories() -> None:
    single = _capture(bars_by_symbol={"AAA": [_bar("2026-08-22T10:00:00+00:00")]})[0]
    single_numeric = single.observation.numeric_features
    for key in ("volatility", "z_score", "efficiency_ratio", "trend_slope"):
        assert key not in single_numeric
    for key in ("ohlc_volatility", "atr_pct", "range_position"):
        assert key in single_numeric

    flat = _capture(
        bars_by_symbol={
            "AAA": [
                _bar(
                    f"2026-08-22T0{hour}:00:00+00:00",
                    open_=100.0,
                    high=100.0,
                    low=100.0,
                    close=100.0,
                    volume=1_000.0,
                )
                for hour in (7, 8, 9, 10)
            ]
        }
    )[0]
    flat_numeric = flat.observation.numeric_features
    for key in ("z_score", "efficiency_ratio", "range_position"):
        assert key not in flat_numeric
    assert flat_numeric["volatility"] == pytest.approx(0.0)
    assert flat_numeric["trend_slope"] == pytest.approx(0.0)
    assert flat_numeric["ohlc_volatility"] == pytest.approx(0.0)
    assert flat_numeric["atr_pct"] == pytest.approx(0.0)

    nonpositive = _capture(
        bars_by_symbol={
            "AAA": [
                _bar("2026-08-22T09:00:00+00:00", close=0.0),
                _bar("2026-08-22T10:00:00+00:00", close=102.0),
            ]
        }
    )[0]
    nonpositive_numeric = nonpositive.observation.numeric_features
    for key in ("return", "volatility"):
        assert key not in nonpositive_numeric


def _contains_forbidden_key(value: object) -> bool:
    forbidden = {
        "action",
        "decision",
        "portfolio",
        "scheduler",
        "risk",
        "prompt",
        "tools",
        "llm",
        "qty",
        "fill",
        "pnl",
    }
    if isinstance(value, dict):
        return any(
            key.lower() in forbidden or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_forbidden_key(nested) for nested in value)
    return False
