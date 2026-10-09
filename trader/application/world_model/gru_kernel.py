"""Pure NumPy GRU mechanics shared by market classifiers and dynamics models.

No dataset, output head, loss, model identity, or persistence belongs here.
Callers keep parameter order and own the random generator and training policy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

GRUCache = list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None]


def initial_recurrent_parameters(
    rng: np.random.Generator, input_size: int, hidden_size: int
) -> dict[str, np.ndarray]:
    """Draw gates in the historical classifier order from the caller's RNG."""

    input_scale = 1.0 / math.sqrt(input_size + hidden_size)
    recurrent_scale = 1.0 / math.sqrt(2 * hidden_size)

    def normal(shape: tuple[int, ...], scale: float) -> np.ndarray:
        return np.asarray(rng.normal(0.0, scale, size=shape), dtype=np.float64)

    return {
        "Wz": normal((input_size, hidden_size), input_scale),
        "Uz": normal((hidden_size, hidden_size), recurrent_scale),
        "bz": np.zeros(hidden_size, dtype=np.float64),
        "Wr": normal((input_size, hidden_size), input_scale),
        "Ur": normal((hidden_size, hidden_size), recurrent_scale),
        "br": np.zeros(hidden_size, dtype=np.float64),
        "Wh": normal((input_size, hidden_size), input_scale),
        "Uh": normal((hidden_size, hidden_size), recurrent_scale),
        "bh": np.zeros(hidden_size, dtype=np.float64),
    }


def sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -50.0, 50.0)))


def gru_forward(
    parameters: Mapping[str, np.ndarray], matrix: np.ndarray, mask: np.ndarray, hidden_size: int
) -> tuple[np.ndarray, GRUCache]:
    """Encode observed steps; masked padding has no learned-bias drift."""

    hidden = np.zeros(hidden_size, dtype=np.float64)
    cache: GRUCache = []
    for x, valid in zip(matrix, mask, strict=True):
        if not bool(valid):
            cache.append(None)
            continue
        previous = hidden
        update = sigmoid(x @ parameters["Wz"] + previous @ parameters["Uz"] + parameters["bz"])
        reset = sigmoid(x @ parameters["Wr"] + previous @ parameters["Ur"] + parameters["br"])
        candidate = np.tanh(x @ parameters["Wh"] + (reset * previous) @ parameters["Uh"] + parameters["bh"])
        hidden = (1.0 - update) * candidate + update * previous
        cache.append((x, previous, update, reset, candidate))
    return hidden, cache


def accumulate_gru_gradients(
    parameters: Mapping[str, np.ndarray], cache: GRUCache, d_hidden: np.ndarray,
    gradients: dict[str, np.ndarray],
) -> None:
    """Accumulate recurrent BPTT into the caller's ordered gradient mapping."""

    for item in reversed(cache):
        if item is None:
            continue
        x, h_prev, z, r, candidate = item
        d_candidate = d_hidden * (1.0 - z)
        d_update = d_hidden * (h_prev - candidate)
        d_prev = d_hidden * z
        d_candidate_pre = d_candidate * (1.0 - candidate * candidate)
        gradients["Wh"] += np.outer(x, d_candidate_pre)
        gradients["Uh"] += np.outer(r * h_prev, d_candidate_pre)
        gradients["bh"] += d_candidate_pre
        d_reset_times_prev = d_candidate_pre @ parameters["Uh"].T
        d_reset = d_reset_times_prev * h_prev
        d_prev += d_reset_times_prev * r
        d_reset_pre = d_reset * r * (1.0 - r)
        gradients["Wr"] += np.outer(x, d_reset_pre)
        gradients["Ur"] += np.outer(h_prev, d_reset_pre)
        gradients["br"] += d_reset_pre
        d_prev += d_reset_pre @ parameters["Ur"].T
        d_update_pre = d_update * z * (1.0 - z)
        gradients["Wz"] += np.outer(x, d_update_pre)
        gradients["Uz"] += np.outer(h_prev, d_update_pre)
        gradients["bz"] += d_update_pre
        d_prev += d_update_pre @ parameters["Uz"].T
        d_hidden = d_prev


def apply_gradients(
    parameters: dict[str, np.ndarray], gradients: Mapping[str, np.ndarray],
    *, learning_rate: float, gradient_clip: float,
) -> None:
    """Apply one globally clipped update without changing arithmetic order."""

    squared_norm = sum(float(np.sum(gradient * gradient)) for gradient in gradients.values())
    global_norm = math.sqrt(squared_norm)
    if not math.isfinite(global_norm):
        raise FloatingPointError("non-finite GRU gradient")
    scale = 1.0 if global_norm <= gradient_clip else gradient_clip / global_norm
    updated = {
        name: parameters[name] - learning_rate * scale * gradient for name, gradient in gradients.items()
    }
    for value in updated.values():
        if not np.all(np.isfinite(value)):
            raise FloatingPointError("non-finite GRU parameter after update")
    parameters.update(updated)
