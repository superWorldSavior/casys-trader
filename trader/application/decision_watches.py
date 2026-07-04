"""Prepare decision-requested indicator watches for runtime scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trader.planning.indicator_watch import build_indicator_watch


@dataclass(frozen=True)
class DecisionIndicatorWatchPreparation:
    pending_watch: dict | None
    entry_updates: dict[str, object]
    rejections: list[dict]


def prepare_decision_indicator_watch(
    raw_watch: object,
    *,
    symbol: str,
    now: datetime,
    scheduling_enabled: bool,
) -> DecisionIndicatorWatchPreparation:
    """Normalize a decision watch and expose the audit updates for the ledger entry."""
    if not raw_watch:
        return DecisionIndicatorWatchPreparation(pending_watch=None, entry_updates={}, rejections=[])

    result = build_indicator_watch(raw_watch, owner_symbol=symbol, now=now)
    pending_watch = result.watch if scheduling_enabled else None
    return DecisionIndicatorWatchPreparation(
        pending_watch=pending_watch,
        entry_updates={"indicator_watch_rejections": result.rejections},
        rejections=result.rejections,
    )
