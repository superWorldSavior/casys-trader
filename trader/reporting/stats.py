"""stats — read-only live KPI reporting helpers."""

from __future__ import annotations

from pathlib import Path

from trader.reporting.read_models import live_kpis
from trader.reporting.renderers.live_kpis import render_text


def _compute_model_performance(state_dir: Path) -> list[dict]:
    """Compatibility wrapper for legacy tests/imports."""
    return live_kpis.compute_model_performance(state_dir)


def compute_live_kpis(state_dir: Path) -> dict:
    """Compatibility wrapper for the canonical live KPI read model."""
    return live_kpis.compute_live_kpis(state_dir)


_render_text = render_text


if __name__ == "__main__":
    from trader.interfaces.cli.stats import main

    main()
