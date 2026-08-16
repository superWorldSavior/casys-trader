"""Ports of the learnings-consolidation hexagon.

Inbound (driving) — runtime calls ``maybe_consolidate`` / ``consolidate_payload``.

Outbound (driven) — injected by the composition root:
    raw buffer, consolidated roster, failure status, optional curation,
    and a composer that returns a proposed roster (prompt + LLM stay behind it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

__all__ = [
    "ConsolidatedLearningsPort",
    "ConsolidationStatusPort",
    "CurationPort",
    "LearningComposer",
    "RawLearningsPort",
]


class RawLearningsPort(Protocol):
    """Read the disposable raw JSONL buffer."""

    def all(self) -> list[dict]: ...


class ConsolidatedLearningsPort(Protocol):
    """Read/write the durable consolidated roster."""

    def read(self) -> dict: ...

    def write(self, payload: dict, *, watermark: str | None) -> None: ...


class ConsolidationStatusPort(Protocol):
    """Persist the last failed attempt so the next cycle can back off."""

    def read(self) -> dict: ...

    def write_failure(
        self,
        *,
        consolidated_watermark: str | None,
        raw_watermark: str | None,
        new_raw_count: int,
        error: dict,
    ) -> None: ...

    def clear(self) -> None: ...


class CurationPort(Protocol):
    """Optional FLAIR/MemRL enrichment and leftover acknowledgement.

    Absent → the use case still consolidates from raw JSONL alone.
    """

    def counts(self) -> Mapping[str, object] | None: ...

    def snapshot_pending(self) -> list[dict] | None: ...

    def select_candidates(self, *, current: dict, new_raw: list[dict]) -> list[dict]: ...

    def attach_memrl(self, current: dict) -> dict: ...

    def acknowledge(
        self,
        *,
        candidates: Sequence[dict],
        source_rows: Sequence[dict] | None,
        watermark: str | None,
    ) -> int: ...

    def sync_rules(self, rule_ids: Sequence[str]) -> dict | None: ...


class LearningComposer(Protocol):
    """Propose a new global roster from the current one plus curated evidence.

    Prompt construction, transport, retries and JSON recovery stay in the
    adapter. The use case then validates identity, evidence and keep/restore.
    """

    def compose(
        self,
        current: dict,
        candidates: list[dict],
        *,
        attribution: dict | None = None,
        meta_performance: dict | None = None,
        timeout_s: int,
        max_attempts: int = 3,
    ) -> tuple[dict | None, dict | None]:
        """Return ``(raw_payload, None)`` or ``(None, error)``."""
        ...
