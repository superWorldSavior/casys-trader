"""Compatibility facade for pure domain relevance gating."""

from __future__ import annotations

from trader.domain.planning.relevance_gate import (
    SIGNAL_DEBOUNCE_HOURS,
    _HTF_SIG_PREFIXES,
    _as_wake_reason_set,
    _cockpit_cell,
    _debounced,
    _has_htf_sig,
    _htf_material,
    cockpit_activity,
    persistent_wake_reasons,
    symbol_needs_llm,
)

__all__ = [
    "SIGNAL_DEBOUNCE_HOURS",
    "_HTF_SIG_PREFIXES",
    "_as_wake_reason_set",
    "_cockpit_cell",
    "_debounced",
    "_has_htf_sig",
    "_htf_material",
    "cockpit_activity",
    "persistent_wake_reasons",
    "symbol_needs_llm",
]
