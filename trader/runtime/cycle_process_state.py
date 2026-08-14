"""Volatile state shared between successive cycles of one daemon process."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CycleProcessState:
    """Cross-cycle memory that is intentionally reset when the daemon restarts."""

    last_llm_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    last_wake_reasons: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    last_gross_rejections: dict[str, dict | None] = field(default_factory=dict)


__all__ = ["CycleProcessState"]
