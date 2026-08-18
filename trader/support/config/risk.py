"""Risk configuration readers shared outside the execution layer."""

from __future__ import annotations

import math
from pathlib import Path

DEFAULT_MIN_TRADE_CONFIDENCE = 0.7
# Live-safe, aligned with ``RiskLimits.confidence_gate_enabled``.
DEFAULT_CONFIDENCE_GATE_ENABLED = True


def _load_risk_yaml(risk_yaml_path: Path) -> dict:
    try:
        import yaml

        raw = yaml.safe_load(Path(risk_yaml_path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _parse_min_trade_confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_TRADE_CONFIDENCE
    if math.isfinite(parsed) and 0.0 <= parsed <= 1.0:
        return parsed
    return DEFAULT_MIN_TRADE_CONFIDENCE


def read_min_trade_confidence(risk_yaml_path: Path) -> float:
    """Read min_trade_confidence from config/risk.yaml with fail-safe defaults."""
    raw = _load_risk_yaml(risk_yaml_path)
    return _parse_min_trade_confidence(
        raw.get("min_trade_confidence", DEFAULT_MIN_TRADE_CONFIDENCE)
    )


def attribution_min_entry_confidence(
    risk_cfg: dict,
    *,
    confidence_gate_enabled: bool,
) -> float | None:
    """Attribution censor threshold, coupled to the live confidence gate.

    Gate off → ``None`` (no censor): low-confidence trips stay in the
    ``by_confidence`` buckets and in UI ``recent_trips``. Gate on →
    ``min_trade_confidence``.
    """
    if not confidence_gate_enabled:
        return None
    return _parse_min_trade_confidence(
        risk_cfg.get("min_trade_confidence", DEFAULT_MIN_TRADE_CONFIDENCE)
    )


def read_attribution_min_entry_confidence(risk_yaml_path: Path) -> float | None:
    """Same gate coupling as the daemon, read from ``config/risk.yaml``."""
    raw = _load_risk_yaml(risk_yaml_path)
    gate = raw.get("confidence_gate_enabled", DEFAULT_CONFIDENCE_GATE_ENABLED)
    return attribution_min_entry_confidence(
        raw,
        confidence_gate_enabled=bool(gate),
    )
