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

from trader.agent import llm
from trader.agent.protocol.types import Decision
from trader.application.decide.one import SESSION_ROUND_BACKSTOP, decide_one
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
    result, _calls = decide_one(**_BASE_KWARGS, codex_client=client)
    assert result.action == "BUY"
    assert result.symbol == SYMBOL
    assert result.llm_error is None
    assert client.calls[0]["use_symbol_calls_contract"] is True
    assert client.calls[0]["allow_tool_calls"] is False


def test_succes_sell_retourne_decision():
    """SUCCÈS SELL → decide_one retourne la Decision SELL sans exception."""
    client = _FakeClient({SYMBOL: _ok_decision("SELL")})
    result, _calls = decide_one(**_BASE_KWARGS, codex_client=client)
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
    result, _calls = decide_one(**_BASE_KWARGS, codex_client=client)
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


def test_appel_client_projette_les_postures_univers_hors_cible():
    facts = {
        "universe_mandate": {
            "symbol_mandate": {
                "symbol": SYMBOL,
                "family_context": {"family": "tech"},
            },
            "family_postures": {
                "tech": {"posture": "constructive"},
                "energy": {"posture": "defensive"},
            },
        }
    }
    client = _FakeClient({SYMBOL: _ok_decision()})

    decide_one(
        **{**_BASE_KWARGS, "per_symbol_facts": facts},
        codex_client=client,
    )

    sent = client.calls[0]["per_symbol"][SYMBOL]
    assert sent["universe_mandate"]["family_postures"] == {
        "tech": {"posture": "constructive"}
    }
    assert set(facts["universe_mandate"]["family_postures"]) == {"tech", "energy"}


# ---------------------------------------------------------------------------
# Erreurs de parsing (FIX 1) — parse_batch réel + decide_one
#
# Ces tests utilisent un mock passthrough qui appelle le VRAI parse_batch avec
# un texte LLM brut contrôlé. Ils valident que les HOLD-parser produits par
# parse_batch (après FIX 1 : llm_error != None) font bien lever RetryableError
# dans decide_one, au lieu d'être silencieusement retournés comme HOLD délibérés.
# ---------------------------------------------------------------------------


class _ParsePassthroughClient:
    """Mock qui fait transiter la réponse LLM brute via le VRAI parse_batch.

    Simule le chemin de client.decide_batch pour le cas sans tool_calls :
    parse_batch est appelé avec le texte brut et retourne le dict décisions.
    Sans _attach_llm_metadata (pas de LlmCompletion), mais decide_one ne consulte
    que llm_error — pas llm_provider/llm_model.
    """

    def __init__(self, llm_text: str):
        self._llm_text = llm_text

    def decide_batch(self, *, symbols, allow_context_request=False, **kwargs):
        from trader.agent.protocol.parsing import parse_batch
        return parse_batch(
            self._llm_text,
            list(symbols),
            allow_context_request=allow_context_request,
        )


def test_parse_error_json_invalide_leve_retryable():
    """JSON invalide → parse_error:invalid_json → RetryableError(is_overload=False)."""
    client = _ParsePassthroughClient("pas du json valide {{{")
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False
    assert "parse_error:invalid_json" in str(exc_info.value)


def test_parse_error_symbole_manquant_leve_retryable():
    """Symbole absent de la réponse batch → parse_error:missing_symbol → RetryableError."""
    # Réponse LLM valide mais sans AAPL (symbole inconnu GOOG à la place)
    llm_text = (
        '{"decisions": [{"symbol": "GOOG", "action": "HOLD", "quantity": 0,'
        ' "confidence": 0, "rationale": "ok", "decision_reason_code": "NO_EDGE"}]}'
    )
    client = _ParsePassthroughClient(llm_text)
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False
    assert "parse_error:missing_symbol" in str(exc_info.value)


def test_parse_error_tool_loop_leve_retryable():
    """tool_calls sans decisions (tour final) → tool_loop → RetryableError(is_overload=False)."""
    llm_text = '{"tool_calls": [{"tool": "get_freshness", "args": {}}]}'
    client = _ParsePassthroughClient(llm_text)
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False
    assert "tool_loop" in str(exc_info.value)


def test_parse_error_element_corrompu_leve_retryable():
    """Élément decisions malformé (action invalide) → parse_error:corrupt_element → RetryableError."""
    llm_text = (
        '{"decisions": [{"symbol": "AAPL", "action": "INVALID", "quantity": 0,'
        ' "confidence": 0, "rationale": "bad"}]}'
    )
    client = _ParsePassthroughClient(llm_text)
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is False
    assert "parse_error:corrupt_element" in str(exc_info.value)


def test_hold_delibere_via_parse_batch_retourne_decision_sans_exception():
    """HOLD délibéré parsé proprement (llm_error=None) → retourné sans exception.

    Valide que FIX 1 n'a pas accidentellement estampillé les HOLD LLM légitimes.
    """
    llm_text = (
        '{"decisions": [{"symbol": "AAPL", "action": "HOLD", "quantity": 0,'
        ' "confidence": 0.3, "rationale": "pas de signal", "decision_reason_code": "NO_EDGE"}]}'
    )
    client = _ParsePassthroughClient(llm_text)
    result, _calls = decide_one(**_BASE_KWARGS, codex_client=client)
    assert result.action == "HOLD"
    assert result.llm_error is None  # HOLD délibéré — pas estampillé


# ---------------------------------------------------------------------------
# FIX 2 — provider_error → is_overload=True
# ---------------------------------------------------------------------------


def test_provider_error_leve_retryable_overload():
    """provider_error (internal error / sortie vide acpx) → RetryableError(is_overload=True).

    §4.5 design : internal-error/sortie vide acpx = pression app-server → decrease M.
    """
    client = _FakeClient({SYMBOL: _synthetic_hold("provider_error")})
    with pytest.raises(RetryableError) as exc_info:
        decide_one(**_BASE_KWARGS, codex_client=client)
    assert exc_info.value.is_overload is True


# ---------------------------------------------------------------------------
# Tour d'outils grain-1 (T4 — spec queue tool-round, issue #2)
# ---------------------------------------------------------------------------

from trader.agent.protocol.types import BatchToolCallRequest  # noqa: E402
from trader.application.decide.one import ToolRoundServices  # noqa: E402
from trader.runtime.worker_cycle_context import (  # noqa: E402
    ExitValidationInputs,
    OpenPlansSnapshot,
    WorkerCycleContext,
    WorkerCycleContextHandle,
)
from trader.runtime.queue_runtime import build_decide_tool_services  # noqa: E402


class _SeqClient:
    """Client mock séquentiel : une réponse par appel, kwargs enregistrés."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def decide_batch(self, *, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), **kwargs})
        return self._responses.pop(0)


def _services(**over) -> ToolRoundServices:
    base = dict(
        get_bars=lambda symbol, lookback="5d", interval="1h": [],
        learnings_recall_provider=None,
        max_context_requests_per_symbol=2,
        max_indicators_per_request=4,
    )
    base.update(over)
    return ToolRoundServices(**base)


class _FakeSession:
    def send(self, prompt, *, timeout_s, call_ctx=None):
        raise AssertionError("decide_batch fake ignore complete_fn ; send ne doit pas être appelé")

    def close(self):
        return None


class _FakeSessionBackend:
    def open_session(self, name, *, timeout_s):
        return _FakeSession()


def _session_backends() -> list:
    return [_FakeSessionBackend()]


def _published_cycle_context(
    *,
    cycle_id: str,
    rows: tuple[dict, ...] = (),
    raw_plans: tuple[object, ...] = (),
    bars_by_symbol: dict[str, list] | None = None,
    prices_by_symbol: dict[str, float] | None = None,
    attribution: dict | None = None,
) -> WorkerCycleContextHandle:
    handle = WorkerCycleContextHandle()
    handle.publish(
        WorkerCycleContext(
            cycle_id=cycle_id,
            as_of=cycle_id,
            open_plans=OpenPlansSnapshot(rows=rows, raw_plans=raw_plans),
            exit_validation=ExitValidationInputs(
                bars_by_symbol=bars_by_symbol or {},
                prices_by_symbol=prices_by_symbol or {},
            ),
            attribution=attribution or {},
        )
    )
    return handle


def _cycle_services(tmp_path, handle: WorkerCycleContextHandle) -> ToolRoundServices:
    return build_decide_tool_services(
        get_data_source=lambda: None,
        worker_cycle_context=handle,
        learnings_db_path=tmp_path / "learnings.db",
        max_context_requests_per_symbol=2,
        max_indicators_per_request=4,
    )


def _tool_request() -> BatchToolCallRequest:
    return BatchToolCallRequest(
        calls=[{"id": "c1", "tool": "get_active_plans", "args": {"symbol": SYMBOL}}]
    )


def test_tool_round_puis_decision_au_tour_2_sort_tot():
    """Round d'outils + décision au tour suivant : sortie tôt, sans tour final forcé."""
    client = _SeqClient([_tool_request(), {SYMBOL: _ok_decision("BUY")}])
    kwargs = {
        **_BASE_KWARGS,
        "agent_tools_enabled": True,
        "per_symbol_facts": {"active_watches": [{"id": "w1", "kind": "stop"}]},
    }

    decision, calls = decide_one(
        **kwargs,
        codex_client=client,
        tool_services=_services(),
        session_backends=_session_backends(),
        task_id="t",
    )

    assert decision.action == "BUY"
    assert calls == 2
    assert client.calls[0]["allow_tool_calls"] is True     # round : outils autorisés
    assert client.calls[1]["allow_tool_calls"] is True     # l'agent décide librement au tour 2
    assert all(c["allow_context_request"] is False for c in client.calls)  # Q4 : pas de legacy
    assert all(c["max_tool_calls_per_symbol"] == 8 for c in client.calls)
    assert [c["session_followup"] for c in client.calls] == [False, True]
    assert client.calls[1]["per_symbol"][SYMBOL]["tool_results"]  # résultats réinjectés
    assert decision.domain_tools["tool_rounds"] == 1       # traces mergées (persistance)


def test_fallback_session_recommence_par_un_prompt_complet():
    """Un nouveau backend ne doit jamais hériter d'un delta sans son cockpit."""

    failure = llm.LlmFailure(
        provider="terra",
        model="gpt-5.6-terra",
        code="rate_limited",
        message="retry elsewhere",
        retryable=True,
    )

    class FallbackClient:
        def __init__(self):
            self.calls: list[dict] = []
            self.responses = [
                _tool_request(),
                llm.SessionProviderDown(failure),
                {SYMBOL: _ok_decision("HOLD")},
            ]

        def decide_batch(self, *, symbols, **kwargs):
            self.calls.append({"symbols": list(symbols), **kwargs})
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    client = FallbackClient()
    backends = [_FakeSessionBackend(), _FakeSessionBackend()]

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=backends,
        task_id="fallback:AAPL",
    )

    assert decision.action == "HOLD"
    assert calls == 3
    assert [call["session_followup"] for call in client.calls] == [False, True, False]


def test_tool_context_recoit_open_plans_du_worker_cycle_context():
    client = _SeqClient([BatchToolCallRequest(calls=[{"id": "c1", "tool": "get_active_plans", "args": {}}]), {SYMBOL: _ok_decision("HOLD")}])
    handle = _published_cycle_context(
        cycle_id="cycle-1",
        rows=(
            {"id": "plan-aapl", "symbol": SYMBOL},
            {"id": "plan-msft", "symbol": "MSFT"},
        ),
    )

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(worker_cycle_context=handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    tool_result = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert calls == 2
    assert tool_result["ok"] is True
    assert [row["symbol"] for row in tool_result["result"]["rows"]] == [SYMBOL, "MSFT"]


def test_tool_context_recoit_attribution_complete_hors_prompt() -> None:
    client = _SeqClient([
        BatchToolCallRequest(
            calls=[{"id": "c1", "tool": "get_attribution", "args": {"scope": "summary"}}]
        ),
        {SYMBOL: _ok_decision("HOLD")},
    ])
    handle = _published_cycle_context(
        cycle_id="cycle-1",
        attribution={"n_closed_trades": 9, "realized_pnl": 123.0},
    )

    decision, calls = decide_one(
        **{
            **_BASE_KWARGS,
            "agent_tools_enabled": True,
            "shared_context": {"attribution": {"n_closed_trades": 1}},
        },
        codex_client=client,
        tool_services=_services(worker_cycle_context=handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    tool_result = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert calls == 2
    assert tool_result["result"] == {
        "summary": {"n_closed_trades": 9, "realized_pnl": 123.0}
    }


def test_attribution_cycle_mismatch_retombe_sur_le_resume_du_payload() -> None:
    client = _SeqClient([
        BatchToolCallRequest(
            calls=[{"id": "c1", "tool": "get_attribution", "args": {"scope": "summary"}}]
        ),
        {SYMBOL: _ok_decision("HOLD")},
    ])
    handle = _published_cycle_context(
        cycle_id="cycle-2",
        attribution={"n_closed_trades": 99},
    )

    decision, _ = decide_one(
        **{
            **_BASE_KWARGS,
            "agent_tools_enabled": True,
            "shared_context": {"attribution": {"n_closed_trades": 3}},
        },
        codex_client=client,
        tool_services=_services(worker_cycle_context=handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    tool_result = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert tool_result["result"] == {"summary": {"n_closed_trades": 3}}


def test_get_active_plans_cycle_mismatch_devient_tool_error_sans_lire_le_cycle_courant():
    handle = _published_cycle_context(
        cycle_id="cycle-2",
        rows=({"id": "plan-msft", "symbol": "MSFT"},),
    )
    client = _SeqClient([
        BatchToolCallRequest(calls=[{"id": "c1", "tool": "get_active_plans", "args": {}}]),
        {SYMBOL: _ok_decision("HOLD")},
    ])

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(worker_cycle_context=handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    tool_result = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert calls == 2
    assert tool_result["ok"] is False
    assert "CycleContextUnavailable" in tool_result["error"]
    assert "plan-msft" not in str(tool_result)


def test_strategy_exit_cycle_mismatch_fail_closed_avant_correction_llm(tmp_path):
    handle = _published_cycle_context(cycle_id="cycle-2", raw_plans=(object(),))
    first_decision = replace(
        _ok_decision("HOLD"),
        exit_update={"hard_stop": {"type": "structural", "anchor": "swing_low"}},
    )
    client = _SeqClient([{SYMBOL: first_decision}, {SYMBOL: _ok_decision("HOLD")}])

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_cycle_services(tmp_path, handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    feedback = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert calls == 2
    assert feedback == {
        "id": f"validation:strategy_exit:{SYMBOL}:1",
        "tool": "strategy_exit",
        "ok": False,
        "error": "cycle_context_unavailable",
    }


def test_strategy_exit_cycle_match_valide_sur_contexte_worker_sans_refetch(monkeypatch, tmp_path):
    from trader.application.exit.exit_update import ExitUpdateValidation

    raw_plan = object()
    runtime_bars = [("runtime", SYMBOL)]
    validation = ExitUpdateValidation(would_apply=False, reason="resolve_failed:test", warnings=[])
    calls: dict[str, object] = {}
    handle = _published_cycle_context(
        cycle_id="cycle-1",
        raw_plans=(raw_plan,),
        bars_by_symbol={SYMBOL: runtime_bars},
        prices_by_symbol={SYMBOL: 432.1},
    )
    first_decision = replace(
        _ok_decision("HOLD"),
        exit_update={"hard_stop": {"mode": "structural"}},
    )
    client = _SeqClient([{SYMBOL: first_decision}, {SYMBOL: _ok_decision("HOLD")}])

    def fake_validate_exit_update(**kwargs):
        calls["validate"] = kwargs
        return validation

    monkeypatch.setattr("trader.application.exit.exit_update.validate_exit_update", fake_validate_exit_update)

    decision, model_calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_cycle_services(tmp_path, handle),
        cycle_id="cycle-1",
        session_backends=_session_backends(),
        task_id="t",
    )

    validate_call = calls["validate"]
    feedback = client.calls[1]["per_symbol"][SYMBOL]["tool_results"][0]
    assert decision.action == "HOLD"
    assert model_calls == 2
    assert validate_call["plan_store"].open_plans() == [raw_plan]
    assert validate_call["symbol"] == SYMBOL
    assert validate_call["exit_update"] == {"hard_stop": {"mode": "structural"}}
    assert validate_call["bars"] == runtime_bars
    assert validate_call["current_price"] == 432.1
    assert feedback["error"] == "resolve_failed:test"


def test_session_multiround_heartbeat_apres_open_et_chaque_appel_modele():
    """Session : heartbeat après open_session puis après chaque call_model."""
    client = _SeqClient([_tool_request(), {SYMBOL: _ok_decision("BUY")}])
    heartbeats: list[str] = []

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=_session_backends(),
        task_id="t",
        heartbeat=lambda: heartbeats.append("hb"),
    )

    assert decision.action == "BUY"
    assert calls == 2
    assert heartbeats == ["hb", "hb", "hb"]


def test_session_prompt_libre_mais_loop_backstop():
    client = _SeqClient([_tool_request(), _tool_request(), {SYMBOL: _ok_decision("BUY")}])

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=_session_backends(),
        task_id="t",
    )

    assert decision.action == "BUY"
    assert calls == 3
    assert all(c["max_rounds"] is None for c in client.calls)


def test_session_backstop_force_tour_final_apres_20_demandes_outils():
    client = _SeqClient([_tool_request()] * (SESSION_ROUND_BACKSTOP + 1))

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=_session_backends(),
        task_id="t",
    )

    assert decision.action == "HOLD"
    assert decision.rationale == "tool_loop_blocked"
    assert calls == SESSION_ROUND_BACKSTOP + 1
    assert [c["allow_tool_calls"] for c in client.calls] == [True] * SESSION_ROUND_BACKSTOP + [False]
    assert all(c["max_rounds"] is None for c in client.calls)


def test_session_tool_loop_parse_au_tour_final_est_terminal_sans_retry():
    """Au tour final session, tool_calls parsés en tool_loop_blocked ne doivent pas retry."""
    parsed_blocked = replace(Decision.hold(SYMBOL, "tool_loop_blocked"), llm_error="tool_loop")
    client = _SeqClient([_tool_request()] * SESSION_ROUND_BACKSTOP + [{SYMBOL: parsed_blocked}])

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=_session_backends(),
        task_id="t",
    )

    assert decision.action == "HOLD"
    assert decision.rationale == "tool_loop_blocked"
    assert decision.llm_error is None
    assert calls == SESSION_ROUND_BACKSTOP + 1
    assert client.calls[-1]["allow_tool_calls"] is False


def test_sans_tool_services_mode_degrade_un_appel():
    """tool_services=None → mode dégradé historique : 1 appel, aucun outil au prompt."""
    client = _FakeClient({SYMBOL: _ok_decision("BUY")})

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True}, codex_client=client
    )

    assert decision.action == "BUY"
    assert calls == 1
    assert client.calls[0]["allow_tool_calls"] is False


def test_erreur_llm_au_tour_final_leve_retryable():
    """HOLD synthétique (llm_error) rendu APRÈS le round → RetryableError, comme sans round."""
    client = _SeqClient([_tool_request(), {SYMBOL: _synthetic_hold("timeout")}])
    kwargs = {**_BASE_KWARGS, "agent_tools_enabled": True}

    with pytest.raises(RetryableError) as exc:
        decide_one(
            **kwargs,
            codex_client=client,
            tool_services=_services(),
            session_backends=_session_backends(),
            task_id="t",
        )
    assert exc.value.is_overload is False


def test_univers_transmis_au_resolver_via_cross_asset():
    """symbols_universe (payload) alimente le resolver : la paire de FAMILLE est fetchée.

    SPY/QQQ = même famille (indices) — un indicateur cross-asset (relative_strength)
    déclenche le fetch de la paire, preuve que l'univers du payload atteint le resolver.
    """
    fetched: list[str] = []

    def spy_get_bars(symbol, lookback="5d", interval="1h"):
        fetched.append(symbol)
        return []

    request = BatchToolCallRequest(
        calls=[{
            "id": "c1",
            "tool": "get_indicator_context",
            "args": {"symbol": "SPY", "indicators": ["relative_strength"], "timeframe": "1h"},
        }]
    )
    client = _SeqClient([request, {"SPY": replace(_ok_decision("HOLD"), symbol="SPY")}])
    kwargs = {**_BASE_KWARGS, "symbol": "SPY", "agent_tools_enabled": True}

    decide_one(
        **kwargs,
        codex_client=client,
        tool_services=_services(get_bars=spy_get_bars),
        symbols_universe=["SPY", "QQQ"],
        session_backends=_session_backends(),
        task_id="t",
    )

    assert "SPY" in fetched           # le symbole est fetché
    assert "QQQ" in fetched           # la paire de famille (univers) aussi


def test_session_mode_utilise_runner_delta_et_complete_fn(monkeypatch):
    events = []
    heartbeats = []

    class FakeSession:
        def send(self, prompt, *, timeout_s, call_ctx=None):
            events.append(("send", prompt, timeout_s))
            return llm.LlmCompletion(provider="acpx", model="gpt-5.5", text="{}")

    def fake_run_with_session_fallback(backends, *, task_id, resolve, open_timeout_s):
        events.append(("runner", backends, task_id, open_timeout_s))
        return resolve(FakeSession())

    def action_validator(symbol: str, exit_update: dict):
        raise AssertionError("ce test vérifie seulement la propagation du callable")

    def fake_resolve_symbol_decision(**kwargs):
        assert kwargs["heartbeat"] is heartbeat
        assert kwargs["action_validator"] is action_validator
        events.append(("resolve", kwargs["max_rounds"], kwargs.get("reinject")))
        response = kwargs["call_model"]({SYMBOL: {"round": 1}}, allow_tool_calls=True)
        return response[SYMBOL]

    class SessionAwareClient:
        def __init__(self):
            self.calls = []

        def decide_batch(self, *, symbols, **kwargs):
            self.calls.append({"symbols": list(symbols), **kwargs})
            assert callable(kwargs["complete_fn"])
            kwargs["complete_fn"]("session prompt", 9)
            return {SYMBOL: _ok_decision("BUY")}

    monkeypatch.setattr(
        "trader.application.decide.one.llm.run_with_session_fallback",
        fake_run_with_session_fallback,
    )
    monkeypatch.setattr(
        "trader.application.decide.one.resolve_symbol_decision",
        fake_resolve_symbol_decision,
    )

    client = SessionAwareClient()
    backends = [object()]

    def heartbeat():
        events.append(("heartbeat",))
        heartbeats.append("hb")

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(action_validator=action_validator),
        session_backends=backends,
        task_id="decide:AAPL",
        heartbeat=heartbeat,
    )

    assert decision.action == "BUY"
    assert calls == 1
    assert events == [
        ("runner", backends, "decide:AAPL", 75),
        ("heartbeat",),
        ("resolve", SESSION_ROUND_BACKSTOP, "delta"),
        ("send", "session prompt", 9),
    ]
    assert heartbeats == ["hb"]
    assert client.calls[0]["complete_fn"] is not None
    assert client.calls[0]["allow_tool_calls"] is True
    assert client.calls[0]["max_rounds"] is None
    assert client.calls[0]["timeout_s"] == 60


def test_max_rounds_1_avec_session_backends_utilise_session_mode(monkeypatch):
    events = []

    class FakeSession:
        def send(self, prompt, *, timeout_s, call_ctx=None):
            events.append(("send", prompt, timeout_s))
            return llm.LlmCompletion(provider="acpx", model="gpt-5.5", text="{}")

    def fake_run_with_session_fallback(backends, *, task_id, resolve, open_timeout_s):
        events.append(("runner", backends, task_id, open_timeout_s))
        return resolve(FakeSession())

    def fake_resolve_symbol_decision(**kwargs):
        events.append(("resolve", kwargs["max_rounds"], kwargs.get("reinject")))
        response = kwargs["call_model"]({SYMBOL: {"round": 1}}, allow_tool_calls=True)
        return response[SYMBOL]

    class SessionAwareClient:
        def __init__(self):
            self.calls = []

        def decide_batch(self, *, symbols, **kwargs):
            self.calls.append({"symbols": list(symbols), **kwargs})
            assert callable(kwargs["complete_fn"])
            kwargs["complete_fn"]("session prompt", 9)
            return {SYMBOL: _ok_decision("BUY")}

    monkeypatch.setattr(
        "trader.application.decide.one.llm.run_with_session_fallback",
        fake_run_with_session_fallback,
    )
    monkeypatch.setattr(
        "trader.application.decide.one.resolve_symbol_decision",
        fake_resolve_symbol_decision,
    )

    client = SessionAwareClient()
    backends = [object()]

    decision, calls = decide_one(
        **{**_BASE_KWARGS, "agent_tools_enabled": True},
        codex_client=client,
        tool_services=_services(),
        session_backends=backends,
        task_id="decide:AAPL",
    )

    assert decision.action == "BUY"
    assert calls == 1
    assert events == [
        ("runner", backends, "decide:AAPL", 75),
        ("resolve", SESSION_ROUND_BACKSTOP, "delta"),
        ("send", "session prompt", 9),
    ]
    assert client.calls[0]["complete_fn"] is not None
    assert client.calls[0]["max_rounds"] is None


def test_session_mode_tous_backends_down_leve_retryable(monkeypatch):
    failure = llm.LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="provider_error",
        message="internal error",
        retryable=True,
    )

    def fake_run_with_session_fallback(*args, **kwargs):
        return failure

    monkeypatch.setattr(
        "trader.application.decide.one.llm.run_with_session_fallback",
        fake_run_with_session_fallback,
    )

    client = _FakeClient({SYMBOL: _ok_decision("BUY")})

    with pytest.raises(RetryableError) as exc_info:
        decide_one(
            **{**_BASE_KWARGS, "agent_tools_enabled": True},
            codex_client=client,
            tool_services=_services(),
            session_backends=[object()],
            task_id="decide:AAPL",
        )

    assert exc_info.value.is_overload is True
    assert "provider_error" in str(exc_info.value)
