"""Tests TDD — decide_one : handler grain-symbole.

Garantit que decide_one expose les erreurs LLM via RetryableError
au lieu de les absorber en HOLD synthétique (comportement de batch_decide).

Cas couverts :
  - succès BUY           → Decision retournée
  - HOLD délibéré LLM   → Decision HOLD retournée, PAS d'exception
  - rate_limited         → RetryableError(is_overload=True)
  - quota_exceeded       → RetryableError(is_overload=True)
  - timeout              → RetryableError(is_overload=False)
  - nonzero_exit         → RetryableError(is_overload=False)
  - exception client     → RetryableError(is_overload=False)
  - grain-1 vérifié      → symbols=[symbol] dans l'appel client
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from trader.agent_protocol.types import Decision
from trader.application.decide_one import decide_one
from trader.queue.worker import RetryableError

SYMBOL = "AAPL"

_BASE_KWARGS = dict(
    symbol=SYMBOL,
    mandate="test mandate",
    memory="test memory",
    shared_context={},
    per_symbol_facts={},
    decision_timeout_s=60,
    agent_tools_enabled=False,
)


class _FakeClient:
    """Client mock déterministe : retourne une réponse fixe ou lève une exception."""

    def __init__(self, response=None, *, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    def decide_batch(self, *, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), **kwargs})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _ok_decision(action: str = "BUY") -> Decision:
    """Decision LLM authentique — llm_error=None."""
    return Decision(
        symbol=SYMBOL,
        action=action,  # type: ignore[arg-type]
        quantity=10.0,
        confidence=0.8,
        rationale="signal fort",
    )


def _synthetic_hold(code: str) -> Decision:
    """HOLD synthétique produit par _hold_from_llm_failure — llm_error=code."""
    return replace(
        Decision.hold(SYMBOL, f"llm_failed:acpx:{code}:message"),
        llm_error=code,
    )


# ---------------------------------------------------------------------------
# Succès
# ---------------------------------------------------------------------------


def test_succes_buy_retourne_decision():
    """SUCCÈS BUY → decide_one retourne la Decision BUY sans exception."""
    client = _FakeClient({SYMBOL: _ok_decision("BUY")})
    result = decide_one(**_BASE_KWARGS, codex_client=client)
    assert result.action == "BUY"
    assert result.symbol == SYMBOL
    assert result.llm_error is None


def test_succes_sell_retourne_decision():
    """SUCCÈS SELL → decide_one retourne la Decision SELL sans exception."""
    client = _FakeClient({SYMBOL: _ok_decision("SELL")})
    result = decide_one(**_BASE_KWARGS, codex_client=client)
    assert result.action == "SELL"


def test_hold_delibere_retourne_decision_sans_exception():
    """HOLD délibéré du LLM (llm_error=None) → retourné tel quel, PAS d'exception.

    C'est le cas central : ne pas confondre un HOLD choisi par le LLM avec
    un HOLD synthétique d'erreur.
    """
    hold = Decision(
        symbol=SYMBOL,
        action="HOLD",
        quantity=0.0,
        confidence=0.3,
        rationale="Pas de signal convaincant en ce moment",
        intent="HOLD",
    )
    assert hold.llm_error is None  # invariant du test
    client = _FakeClient({SYMBOL: hold})
    result = decide_one(**_BASE_KWARGS, codex_client=client)
    assert result.action == "HOLD"
    assert result.llm_error is None


# ---------------------------------------------------------------------------
# Erreurs overload (is_overload=True)
# ---------------------------------------------------------------------------


def test_rate_limited_leve_retryable_overload():
    """rate_limited → RetryableError(is_overload=True)."""
    client = _FakeClient({SYMBOL: _synthetic_hold("rate_limited")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is True


def test_quota_exceeded_leve_retryable_overload():
    """quota_exceeded → RetryableError(is_overload=True)."""
    client = _FakeClient({SYMBOL: _synthetic_hold("quota_exceeded")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is True


# ---------------------------------------------------------------------------
# Erreurs non-overload (is_overload=False)
# ---------------------------------------------------------------------------


def test_timeout_leve_retryable_non_overload():
    """timeout → RetryableError(is_overload=False)."""
    client = _FakeClient({SYMBOL: _synthetic_hold("timeout")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False


def test_nonzero_exit_leve_retryable_non_overload():
    """nonzero_exit → RetryableError(is_overload=False)."""
    client = _FakeClient({SYMBOL: _synthetic_hold("nonzero_exit")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False


def test_bad_output_leve_retryable_non_overload():
    """bad_output (sortie LLM non parseable) → RetryableError(is_overload=False)."""
    client = _FakeClient({SYMBOL: _synthetic_hold("bad_output")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False


def test_exception_client_leve_retryable_non_overload():
    """Exception levée par le client → RetryableError(is_overload=False)."""
    client = _FakeClient(raise_exc=RuntimeError("connexion coupée"))
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False


# ---------------------------------------------------------------------------
# Invariant grain-1 : l'appel client reçoit symbols=[symbol]
# ---------------------------------------------------------------------------


def test_appel_client_grain_1_symbole():
    """Le client reçoit bien symbols=[symbol] (grain-1)."""
    client = _FakeClient({SYMBOL: _ok_decision()})
    decide_one(**_BASE_KWARGS, codex_client=client)
    assert len(client.calls) == 1
    assert client.calls[0]["symbols"] == [SYMBOL]


def test_appel_client_per_symbol_contient_facts():
    """per_symbol transmis au client contient per_symbol_facts sous la clé symbol."""
    facts = {"data_age_m": 5, "session": {"open": True}}
    client = _FakeClient({SYMBOL: _ok_decision()})
    kwargs = {**_BASE_KWARGS, "per_symbol_facts": facts}
    decide_one(**kwargs, codex_client=client)
    assert client.calls[0]["per_symbol"] == {SYMBOL: facts}
