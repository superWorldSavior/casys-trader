"""Parametres calibrables du radar Tier-1."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RadarParams:
    cap_m: int = 25
    delta: float = 0.05
    dwell_days: int = 3
    score_window_bars: int = 15  # horizon du score (~3 semaines daily), distinct du dwell
    atr_floor: float = 0.01
    amplitude_cap: float = 0.05
    min_coverage: float = 0.8
    emergency_score: float = 0.0
    w_trend: float = 1.0
    w_rs: float = 1.0
    w_amp: float = 1.0
    benchmarks: dict[str, str] = field(default_factory=lambda: {"US": "SPY"})
    default_benchmark: str = "SPY"
    gap_threshold: float = 0.03


def load_radar_params(config_dir: Path) -> RadarParams:
    """Charge radar.yaml, ou retourne les defauts explicites documentes."""
    path = config_dir / "radar.yaml"
    if not path.exists():
        return RadarParams()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = RadarParams().__dict__
    kwargs = {key: cfg[key] for key in known if key in cfg}
    return RadarParams(**kwargs)


class ConvictionError(ValueError):
    pass


def load_conviction(config_dir: Path, known_families: set[str]) -> dict[str, float]:
    """Charge les tilts par famille ; fichier absent = pas de tilt."""
    path = config_dir / "conviction.yaml"
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, float] = {}
    for family, raw in cfg.items():
        if family not in known_families:
            raise ConvictionError(f"unknown_family: {family}")
        try:
            tilt = float(raw)
        except (TypeError, ValueError) as exc:
            raise ConvictionError(f"non_numeric_tilt: {family}={raw!r}") from exc
        if tilt <= -1.0:
            raise ConvictionError(f"tilt_le_minus_one: {family}={tilt}")
        out[family] = tilt
    return out
