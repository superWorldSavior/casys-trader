"""Compatibility aliases for decision-ledger consumer-owned ports."""

from __future__ import annotations

from trader.application.decide.recent_decisions import DecisionLedgerReader
from trader.application.record.decision_recorder import DecisionLedgerAppender

__all__ = ["DecisionLedgerAppender", "DecisionLedgerReader"]
