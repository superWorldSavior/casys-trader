"""Small offline OHLCV dynamics models with past-only joint bootstrap noise.

The GRU predicts a five-coordinate next-bar mean with squared-error updates.
Uncertainty is empirical, not a Gaussian likelihood or calibrated guarantee.
No synthetic bar is promoted into a WorldEpisode or written to the ledger.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
import json
import math

import numpy as np

from trader.application.world_model.gru_kernel import (
    accumulate_gru_gradients,
    apply_gradients,
    gru_forward,
    initial_recurrent_parameters,
)
from trader.domain.world_dynamics import SimulatedBar
from trader.domain.world_episode import AnchorBar

Bar = AnchorBar | SimulatedBar
COORDINATE_NAMES = ("log_open_gap", "log_body_return", "log_upper_wick", "log_lower_wick", "log_volume_change")
COORDINATE_SCALES = np.asarray((0.01, 0.01, 0.01, 0.01, 1.0), dtype=np.float64)
DECODE_POLICY = "wick_rectification_and_nonnegative_log_volume.v1"
COORDINATE_CODEC_VERSION = "log_ohlcv_transition_coordinates.v1"


class InsufficientDynamicsSupport(ValueError):
    """A cold model or residual window cannot support stochastic rollout."""


class DynamicsSamplingError(ValueError):
    """A sampled transition cannot be represented as valid finite OHLCV."""


def _validated_bars(history: Sequence[Bar], *, real_only: bool = False) -> tuple[Bar, ...]:
    if not history:
        raise ValueError("dynamics history must not be empty")
    bars = tuple(history)
    for bar in bars:
        if not isinstance(bar, AnchorBar if real_only else (AnchorBar, SimulatedBar)):
            raise TypeError("training requires real AnchorBar evidence" if real_only else "expected market or simulated bar")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("OHLCV must be finite")
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            raise ValueError("OHLCV prices must be positive and volume nonnegative")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("OHLCV wick ordering is invalid")
    return bars


def transition_coordinates(source: Bar, target: Bar) -> np.ndarray:
    """Raw log coordinates; differences of logs avoid overflowing ratios."""
    _validated_bars((source, target))
    return np.asarray(
        (
            math.log(target.open) - math.log(source.close),
            math.log(target.close) - math.log(target.open),
            math.log(target.high) - math.log(max(target.open, target.close)),
            math.log(min(target.open, target.close)) - math.log(target.low),
            math.log1p(target.volume) - math.log1p(source.volume),
        ),
        dtype=np.float64,
    )


def encode_history(history: Sequence[Bar], sequence_len: int) -> tuple[np.ndarray, np.ndarray]:
    """Fixed scaling and masked left padding; first unknown gap/volume change are zero."""
    bars = _validated_bars(history)
    matrix = np.zeros((sequence_len, len(COORDINATE_NAMES)), dtype=np.float64)
    mask = np.zeros(sequence_len, dtype=bool)
    first = max(0, len(bars) - sequence_len)
    offset = sequence_len - (len(bars) - first)
    for index in range(first, len(bars)):
        bar = bars[index]
        previous = bars[index - 1] if index else bar
        coordinates = transition_coordinates(previous, bar)
        if index == 0:
            coordinates[0] = 0.0
            coordinates[4] = 0.0
        matrix[offset + index - first] = coordinates / COORDINATE_SCALES
        mask[offset + index - first] = True
    return matrix, mask


def decode_coordinates(
    source: Bar, coordinates: Sequence[float] | np.ndarray, *, end_at: datetime, step_index: int,
) -> SimulatedBar:
    """Rectify negative wick/log-volume samples; reject overflow rather than silently cap prices."""
    _validated_bars((source,))
    vector = np.asarray(coordinates, dtype=np.float64)
    if vector.shape != (5,) or not np.all(np.isfinite(vector)):
        raise DynamicsSamplingError("transition coordinates must be five finite values")
    gap, body, upper, lower, volume_change = (float(value) for value in vector)
    log_open = math.log(source.close) + gap
    log_close = log_open + body
    log_high = max(log_open, log_close) + max(0.0, upper)
    log_low = min(log_open, log_close) - max(0.0, lower)
    log_volume = max(0.0, math.log1p(source.volume) + volume_change)
    try:
        opening, high, low, closing = (math.exp(value) for value in (log_open, log_high, log_low, log_close))
        volume = math.expm1(log_volume)
    except OverflowError as exc:
        raise DynamicsSamplingError("sampled OHLCV overflows finite representation") from exc
    if not all(math.isfinite(value) for value in (opening, high, low, closing, volume)):
        raise DynamicsSamplingError("sampled OHLCV is non-finite")
    if min(opening, high, low, closing) <= 0:
        raise DynamicsSamplingError("sampled price underflows positive representation")
    return SimulatedBar(step_index=step_index, end_at=end_at, open=opening, high=high, low=low, close=closing, volume=volume)


def _bounded_integer(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _positive_finite(value: float, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return numeric


class OnlineOHLCVDynamicsModel:
    """Action-free GRU mean plus a bounded joint bootstrap of pre-update residuals."""

    model_id = "online_gru_ohlcv_dynamics.v1"
    context_policy = "market_only"

    def __init__(
        self, *, sequence_len: int = 4, hidden_size: int = 12, learning_rate: float = 0.025,
        gradient_clip: float = 1.0, minimum_support: int = 20, residual_window: int = 256, seed: int = 20261009,
    ) -> None:
        self.sequence_len = _bounded_integer(sequence_len, "sequence_len", 4)
        self.hidden_size = _bounded_integer(hidden_size, "hidden_size", 64)
        self.minimum_support = _bounded_integer(minimum_support, "minimum_support", 100000)
        self.residual_window = _bounded_integer(residual_window, "residual_window", 4096)
        if self.residual_window < self.minimum_support:
            raise ValueError("residual_window must retain at least minimum_support vectors")
        self.learning_rate = _positive_finite(learning_rate, "learning_rate")
        self.gradient_clip = _positive_finite(gradient_clip, "gradient_clip")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        self.seed = seed
        rng = np.random.default_rng(seed)
        self._parameters = initial_recurrent_parameters(rng, 5, self.hidden_size)
        scale = 1.0 / math.sqrt(self.hidden_size + 5)
        self._parameters["Wo"] = np.asarray(rng.normal(0.0, scale, size=(self.hidden_size, 5)), dtype=np.float64)
        self._parameters["bo"] = np.zeros(5, dtype=np.float64)
        self._residuals: deque[np.ndarray] = deque(maxlen=self.residual_window)
        self._support = 0

    @property
    def support(self) -> int:
        return self._support

    @property
    def residual_support(self) -> int:
        return len(self._residuals)

    def predict_coordinates(self, history: Sequence[Bar]) -> np.ndarray:
        """Return the deterministic next-bar mean in raw log units, without learning."""
        matrix, mask = encode_history(history, self.sequence_len)
        hidden, _ = gru_forward(self._parameters, matrix, mask, self.hidden_size)
        result = hidden @ self._parameters["Wo"] + self._parameters["bo"]
        if not np.all(np.isfinite(result)):
            raise FloatingPointError("non-finite dynamics mean")
        return result * COORDINATE_SCALES

    def update(self, history: Sequence[AnchorBar], target: AnchorBar) -> None:
        """Learn once per caller-selected causal transition; residual precedes this update."""
        bars = _validated_bars(history, real_only=True)
        _validated_bars((target,), real_only=True)
        matrix, mask = encode_history(bars, self.sequence_len)
        actual = transition_coordinates(bars[-1], target) / COORDINATE_SCALES
        hidden, cache = gru_forward(self._parameters, matrix, mask, self.hidden_size)
        mean = hidden @ self._parameters["Wo"] + self._parameters["bo"]
        residual = actual - mean
        gradients = {name: np.zeros_like(value) for name, value in self._parameters.items()}
        d_mean = 2.0 * (mean - actual) / len(COORDINATE_NAMES)
        gradients["Wo"] += np.outer(hidden, d_mean)
        gradients["bo"] += d_mean
        d_hidden = d_mean @ self._parameters["Wo"].T
        accumulate_gru_gradients(self._parameters, cache, d_hidden, gradients)
        apply_gradients(self._parameters, gradients, learning_rate=self.learning_rate, gradient_clip=self.gradient_clip)
        self._residuals.append(residual.copy())
        self._support += 1

    def sample_next(
        self, history: Sequence[Bar], rng: np.random.Generator, end_at: datetime, step_index: int,
    ) -> SimulatedBar:
        if self.support < self.minimum_support or self.residual_support < self.minimum_support:
            raise InsufficientDynamicsSupport("insufficient model or past-residual support for rollout")
        mean = self.predict_coordinates(history)
        residual = self._residuals[int(rng.integers(len(self._residuals)))]
        return decode_coordinates(history[-1], mean + residual * COORDINATE_SCALES, end_at=end_at, step_index=step_index)

    def diagnostics(self) -> dict[str, object]:
        return {
            "model_id": self.model_id, "support": self.support, "residual_support": self.residual_support,
            "minimum_support": self.minimum_support, "residual_window": self.residual_window,
            "minimum_residual_support": self.minimum_support, "coordinate_codec": COORDINATE_CODEC_VERSION,
            "sequence_len": self.sequence_len, "hidden_size": self.hidden_size,
            "learning_rate": self.learning_rate, "gradient_clip": self.gradient_clip, "seed": self.seed,
            "uncertainty_policy": "past_pre_update_joint_residual_bootstrap.v1", "decode_policy": DECODE_POLICY,
            "context_policy": self.context_policy, "calibration_claim": False,
        }

    def fingerprint(self) -> str:
        digest = sha256()
        config = self.diagnostics()
        digest.update(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
        for name, value in sorted(self._parameters.items()):
            digest.update(name.encode())
            digest.update(np.asarray(value, dtype="<f8").tobytes())
        for residual in self._residuals:
            digest.update(np.asarray(residual, dtype="<f8").tobytes())
        return digest.hexdigest()


class EmpiricalDynamicsBaseline:
    """Past target-vector bootstrap baseline with the same constrained decoder."""

    model_id = "empirical_joint_ohlcv_dynamics.v1"
    context_policy = "market_only"

    def __init__(self, *, minimum_support: int = 20, residual_window: int = 256) -> None:
        self.minimum_support = _bounded_integer(minimum_support, "minimum_support", 100000)
        self.residual_window = _bounded_integer(residual_window, "residual_window", 4096)
        if self.residual_window < self.minimum_support:
            raise ValueError("residual_window must retain at least minimum_support vectors")
        self._targets: deque[np.ndarray] = deque(maxlen=self.residual_window)
        self._support = 0

    @property
    def support(self) -> int:
        return self._support

    @property
    def residual_support(self) -> int:
        return len(self._targets)

    def update(self, history: Sequence[AnchorBar], target: AnchorBar) -> None:
        bars = _validated_bars(history, real_only=True)
        _validated_bars((target,), real_only=True)
        self._targets.append(transition_coordinates(bars[-1], target))
        self._support += 1

    def predict_coordinates(self, history: Sequence[Bar]) -> np.ndarray:
        _validated_bars(history)
        if not self._targets:
            raise InsufficientDynamicsSupport("empirical baseline has no past target support")
        return np.mean(np.asarray(self._targets), axis=0)

    def sample_next(
        self, history: Sequence[Bar], rng: np.random.Generator, end_at: datetime, step_index: int,
    ) -> SimulatedBar:
        bars = _validated_bars(history)
        if self.support < self.minimum_support or self.residual_support < self.minimum_support:
            raise InsufficientDynamicsSupport("insufficient past target support for empirical rollout")
        coordinates = self._targets[int(rng.integers(len(self._targets)))]
        return decode_coordinates(bars[-1], coordinates, end_at=end_at, step_index=step_index)

    def diagnostics(self) -> dict[str, object]:
        return {
            "model_id": self.model_id, "support": self.support, "residual_support": self.residual_support,
            "minimum_support": self.minimum_support, "residual_window": self.residual_window,
            "minimum_residual_support": self.minimum_support, "coordinate_codec": COORDINATE_CODEC_VERSION,
            "uncertainty_policy": "past_joint_target_bootstrap.v1", "decode_policy": DECODE_POLICY,
            "context_policy": self.context_policy, "calibration_claim": False,
        }

    def fingerprint(self) -> str:
        digest = sha256(json.dumps(self.diagnostics(), sort_keys=True, separators=(",", ":")).encode())
        for target in self._targets:
            digest.update(np.asarray(target, dtype="<f8").tobytes())
        return digest.hexdigest()
