"""Compatibility facade for the decision-ledger write side.

Canonical owners:
- identity: :mod:`trader.domain.decision_identity`;
- row projection: :mod:`trader.application.record.decision_ledger_rows`;
- JSONL adapter and migrations: :mod:`trader.infrastructure.files.decision_ledger`.
"""

from __future__ import annotations

from trader.application.record.decision_ledger_rows import (
    SCHEMA_VERSION,
    UNKNOWN_CODE_VERSION,
    build_decision_row,
)
from trader.domain.decision_identity import decision_id
from trader.infrastructure.files import decision_ledger as _decision_ledger
from trader.infrastructure.files.decision_ledger import (
    DEFAULT_EVENT_NAMES,
    DEFAULT_LEDGER_FILENAME,
    DEFAULT_REPORT_NAMES,
    LEGACY_DUPLICATE_WINDOW_S,
    DecisionLedgerStore,
    backfill_code_versions,
    build_legacy_event_row,
    seed_existing_events,
    seed_existing_reports,
)

_decision_id = decision_id
code_version = _decision_ledger.code_version

__all__ = [
    "DEFAULT_EVENT_NAMES",
    "DEFAULT_LEDGER_FILENAME",
    "DEFAULT_REPORT_NAMES",
    "LEGACY_DUPLICATE_WINDOW_S",
    "SCHEMA_VERSION",
    "UNKNOWN_CODE_VERSION",
    "DecisionLedgerStore",
    "backfill_code_versions",
    "build_decision_row",
    "build_legacy_event_row",
    "code_version",
    "decision_id",
    "seed_existing_events",
    "seed_existing_reports",
]
