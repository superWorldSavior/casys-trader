"""Risk configuration readers shared outside the execution layer."""

from __future__ import annotations

import math
from pathlib import Path

DEFAULT_MIN_TRADE_CONFIDENCE = 0.7


def read_min_trade_confidence(risk_yaml_path: Path) -> float:
    """Read min_trade_confidence from config/risk.yaml with fail-safe defaults."""
    try:
        import yaml

        raw = yaml.safe_load(Path(risk_yaml_path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            value = raw.get("min_trade_confidence", DEFAULT_MIN_TRADE_CONFIDENCE)
            parsed = float(value)
            if math.isfinite(parsed) and 0.0 <= parsed <= 1.0:
                return parsed
    except Exception:
        pass
    return DEFAULT_MIN_TRADE_CONFIDENCE
