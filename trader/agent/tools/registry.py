"""Stable registry assembly for bounded agent tools."""

from __future__ import annotations

from trader.agent.tools import attribution, freshness, indicators, learnings, plans
from trader.agent.tools.core import ToolSpec

TOOL_REGISTRY: dict[str, ToolSpec] = {}
for module in (freshness, plans, attribution, indicators, learnings):
    for spec in module.SPECS:
        TOOL_REGISTRY[spec.name] = spec
