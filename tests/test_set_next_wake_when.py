"""Tests TDD — set_next_wake{when} : réveil-sur-indicateur via set_next_wake.

Couvre :
- set_next_wake{when: <condition>} → indicator_watch{WAKE} assemblé
- ttl_minutes optionnel propagé
- Coexistence set_next_wake{when} + propose_indicator_watch → rejet tracé
- Compat {minutes} et {on:event} intacts
- when non-dict → ValueError (fast-fail)
- Pas de when/minutes/on → ValueError (compat)
- Intégration build_indicator_watch sur l'indicator_watch assemblé
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from trader.agent_protocol.parsing import _decision_from_symbol_calls, parse_batch
from trader.agent_protocol.types import Decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "AAPL"
_VALID_CONDITION = {
    "indicator": "z_score",
    "op": ">=",
    "value": 1.8,
    "interval": "15m",
    "window": 24,
    "as_of": "latest",
}


def _parse_calls(calls: list, symbol: str = _SYMBOL) -> Decision:
    """Construit une décision minimale avec les tool calls fournis."""
    data = {
        "symbol": symbol,
        "confidence": 0.5,
        "rationale": "test",
        "decision_reason_code": "HOLD_NO_SIGNAL",
        "calls": calls,
    }
    return _decision_from_symbol_calls(data, symbol)


def _now() -> datetime:
    return datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# set_next_wake{when} → indicator_watch assemblé avec on_trigger=WAKE
# ---------------------------------------------------------------------------


def test_wake_when_sets_indicator_watch() -> None:
    """{when} → decision.indicator_watch non None."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None


def test_wake_when_on_trigger_is_wake() -> None:
    """{when} → on_trigger=WAKE dans indicator_watch."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None
    assert d.indicator_watch.get("on_trigger") == "WAKE"


def test_wake_when_logic_is_all() -> None:
    """{when} → logic=all dans indicator_watch."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None
    assert d.indicator_watch.get("logic") == "all"


def test_wake_when_condition_wrapped_in_list() -> None:
    """{when} → conditions = liste d'un élément == la condition fournie."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None
    conditions = d.indicator_watch.get("conditions")
    assert isinstance(conditions, list)
    assert len(conditions) == 1
    assert conditions[0] == _VALID_CONDITION


def test_wake_when_no_wake_minutes() -> None:
    """{when} → next_wake_in_minutes reste None."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.next_wake_in_minutes is None


def test_wake_when_no_wake_event() -> None:
    """{when} → next_wake_event reste None (ce n'est pas un réveil calendaire)."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.next_wake_event is None


# ---------------------------------------------------------------------------
# ttl_minutes optionnel
# ---------------------------------------------------------------------------


def test_wake_when_ttl_minutes_propagated() -> None:
    """{when, ttl_minutes: 120} → ttl_minutes=120 dans indicator_watch."""
    d = _parse_calls([
        {"tool": "set_next_wake", "args": {"when": _VALID_CONDITION, "ttl_minutes": 120}}
    ])
    assert d.indicator_watch is not None
    assert d.indicator_watch.get("ttl_minutes") == 120


def test_wake_when_no_ttl_minutes_key_absent() -> None:
    """{when} sans ttl_minutes → clé absente de indicator_watch (build_indicator_watch utilise son défaut)."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None
    assert "ttl_minutes" not in d.indicator_watch


# ---------------------------------------------------------------------------
# Coexistence set_next_wake{when} + propose_indicator_watch → rejet
# ---------------------------------------------------------------------------


def test_coexistence_wake_when_and_propose_indicator_watch_raises() -> None:
    """set_next_wake{when} ET propose_indicator_watch dans le même calls → ValueError."""
    watch_raw = {
        "on_trigger": "EXECUTE_ORDER",
        "logic": "all",
        "ttl_minutes": 60,
        "conditions": [_VALID_CONDITION],
        "order": {
            "intent": "OPEN_LONG",
            "qty": 10,
            "confidence": 0.75,
            "exit_plan": {"hard_stop": 150.0},
        },
    }
    calls = [
        {"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}},
        {"tool": "propose_indicator_watch", "args": {"watch": watch_raw}},
    ]
    with pytest.raises(ValueError, match="wake_when_conflicts_with_propose_indicator_watch"):
        _parse_calls(calls)


def test_coexistence_reversed_order_also_raises() -> None:
    """propose_indicator_watch EN PREMIER puis set_next_wake{when} → aussi ValueError."""
    watch_raw = {
        "on_trigger": "WAKE",
        "logic": "all",
        "ttl_minutes": 60,
        "conditions": [_VALID_CONDITION],
    }
    calls = [
        {"tool": "propose_indicator_watch", "args": {"watch": watch_raw}},
        {"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}},
    ]
    with pytest.raises(ValueError, match="wake_when_conflicts_with_propose_indicator_watch"):
        _parse_calls(calls)


def test_coexistence_becomes_hold_in_parse_batch() -> None:
    """Coexistence wake_when + propose_indicator_watch → HOLD via parse_batch (isolation)."""
    watch_raw = {"on_trigger": "WAKE", "logic": "all", "ttl_minutes": 60, "conditions": [_VALID_CONDITION]}
    raw = json.dumps({
        "decisions": [{
            "symbol": "AAPL",
            "confidence": 0.5,
            "rationale": "test",
            "decision_reason_code": "HOLD_NO_SIGNAL",
            "calls": [
                {"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}},
                {"tool": "propose_indicator_watch", "args": {"watch": watch_raw}},
            ],
        }]
    })
    result = parse_batch(raw, ["AAPL"], allow_context_request=False)
    d = result["AAPL"]
    assert isinstance(d, Decision)
    assert d.action == "HOLD"
    assert "wake_when_conflicts" in d.rationale


# ---------------------------------------------------------------------------
# Compat {minutes} et {on:event} intacts
# ---------------------------------------------------------------------------


def test_compat_minutes_still_sets_next_wake_in_minutes() -> None:
    """{minutes: 15} → next_wake_in_minutes=15, indicator_watch=None."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"minutes": 15}}])
    assert d.next_wake_in_minutes == 15.0
    assert d.indicator_watch is None


def test_compat_on_session_open_still_sets_event() -> None:
    """{on: session_open} → next_wake_event='session_open', indicator_watch=None."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"on": "session_open"}}])
    assert d.next_wake_event == "session_open"
    assert d.next_wake_in_minutes is None
    assert d.indicator_watch is None


def test_compat_on_macro_event() -> None:
    """{on: macro_event} → next_wake_event='macro_event'."""
    d = _parse_calls([{"tool": "set_next_wake", "args": {"on": "macro_event"}}])
    assert d.next_wake_event == "macro_event"
    assert d.indicator_watch is None


# ---------------------------------------------------------------------------
# Malformation — fast-fail
# ---------------------------------------------------------------------------


def test_wake_when_not_dict_raises() -> None:
    """{when: 'pas_un_dict'} → ValueError(wake_when_must_be_object)."""
    with pytest.raises(ValueError, match="wake_when_must_be_object"):
        _parse_calls([{"tool": "set_next_wake", "args": {"when": "pas_un_dict"}}])


def test_wake_when_number_raises() -> None:
    """{when: 42} → ValueError(wake_when_must_be_object)."""
    with pytest.raises(ValueError, match="wake_when_must_be_object"):
        _parse_calls([{"tool": "set_next_wake", "args": {"when": 42}}])


def test_wake_when_list_raises() -> None:
    """{when: [...]} → ValueError(wake_when_must_be_object) (liste = pas un objet condition)."""
    with pytest.raises(ValueError, match="wake_when_must_be_object"):
        _parse_calls([{"tool": "set_next_wake", "args": {"when": [_VALID_CONDITION]}}])


def test_no_args_still_raises_wake_minutes_required() -> None:
    """Aucun arg → ValueError(wake_minutes_required) (compat)."""
    with pytest.raises(ValueError, match="wake_minutes_required"):
        _parse_calls([{"tool": "set_next_wake", "args": {}}])


# ---------------------------------------------------------------------------
# Intégration avec build_indicator_watch
# ---------------------------------------------------------------------------


def test_assembled_watch_is_valid_for_build_indicator_watch() -> None:
    """L'indicator_watch assemblé par set_next_wake{when} est traité correctement
    par build_indicator_watch (pas de rejet) → watch valide avec on_trigger=WAKE."""
    from trader.planning.indicator_watch import build_indicator_watch

    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}}])
    assert d.indicator_watch is not None
    result = build_indicator_watch(d.indicator_watch, owner_symbol=_SYMBOL, now=_now())
    assert result.watch is not None, f"rejets: {result.rejections}"
    assert result.rejections == []
    assert result.watch["on_trigger"] == "WAKE"
    assert len(result.watch["conditions"]) == 1


def test_assembled_watch_malformed_condition_rejected_by_builder() -> None:
    """Condition avec indicator inconnu → build_indicator_watch rejette (atomicité),
    mais le parsing n'échoue pas (validation tardive au daemon)."""
    bad_condition = {"indicator": "indicateur_inconnu", "op": ">=", "value": 1.0}
    d = _parse_calls([{"tool": "set_next_wake", "args": {"when": bad_condition}}])
    assert d.indicator_watch is not None  # Parsing OK

    from trader.planning.indicator_watch import build_indicator_watch

    result = build_indicator_watch(d.indicator_watch, owner_symbol=_SYMBOL, now=_now())
    assert result.watch is None  # Rejeté par build_indicator_watch
    assert len(result.rejections) == 1
    assert result.rejections[0]["reason"] == "unknown_indicator"


def test_parse_batch_wake_when_sets_indicator_watch() -> None:
    """Intégration parse_batch : {when} depuis decisions[].calls → indicator_watch."""
    raw = json.dumps({
        "decisions": [{
            "symbol": "AAPL",
            "confidence": 0.6,
            "rationale": "breakout",
            "decision_reason_code": "HOLD_NO_SIGNAL",
            "calls": [
                {"tool": "set_next_wake", "args": {"when": _VALID_CONDITION}},
            ],
        }]
    })

    result = parse_batch(raw, ["AAPL"], allow_context_request=False)
    d = result["AAPL"]
    assert isinstance(d, Decision)
    assert d.indicator_watch is not None
    assert d.indicator_watch.get("on_trigger") == "WAKE"
    assert d.next_wake_in_minutes is None
    assert d.next_wake_event is None
