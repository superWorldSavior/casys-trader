"""Small, dependency-free coercion helpers shared across outer layers."""

from __future__ import annotations

import math
from typing import Any


def finite_float(
    value: Any,
    default: float | None = 0.0,
) -> float | None:
    """Return ``value`` as a finite float, or ``default`` when invalid."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def dict_list(value: Any) -> list[dict]:
    """Return only mapping rows from a concrete list value."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


__all__ = ["dict_list", "finite_float"]
