"""Compatibility facade and CLI rendering for tool-usage reports."""

from __future__ import annotations

from trader.reporting.read_models.tool_usage import (
    build_report,
    clamp_count,
    domain_tool_usage,
    risk_observability,
    tool_usage_rates,
    tool_vs_quality,
)
from trader.reporting.renderers.tool_usage import render_cli

__all__ = [
    "build_report",
    "clamp_count",
    "domain_tool_usage",
    "render_cli",
    "risk_observability",
    "tool_usage_rates",
    "tool_vs_quality",
]
_render_cli = render_cli


if __name__ == "__main__":
    from trader.interfaces.cli.tool_usage import main

    main()
