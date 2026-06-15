"""Radar Tier-1 : fonctions pures de scoring et de classement."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

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


def scan_and_rank(
    bars_by_symbol: dict[str, object],
    *,
    indicators_fn: Callable[[str, object], dict],
    benchmark_ret_for: dict[str, float],
    tilt_for: Callable[[str], float],
    hard_exclusions: set[str],
    atr_floor: float,
    amplitude_cap: float,
    w_trend: float,
    w_rs: float,
    w_amp: float,
) -> dict:
    """Classe les symboles éligibles par attractivité absolue décroissante."""
    ranked: list[dict] = []
    ineligible: list[dict] = []
    for symbol in sorted(bars_by_symbol):
        components = indicators_fn(symbol, bars_by_symbol[symbol])
        amplitude = components.get("amplitude")
        if not is_eligible(
            symbol,
            amplitude=amplitude,
            hard_exclusions=hard_exclusions,
            atr_floor=atr_floor,
        ):
            reason = "hard_exclusion" if symbol in hard_exclusions else "not_eligible"
            ineligible.append({"symbol": symbol, "reason": reason})
            continue

        directional_score = score_symbol(
            efficiency_ratio=components["efficiency_ratio"],
            ret=components["ret"],
            benchmark_ret=benchmark_ret_for.get(symbol, 0.0),
            amplitude=amplitude,
            tilt=tilt_for(symbol),
            amplitude_cap=amplitude_cap,
            w_trend=w_trend,
            w_rs=w_rs,
            w_amp=w_amp,
        )
        ranked.append(
            {
                "symbol": symbol,
                "directional_score": directional_score,
                "attractiveness": abs(directional_score),
                "bias": "long" if directional_score >= 0 else "short",
            }
        )

    ranked.sort(key=lambda row: (-row["attractiveness"], row["symbol"]))
    return {"ranked": ranked, "ineligible": ineligible}


def build_radar_snapshot(
    ranked: list[dict],
    ineligible: list[dict],
    *,
    as_of: str,
    components_by_symbol: dict[str, dict],
) -> dict:
    """Construit un snapshot machine-readable du scan radar."""
    return {
        "as_of": as_of,
        "ranked": list(ranked),
        "ineligible": list(ineligible),
        "components_by_symbol": dict(components_by_symbol),
    }


def write_snapshot(state_dir: Path, snapshot: dict) -> None:
    """Ecrit le dernier snapshot radar dans state/radar_snapshot.json."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "radar_snapshot.json"
    path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
