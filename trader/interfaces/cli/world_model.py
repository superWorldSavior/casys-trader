"""Thin CLI adapter for the shadow world-model status projector."""

from __future__ import annotations

from trader.reporting.read_models.world_status import HORIZONS, read_world_model_status

__all__ = ["HORIZONS", "read_world_model_status"]
