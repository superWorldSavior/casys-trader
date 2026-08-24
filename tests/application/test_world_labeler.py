from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import pytest

from trader.application.world_model.labeler import (
    DIRECTION_BAND,
    DIRECTION_SEMANTICS_VERSION,
    is_eligible_completed_bar,
    label_episode,
    label_horizon,
)
from trader.domain.world_episode import (
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    is_eligible_completed_bar as domain_is_eligible_completed_bar,
)


UTC = timezone.utc


def _episode(
    anchor_at: datetime,
    *,
    episode_id: str = "episode-1",
    training_eligible: bool = True,
) -> dict:
    return {
        "episode_id": episode_id,
        "training_eligible": training_eligible,
        "observation": {
            "symbol": "SPY",
            "venue": "XNYS",
            "bar_interval": "1h",
            "as_of_bar_ts": anchor_at.isoformat(),
            "anchor": _bar(anchor_at, 100.0, high=101.0, low=99.0),
        },
    }


def _bar(
    timestamp: datetime,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    available_at: datetime | None = None,
) -> dict:
    return {
        "ts": timestamp.isoformat(),
        "open": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 100.0,
        "source": "yahoo",
        "interval": "1h",
        "timestamp_semantics": "bar_close",
        "price_basis": "raw",
        **({"available_at": available_at.isoformat()} if available_at is not None else {}),
    }


def test_elapsed_horizons_are_strictly_independent() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)

    labels = {
        row["horizon_id"]: row
        for row in label_episode(
            _episode(anchor_at),
            [_bar(anchor_at + timedelta(hours=4), 101.0)],
            now=anchor_at + timedelta(hours=25),
        )
    }

    assert labels["elapsed_4h.v1"]["status"] == "observed"
    assert labels["elapsed_4h.v1"]["training_eligible"] is False
    assert labels["elapsed_4h.v1"]["training_eligibility_reason"] == "target_available_at_inferred"
    assert labels["elapsed_1d.v1"]["status"] == "missing"
    assert labels["elapsed_1d.v1"]["target_bar"] is None


def test_before_target_bar_is_never_used_as_a_horizon_fallback() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)

    result = label_horizon(
        _episode(anchor_at),
        [_bar(anchor_at + timedelta(hours=3), 130.0)],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=5),
    )

    assert result["status"] == "missing"
    assert result["target_bar"] is None
    assert result["simple_return"] is None


def test_endpoint_is_pending_then_missing_after_one_source_interval() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    episode = _episode(anchor_at)

    pending = label_horizon(
        episode,
        [],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=4, minutes=30),
    )
    missing = label_horizon(
        episode,
        [],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=5),
    )

    assert pending["status"] == "pending"
    assert missing["status"] == "missing"
    assert pending["max_lateness_seconds"] == 3600.0
    assert missing["deadline_at"] == (anchor_at + timedelta(hours=5)).isoformat()


def test_coarser_daily_observation_cannot_fabricate_a_four_hour_label() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    episode = _episode(anchor_at)
    observation = episode["observation"]
    assert isinstance(observation, dict)
    observation["bar_interval"] = "1d"

    result = label_horizon(
        episode,
        [_bar(anchor_at + timedelta(days=1), 101.0)],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(days=2),
    )

    assert result["status"] == "unknown"
    assert result["reason"] == "source_interval_exceeds_horizon"


def test_anchor_timestamp_not_computation_clock_defines_target() -> None:
    anchor_at = datetime(2026, 1, 2, 9, tzinfo=UTC)
    result = label_horizon(
        _episode(anchor_at),
        [_bar(anchor_at + timedelta(hours=4), 102.0)],
        "elapsed_4h.v1",
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "observed"
    assert result["as_of_bar_ts"] == anchor_at.isoformat()
    assert result["target_at"] == (anchor_at + timedelta(hours=4)).isoformat()
    assert result["actual_elapsed_seconds"] == 4 * 3600


def test_bar_start_anchor_begins_elapsed_horizon_at_its_completed_end() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    episode = _episode(anchor_at)
    observation = episode["observation"]
    assert isinstance(observation, dict)
    anchor = observation["anchor"]
    assert isinstance(anchor, dict)
    anchor["timestamp_semantics"] = "bar_start"
    endpoint_at = anchor_at + timedelta(hours=5)

    result = label_horizon(
        episode,
        [
            _bar(anchor_at + timedelta(hours=4), 130.0),
            _bar(endpoint_at, 101.0, available_at=endpoint_at),
        ],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=6),
    )

    assert result["status"] == "observed"
    assert result["as_of_bar_ts"] == anchor_at.isoformat()
    assert result["anchor_end_at"] == (anchor_at + timedelta(hours=1)).isoformat()
    assert result["target_at"] == endpoint_at.isoformat()
    assert result["target_bar"]["ts"] == endpoint_at.isoformat()
    assert result["actual_elapsed_seconds"] == 4 * 3600


def test_action_decision_pnl_and_scheduler_fields_cannot_change_label() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    clean = _episode(anchor_at)
    noisy = deepcopy(clean)
    noisy.update(
        {
            "action": "BUY",
            "decision": {"action": "SELL", "next_wake_in_minutes": 1},
            "pnl": 999_999.0,
            "broker_fills": [{"price": 1.0}],
            "scheduler": {"symbols_due": ["SPY"], "next_wake": "2099-01-01T00:00:00+00:00"},
        }
    )
    bars = [_bar(anchor_at + timedelta(hours=4), 101.0)]
    now = anchor_at + timedelta(hours=5)

    assert label_horizon(clean, bars, "elapsed_4h.v1", now=now) == label_horizon(
        noisy,
        bars,
        "elapsed_4h.v1",
        now=now,
    )


def test_evidence_and_outcome_event_ids_are_deterministic() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    first = _bar(anchor_at + timedelta(hours=4), 101.0)
    reordered = {key: first[key] for key in reversed(tuple(first))}
    now = anchor_at + timedelta(hours=5)

    left = label_horizon(_episode(anchor_at), [first], "elapsed_4h.v1", now=now)
    right = label_horizon(_episode(anchor_at), [reordered], "elapsed_4h.v1", now=now)

    assert left["target_bar"]["fingerprint"] == right["target_bar"]["fingerprint"]
    assert left["target_bar"]["evidence_id"] == right["target_bar"]["evidence_id"]
    assert left["outcome_event_id"] == right["outcome_event_id"]


def test_target_requires_full_availability_and_exposes_market_path_metrics() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    target = _bar(
        anchor_at + timedelta(hours=4),
        101.0,
        high=104.0,
        low=96.0,
        available_at=anchor_at + timedelta(hours=4, minutes=20),
    )

    pending = label_horizon(
        _episode(anchor_at),
        [target],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=4, minutes=10),
    )
    observed = label_horizon(
        _episode(anchor_at),
        [target],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=4, minutes=20),
    )

    assert pending["status"] == "pending"
    assert observed["status"] == "observed"
    assert observed["available_at"] == (anchor_at + timedelta(hours=4, minutes=20)).isoformat()
    assert observed["training_eligible"] is True
    assert observed["simple_return"] == pytest.approx(0.01)
    assert observed["log_return"] == pytest.approx(math.log(1.01))
    assert observed["direction"] == "UP"
    assert observed["direction_band"] == DIRECTION_BAND
    assert observed["direction_semantics_version"] == DIRECTION_SEMANTICS_VERSION
    assert observed["mfe"] == pytest.approx(0.04)
    assert observed["mae"] == pytest.approx(-0.04)
    assert observed["target_bar"]["source"] == "yahoo"
    assert observed["target_bar"]["timestamp_semantics"] == "bar_close"


def test_world_episode_duck_type_and_world_outcome_projection_are_compatible() -> None:
    anchor_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    episode = WorldEpisode(
        observation=WorldObservation(
            venue="XNYS",
            symbol="SPY",
            bar_interval="1h",
            as_of_bar_ts=anchor_at,
            feature_contract_version="world_feature.market.v1",
            sampling_policy_version="fixed_cadence.v1",
            anchor=AnchorBar(anchor_at, 100.0, 101.0, 99.0, 100.0, 100.0, "yahoo"),
            available_at=anchor_at,
            captured_at=anchor_at,
            freshness="fresh",
            numeric_features={"return": 0.0},
        )
    )
    result = label_horizon(
        episode,
        [
            _bar(
                anchor_at + timedelta(hours=4),
                101.0,
                available_at=anchor_at + timedelta(hours=4),
            )
        ],
        "elapsed_4h.v1",
        now=anchor_at + timedelta(hours=5),
    )

    projected = WorldOutcome(
        episode_id=result["episode_id"],
        horizon=result["horizon"],
        status=result["status"],
        target_at=result["target_at"],
        available_at=result["available_at"],
        computed_at=result["computed_at"],
        anchor_close=result["anchor_close"],
        endpoint_close=result["endpoint_close"],
        endpoint_bar_ts=result["endpoint_bar_ts"],
        source=result["source"],
        source_raw_sha256=result["source_raw_sha256"],
        training_eligible=result["training_eligible"],
    )

    assert result["episode_id"] == episode.episode_id
    assert result["event_id"] == projected.event_id
    assert projected.training_eligible is True


def _fifteen_minute_bar(
    timestamp: datetime,
    close: float,
    *,
    volume: float = 100.0,
    available_at: datetime | None = None,
) -> dict:
    payload = _bar(timestamp, close, available_at=available_at)
    payload["interval"] = "15m"
    payload["timestamp_semantics"] = "bar_start"
    payload["open"] = close
    payload["high"] = close
    payload["low"] = close
    payload["volume"] = volume
    return payload


def test_labeler_ignores_off_grid_trailing_quote_and_keeps_aligned_endpoint() -> None:
    anchor_at = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    episode = {
        "episode_id": "episode-15m",
        "training_eligible": True,
        "observation": {
            "symbol": "SPY",
            "venue": "XNYS",
            "bar_interval": "15m",
            "as_of_bar_ts": anchor_at.isoformat(),
            "anchor": _fifteen_minute_bar(anchor_at, 100.0, available_at=anchor_at + timedelta(minutes=15)),
        },
    }
    aligned_endpoint = _fifteen_minute_bar(
        datetime(2026, 8, 22, 14, 15, tzinfo=UTC),
        101.0,
        available_at=datetime(2026, 8, 22, 14, 30, tzinfo=UTC),
    )
    trailing_quote = _fifteen_minute_bar(
        datetime(2026, 8, 22, 14, 7, tzinfo=UTC),
        130.0,
        volume=0.0,
        available_at=datetime(2026, 8, 22, 14, 30, tzinfo=UTC),
    )
    now = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)

    observed = label_horizon(
        episode,
        [trailing_quote, aligned_endpoint],
        "elapsed_4h.v1",
        now=now,
    )
    quote_only = label_horizon(episode, [trailing_quote], "elapsed_4h.v1", now=now)

    assert observed["status"] == "observed"
    assert observed["target_bar"]["ts"] == aligned_endpoint["ts"]
    assert observed["endpoint_bar_ts"] == (anchor_at + timedelta(hours=4, minutes=30)).isoformat()
    assert quote_only["status"] == "missing"
    assert quote_only["target_bar"] is None
    assert is_eligible_completed_bar is domain_is_eligible_completed_bar

    projected = WorldOutcome(
        episode_id=observed["episode_id"],
        horizon=observed["horizon"],
        status=observed["status"],
        target_at=observed["target_at"],
        available_at=observed["available_at"],
        computed_at=observed["computed_at"],
        anchor_close=observed["anchor_close"],
        endpoint_close=observed["endpoint_close"],
        endpoint_bar_ts=observed["endpoint_bar_ts"],
        source=observed["source"],
        source_raw_sha256=observed["source_raw_sha256"],
        training_eligible=observed["training_eligible"],
    )
    assert projected.training_eligible is True
    assert projected.endpoint_bar_ts == datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
