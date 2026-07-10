"""Compatibility facade for execution risk imports."""

from __future__ import annotations

from trader.domain.execution.risk_gate import RiskGate as RiskGate
from trader.domain.risk import RiskLimits as RiskLimits
from trader.domain.risk import Verdict as Verdict
from trader.support.config.risk import (
    read_min_trade_confidence as read_min_trade_confidence,
)

__all__ = [
    "RiskGate",
    "RiskLimits",
    "Verdict",
    "read_min_trade_confidence",
]
