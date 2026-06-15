"""Radar Tier-1 : fonctions pures de scoring et de classement."""

from __future__ import annotations


def is_eligible(
    symbol: str,
    *,
    amplitude: float | None,
    hard_exclusions: set[str],
    atr_floor: float,
) -> bool:
    """Retourne si un symbole a assez de data/amplitude et n'est pas exclu."""
    if symbol in hard_exclusions:
        return False
    if amplitude is None:
        return False
    return amplitude >= atr_floor


def score_symbol(
    *,
    efficiency_ratio: float,
    ret: float,
    benchmark_ret: float,
    amplitude: float,
    tilt: float,
    amplitude_cap: float,
    w_trend: float,
    w_rs: float,
    w_amp: float,
) -> float:
    """Score composite signé : magnitude = attractivité, signe = biais."""
    direction = 1.0 if ret >= 0 else -1.0
    trend = efficiency_ratio * direction
    relative_strength = (ret - benchmark_ret) * direction
    amplitude_reward = min(amplitude, amplitude_cap)
    raw = w_trend * trend + w_rs * relative_strength
    return raw * (1.0 + w_amp * amplitude_reward) * (1.0 + tilt)
