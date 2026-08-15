"""Compatibility facade for execution risk imports."""

from __future__ import annotations

from trader.domain.execution.risk_gate import RiskGate as RiskGate
from trader.domain.risk import RiskLimits as RiskLimits
from trader.domain.risk import Verdict as Verdict

__all__ = [
    "RiskGate",
    "RiskLimits",
    "Verdict",
]
