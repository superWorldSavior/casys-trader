"""Tests TDD — decide_handler : handler decide grain-symbole.

Cas couverts :
  - payload valide + client mocké → retourne Decision sérialisée (JSON)
  - HOLD délibéré (llm_error=None) → retourné tel quel, pas d'exception
  - RetryableError de decide_one remonte sans être avalée
  - Exception client → RetryableError (via decide_one)
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from trader.agent.protocol.types import Decision
from trader.application.decide_handler import make_decide_handler
from trader.queue.worker import RetryableError

SYMBOL = "AAPL"

_PAYLOAD = {
    "symbol": SYMBOL,
    "mandate": "test mandate",
    "memory": "test memory",
    "shared_context": {"session": "open"},
    "per_symbol_facts": {"data_age_m": 5},
    "decision_timeout_s": 60,
    "agent_tools_enabled": False,
}


class _FakeClient:
    """Client mock : retourne une réponse fixe ou lève une exception."""

    def __init__(self, response=None, *, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    def decide_batch(self, *, symbols, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _ok_decision(action: str = "BUY") -> Decision:
    return Decision(
        symbol=SYMBOL,
        action=action,  # type: ignore[arg-type]
        quantity=10.0,
        confidence=0.8,
        rationale="signal fort",
    )


def _make_task(payload: dict) -> dict:
    return {"id": 42, "kind": "decide", "payload": json.dumps(payload)}


# ---------------------------------------------------------------------------
# Succès
# ---------------------------------------------------------------------------


def test_handler_retourne_decision_json_buy():
    """Payload valide, BUY → retourne JSON avec les bons champs."""
    client = _FakeClient({SYMBOL: _ok_decision("BUY")})
    handler = make_decide_handler(codex_client=client)
    result = handler(_make_task(_PAYLOAD))

    assert result is not None
    envelope = json.loads(result)
    assert envelope["model_calls"] == 1  # mode dégradé : un appel
    data = envelope["decision"]
    assert data["symbol"] == SYMBOL
    assert data["action"] == "BUY"
    assert data["confidence"] == 0.8
    assert data["quantity"] == 10.0


def test_handler_hold_delibere_retourne_json_sans_exception():
    """HOLD délibéré (llm_error=None) → JSON retourné, aucune exception."""
    hold = Decision(
        symbol=SYMBOL,
        action="HOLD",
        quantity=0.0,
        confidence=0.3,
        rationale="pas de signal",
        intent="HOLD",
    )
    assert hold.llm_error is None

    client = _FakeClient({SYMBOL: hold})
    handler = make_decide_handler(codex_client=client)
    result = handler(_make_task(_PAYLOAD))

    envelope = json.loads(result)
    assert envelope["model_calls"] == 1  # mode dégradé : un appel
    data = envelope["decision"]
    assert data["action"] == "HOLD"
    assert data["llm_error"] is None


# ---------------------------------------------------------------------------
# Erreurs qui remontent
# ---------------------------------------------------------------------------


def test_handler_retryable_overload_remonte():
    """HOLD synthétique rate_limited → RetryableError(is_overload=True) remonte."""
    synthetic_hold = replace(
        Decision.hold(SYMBOL, "llm failed"),
        llm_error="rate_limited",
    )
    client = _FakeClient({SYMBOL: synthetic_hold})
    handler = make_decide_handler(codex_client=client)

    with pytest.raises(RetryableError) as exc_info:
        handler(_make_task(_PAYLOAD))
    assert exc_info.value.is_overload is True


def test_handler_retryable_non_overload_remonte():
    """HOLD synthétique timeout → RetryableError(is_overload=False) remonte."""
    synthetic_hold = replace(
        Decision.hold(SYMBOL, "timeout"),
        llm_error="timeout",
    )
    client = _FakeClient({SYMBOL: synthetic_hold})
    handler = make_decide_handler(codex_client=client)

    with pytest.raises(RetryableError) as exc_info:
        handler(_make_task(_PAYLOAD))
    assert exc_info.value.is_overload is False


def test_handler_exception_client_remonte_retryable():
    """Exception levée par le client → RetryableError (via decide_one)."""
    client = _FakeClient(raise_exc=RuntimeError("connexion perdue"))
    handler = make_decide_handler(codex_client=client)

    with pytest.raises(RetryableError):
        handler(_make_task(_PAYLOAD))


def test_handler_transmet_session_backends_et_task_id_a_decide_one(monkeypatch):
    captured = {}

    def spy_decide_one(**kwargs):
        captured.update(kwargs)
        return _ok_decision("BUY"), 1

    monkeypatch.setattr("trader.application.decide_handler.decide_one", spy_decide_one)

    session_backends = [object()]
    handler = make_decide_handler(codex_client=object(), session_backends=session_backends)
    task = _make_task(_PAYLOAD)
    task["id"] = "task-decide-123"
    task["attempts"] = 2

    result = handler(task)

    assert result is not None
    assert captured["session_backends"] is session_backends
    assert captured["task_id"] == "task-decide-123#2"
