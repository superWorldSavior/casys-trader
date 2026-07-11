"""Historical decision-ledger facade.

The write-side implementation lives outside reporting; this module preserves
the public import surface without owning runtime persistence.
"""

from __future__ import annotations

from trader.reporting.ledger import decision_ledger as _decision_ledger
from trader.reporting.ledger.decision_ledger import (
    DEFAULT_EVENT_NAMES,
    DEFAULT_LEDGER_FILENAME,
    DEFAULT_REPORT_NAMES,
    LEGACY_DUPLICATE_WINDOW_S,
    SCHEMA_VERSION,
    UNKNOWN_CODE_VERSION,
    DecisionLedgerStore,
    backfill_code_versions,
    build_decision_row,
    build_legacy_event_row,
    decision_id,
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
