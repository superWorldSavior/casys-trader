"""State shared between cycles and partly restored across daemon restarts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CycleProcessState:
    """Cross-cycle memory for one daemon process.

    ``last_llm_at`` is the relevance-gate cadence clock. With the canonical
    SQLite backend, the daemon atomically persists and restores it together with
    ``last_wake_reasons`` and ``last_wake_fingerprints`` after each effective LLM
    review. Legacy or corrupt persisted context is ignored fail-open, so an
    untrusted regime/signal identity wakes the LLM once. Gross-rejection feedback
    remains process-local.
    """

    last_llm_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    last_wake_reasons: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    last_wake_fingerprints: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    last_gross_rejections: dict[str, dict | None] = field(default_factory=dict)


__all__ = ["CycleProcessState"]
