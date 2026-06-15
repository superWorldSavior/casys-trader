"""Radar Tier-1 : fonctions pures de scoring et de classement."""

from __future__ import annotations

from .features import compute_indicator_values
from .radar_config import RadarParams


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


def daily_components(symbol: str, bars: list[object], params: RadarParams) -> dict:
    """Adapte les indicateurs features aux noms attendus par le radar daily."""
    values = compute_indicator_values(
        bars,
        names=["efficiency_ratio", "return", "ohlc_volatility"],
        window=params.dwell_days,
    )
    return {
        "efficiency_ratio": values["efficiency_ratio"],
        "ret": values["return"],
        "amplitude": values["ohlc_volatility"],
    }
