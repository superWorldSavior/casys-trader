"""Read-model inputs for learning consolidation runtime entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from trader.reporting.read_models import attribution as attribution_mod
from trader.reporting.read_models import meta_performance as meta_performance_mod
from trader.support.config.risk import read_min_trade_confidence


def build_consolidation_inputs(
    *,
    state_dir: Path,
    risk_yaml_path: Path,
    attribution_since: str | None = None,
    exclude_symbols: Sequence[str] = (),
) -> tuple[dict, dict]:
    attr = attribution_mod.compute_attribution(
        state_dir,
        since=attribution_since,
        exclude_symbols=tuple(exclude_symbols),
        min_entry_confidence=read_min_trade_confidence(risk_yaml_path),
    )
    meta = meta_performance_mod.compute_meta_performance(state_dir)
    return attr, meta
