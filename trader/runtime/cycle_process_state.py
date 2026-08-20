"""State shared between successive cycles of one daemon process."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CycleProcessState:
    """Cross-cycle memory for one daemon process.

    ``last_llm_at`` is the relevance-gate cadence clock. The daemon restores it
    from SQLite at boot and writes it back after each effective LLM review so a
    restart does not treat every symbol as never seen. Material fingerprints
    stay process-local: after restart a present regime/signal has no trusted
    prior identity and therefore conservatively wakes the LLM once.
    """

    last_llm_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    last_wake_reasons: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    last_wake_fingerprints: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    last_gross_rejections: dict[str, dict | None] = field(default_factory=dict)


__all__ = ["CycleProcessState"]
