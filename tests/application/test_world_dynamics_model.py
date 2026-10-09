from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from trader.application.world_model.dynamics_model import (
    COORDINATE_SCALES,
    DynamicsSamplingError,
    EmpiricalDynamicsBaseline,
    InsufficientDynamicsSupport,
    OnlineOHLCVDynamicsModel,
    decode_coordinates,
    encode_history,
    transition_coordinates,
)
from trader.domain.world_dynamics import SimulatedBar
from trader.domain.world_episode import AnchorBar

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def _bar(index=0, *, opening=100.0, closing=100.2, high=100.5, low=99.8, volume=1000.0):
    return AnchorBar(
        ts=NOW + timedelta(minutes=15 * index), open=opening, high=high, low=low, close=closing,
        volume=volume, source="test-market", timestamp_semantics="bar_close",
    )


def test_log_coordinate_roundtrip_preserves_ohlcv_and_simulated_provenance():
    source = _bar()
    target = _bar(1, opening=101.0, closing=101.4, high=101.9, low=100.7, volume=1700.0)
    actual = decode_coordinates(source, transition_coordinates(source, target), end_at=target.ts, step_index=1)
    for name in ("open", "high", "low", "close", "volume"):
        assert getattr(actual, name) == pytest.approx(getattr(target, name), rel=1e-12)
    assert isinstance(actual, SimulatedBar)
    assert actual.simulated is True
    assert actual.to_dict()["kind"] == "simulated_bar"
    assert not hasattr(actual, "available_at")


def test_decoder_reports_rectification_policy_and_rejects_unrepresentable_prices():
    source = _bar()
    sampled = decode_coordinates(source, [0.0, 0.002, -1.0, -2.0, -100.0], end_at=NOW, step_index=1)
    assert sampled.high == max(sampled.open, sampled.close)
    assert sampled.low == min(sampled.open, sampled.close)
    assert sampled.volume == 0.0
    for coordinates in ([1e6, 0, 0, 0, 0], [-1e6, 0, 0, 0, 0], [0, 0, 0, 0, 1e6]):
        with pytest.raises(DynamicsSamplingError, match="overflows|underflows"):
            decode_coordinates(source, coordinates, end_at=NOW, step_index=1)
    with pytest.raises(DynamicsSamplingError, match="finite"):
        decode_coordinates(source, [0, 0, np.nan, 0, 0], end_at=NOW, step_index=1)


def test_history_encoder_uses_fixed_scaling_padding_and_observed_gap():
    first, second = _bar(), _bar(1, opening=100.4, closing=100.6, high=100.9, low=100.1, volume=1200)
    matrix, mask = encode_history([first, second], 4)
    assert mask.tolist() == [False, False, True, True]
    assert np.array_equal(matrix[:2], np.zeros((2, 5)))
    assert matrix[2, 0] == matrix[2, 4] == 0
    assert np.array_equal(matrix[3], transition_coordinates(first, second) / COORDINATE_SCALES)
    with pytest.raises(ValueError, match="empty"):
        encode_history([], 4)


def test_model_is_deterministic_and_residuals_are_from_before_each_update():
    history, target = [_bar()], _bar(1, opening=100.4, closing=100.6, high=100.9, low=100.1, volume=1200)
    first = OnlineOHLCVDynamicsModel(seed=7, minimum_support=2, residual_window=2)
    second = OnlineOHLCVDynamicsModel(seed=7, minimum_support=2, residual_window=2)
    cold_mean = first.predict_coordinates(history)
    expected_residual = (transition_coordinates(history[-1], target) - cold_mean) / COORDINATE_SCALES
    first.update(history, target)
    second.update(history, target)
    assert np.allclose(first._residuals[0], expected_residual, rtol=1e-14, atol=1e-14)
    assert first.fingerprint() == second.fingerprint()
    with pytest.raises(InsufficientDynamicsSupport):
        first.sample_next(history, np.random.default_rng(1), NOW, 1)
    for _ in range(3):
        first.update(history, target)
        second.update(history, target)
    assert first.support == 4
    assert first.residual_support == 2
    assert first.fingerprint() == second.fingerprint()
    before = first.fingerprint()
    a = first.sample_next(history, np.random.default_rng(9), NOW, 1)
    b = second.sample_next(history, np.random.default_rng(9), NOW, 1)
    assert a == b
    assert first.fingerprint() == before
    assert first.diagnostics()["calibration_claim"] is False
    assert first.diagnostics()["context_policy"] == "market_only"
    assert {key: first.diagnostics()[key] for key in ("sequence_len", "hidden_size", "learning_rate", "gradient_clip", "seed")} == {
        "sequence_len": 4, "hidden_size": 12, "learning_rate": 0.025, "gradient_clip": 1.0, "seed": 7,
    }


def test_continuous_head_and_shared_bptt_learn_repeated_transition():
    history, target = [_bar()], _bar(1, opening=100.4, closing=100.6, high=100.9, low=100.1, volume=1200)
    model = OnlineOHLCVDynamicsModel(seed=7, minimum_support=1, learning_rate=0.05)
    actual = transition_coordinates(history[-1], target) / COORDINATE_SCALES
    cold_error = np.mean((model.predict_coordinates(history) / COORDINATE_SCALES - actual) ** 2)
    for _ in range(100):
        model.update(history, target)
    learned_error = np.mean((model.predict_coordinates(history) / COORDINATE_SCALES - actual) ** 2)
    assert learned_error < cold_error / 10
    assert np.all(np.isfinite(model.predict_coordinates(history)))


def test_rollout_accepts_generated_inputs_but_never_trains_on_them():
    history, target = [_bar()], _bar(1, opening=100.4, closing=100.6, high=100.9, low=100.1, volume=1200)
    model = OnlineOHLCVDynamicsModel(minimum_support=1)
    model.update(history, target)
    simulated = model.sample_next(history, np.random.default_rng(2), NOW + timedelta(minutes=15), 1)
    assert model.predict_coordinates([*history, simulated]).shape == (5,)
    with pytest.raises(TypeError, match="real AnchorBar"):
        model.update([*history, simulated], target)
    with pytest.raises(TypeError, match="real AnchorBar"):
        model.update(history, simulated)
    baseline = EmpiricalDynamicsBaseline(minimum_support=1)
    with pytest.raises(TypeError, match="real AnchorBar"):
        baseline.update(history, simulated)


def test_empirical_baseline_samples_complete_past_vectors_not_independent_dimensions():
    history = [_bar()]
    targets = [
        _bar(1, opening=101, closing=101.5, high=102, low=100.8, volume=1400),
        _bar(1, opening=99, closing=98.7, high=99.1, low=98.2, volume=700),
    ]
    model = EmpiricalDynamicsBaseline(minimum_support=2, residual_window=2)
    for target in targets:
        model.update(history, target)
    for seed in range(10):
        sampled = model.sample_next(history, np.random.default_rng(seed), NOW, 1)
        coordinates = transition_coordinates(history[-1], sampled)
        assert any(np.allclose(coordinates, transition_coordinates(history[-1], target)) for target in targets)
    before = model.fingerprint()
    assert model.sample_next(history, np.random.default_rng(2), NOW, 1) == model.sample_next(
        history, np.random.default_rng(2), NOW, 1
    )
    assert model.fingerprint() == before


@pytest.mark.parametrize("kwargs", [{"sequence_len": 5}, {"hidden_size": True}, {"learning_rate": 0},
                                     {"gradient_clip": float("nan")}, {"minimum_support": 3, "residual_window": 2}])
def test_invalid_or_unbounded_model_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        OnlineOHLCVDynamicsModel(**kwargs)
