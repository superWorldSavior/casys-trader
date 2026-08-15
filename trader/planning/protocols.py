"""Compatibility facade for planning-facing collaborator protocols."""

from __future__ import annotations

from trader.domain.planning.protocols import SchedulerLike, TradePlanStoreLike

__all__ = ["SchedulerLike", "TradePlanStoreLike"]
