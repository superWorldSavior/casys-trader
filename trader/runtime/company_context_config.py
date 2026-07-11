"""Runtime loading for bounded company-context projection caps."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from trader.domain.universe import CompanyContextProjectionLimits


def load_company_context_projection_limits(config_dir: str | Path) -> CompanyContextProjectionLimits:
    try:
        payload: Any = yaml.safe_load(
            (Path(config_dir) / "company_intelligence.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError):
        payload = {}
    projection = payload.get("projection") if isinstance(payload, Mapping) else None
    if not isinstance(projection, Mapping):
        return CompanyContextProjectionLimits()
    try:
        limits = CompanyContextProjectionLimits(
            summary_chars_per_symbol=int(projection.get("summary_chars_per_symbol", 240)),
            max_points_per_symbol=int(projection.get("max_points_per_symbol", 5)),
        )
    except (TypeError, ValueError):
        return CompanyContextProjectionLimits()
    return limits.normalized()


__all__ = ["load_company_context_projection_limits"]
