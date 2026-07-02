"""Stable registry assembly for bounded agent tools."""
from __future__ import annotations

from trader.agent_tools import attribution, freshness, indicators, learnings, plans, risk
from trader.agent_tools.core import ToolSpec

TOOL_REGISTRY: dict[str, ToolSpec] = {}
for module in (freshness, plans, risk, attribution, indicators, learnings):
    for spec in module.SPECS:
        TOOL_REGISTRY[spec.name] = spec
