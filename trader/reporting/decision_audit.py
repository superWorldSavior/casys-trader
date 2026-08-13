"""Decision audit reporting facade.

Moteur canonique : `trader.reporting.audit.decision_quality`.
Ce module garde la compatibilité d'import historique.
"""

from __future__ import annotations

from trader.reporting.audit import decision_quality as _decision_quality
from trader.reporting.audit.decision_quality import (
    BENCHMARK_SEMANTICS_VERSION,
    audit_rows,
    decision_benchmark_context,
    decision_commit_key,
    decision_verdict,
    load_prices_for_audit,
    load_prices_yfinance,
    parse_horizon,
    parse_ts,
    price_window_for_audit_rows,
    refresh_audit_payload,
    summarize_audited_rows,
)

_MACHINE_REASONS = _decision_quality._MACHINE_REASONS
_verdict = _decision_quality._verdict

__all__ = [
    "audit_rows",
    "BENCHMARK_SEMANTICS_VERSION",
    "decision_benchmark_context",
    "decision_commit_key",
    "decision_verdict",
    "load_prices_for_audit",
    "load_prices_yfinance",
    "parse_horizon",
    "parse_ts",
    "price_window_for_audit_rows",
    "refresh_audit_payload",
    "summarize_audited_rows",
]
