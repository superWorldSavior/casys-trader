"""Stable registry assembly for bounded agent tools."""

from __future__ import annotations

from trader.agent.tools import (
    attribution,
    freshness,
    indicators,
    learnings,
    plans,
    trade_plan_evaluation,
)
from trader.agent.tools.core import ToolSpec

TOOL_REGISTRY: dict[str, ToolSpec] = {}
for module in (
    freshness,
    plans,
    attribution,
    indicators,
    learnings,
    trade_plan_evaluation,
):
    for spec in module.SPECS:
        TOOL_REGISTRY[spec.name] = spec
