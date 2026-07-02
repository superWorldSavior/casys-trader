from trader import codex_client
from trader.codex_client import DEFAULT_MODEL, ContextResearchRequest, build_batch_prompt, parse_decision, parse_decision_or_context_request, parse_batch, decide, decide_batch
from trader.llm import LlmCompletion, LlmFailure


def test_decide_batch_renvoie_une_decision_par_symbole_avec_metadonnees() -> None:
    class StubRouter:
        def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
            return LlmCompletion(
                provider="acpx",
                model="gpt-5.5/medium",
                text=(
                    '{"decisions": ['
                    '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.7,"rationale":"x","intent":"OPEN_LONG"},'
                    '{"symbol":"QQQ","action":"HOLD","quantity":0,"confidence":0.5,"rationale":"y"}'
                    ']}'
                ),
            )

    result = decide_batch(
        symbols=["SPY", "QQQ"],
        mandate="# M",
        memory="# Mem",
        shared_context={"cockpit": {}},
        per_symbol={"SPY": {"indicator_triggers": []}},
        llm_router=StubRouter(),
    )

    assert result["SPY"].action == "BUY"
    assert result["SPY"].llm_provider == "acpx"
    assert result["QQQ"].action == "HOLD"


def test_prompt_documente_rs_court_rs_daily_et_regime_family_daily() -> None:
    prompt = build_batch_prompt(
        mandate="m",
        memory="mem",
        shared_context={"cockpit": {"schema": "rs=short,rs_d=daily"}},
        symbols_payload=[],
    )

    assert "rs=force relative courte" in prompt
    assert "rs_d=force relative daily" in prompt
    assert "~3 séances" in prompt
    assert "~45 min" not in prompt


def test_prompt_demande_un_reason_code_structure() -> None:
    prompt = build_batch_prompt(
        mandate="m",
        memory="mem",
        shared_context={},
        symbols_payload=[],
    )

    assert "decision_reason_code" in prompt
    assert "WAITING_PULLBACK" in prompt
    assert "POST_LOSS_CAUTION" in prompt


def test_prompt_ne_pousse_pas_un_max_hold_par_defaut() -> None:
    prompt = build_batch_prompt(
        mandate="m",
        memory="mem",
        shared_context={},
        symbols_payload=[],
    )
    low = prompt.lower()

    assert "max_hold_minutes est optionnel" in low
    assert "expiration temporelle" in low
    assert "et/ou max_hold_minutes" not in prompt
    assert "fournis autant que possible" not in low


def test_contrat_decision_requiert_un_reason_code_structure() -> None:
    assert "decision_reason_code" in codex_client._DECISION_KEYS


def test_decide_utilise_un_plafond_decisionnel_900s_par_defaut() -> None:
    class StubRouter:
        def __init__(self) -> None:
            self.timeout_s = None

        def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
            self.timeout_s = timeout_s
            return LlmCompletion(
                provider="acpx",
                model="gpt-5.5/medium",
                text='{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,"rationale":"attente"}',
            )

    router = StubRouter()

    result = decide(
        symbol="SPY",
        mandate="",
        memory="",
        context={},
        llm_router=router,
    )

    assert result.action == "HOLD"
    assert router.timeout_s == 900


def test_decide_batch_utilise_un_plafond_decisionnel_900s_par_defaut() -> None:
    class StubRouter:
        def __init__(self) -> None:
            self.timeout_s = None

        def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
            self.timeout_s = timeout_s
            return LlmCompletion(
                provider="acpx",
                model="gpt-5.5/medium",
                text='{"decisions":[{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,"rationale":"attente"}]}',
            )

    router = StubRouter()

    result = decide_batch(
        symbols=["SPY"],
        mandate="",
        memory="",
        shared_context={},
        per_symbol={},
        llm_router=router,
    )

    assert result["SPY"].action == "HOLD"
    assert router.timeout_s == 900


def test_decide_batch_echec_llm_met_tout_en_hold() -> None:
    class FailRouter:
        def complete(self, prompt: str, *, timeout_s: int) -> LlmFailure:
            return LlmFailure(provider="acpx", model="m", code="timeout", message="boom", retryable=True)

    result = decide_batch(
        symbols=["SPY", "QQQ"],
        mandate="",
        memory="",
        shared_context={},
        per_symbol={},
        llm_router=FailRouter(),
    )

    assert result["SPY"].action == "HOLD"
    assert result["QQQ"].action == "HOLD"
    assert result["SPY"].llm_error == "timeout"


def test_parse_batch_decode_un_tableau_par_symbole() -> None:
    text = (
        '{"decisions": ['
        '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.7,"rationale":"breakout","intent":"OPEN_LONG"},'
        '{"symbol":"QQQ","action":"HOLD","quantity":0,"confidence":0.5,"rationale":"range"}'
        ']}'
    )
    result = parse_batch(text, ["SPY", "QQQ"], allow_context_request=False)
    assert set(result.keys()) == {"SPY", "QQQ"}
    assert result["SPY"].action == "BUY"
    assert result["QQQ"].action == "HOLD"


def test_parse_batch_isole_un_element_corrompu() -> None:
    text = (
        '{"decisions": ['
        '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.7,"rationale":"ok","intent":"OPEN_LONG"},'
        '{"symbol":"QQQ","action":"WAT","quantity":"x"}'  # action invalide + quantity non-numérique
        ']}'
    )
    result = parse_batch(text, ["SPY", "QQQ"], allow_context_request=False)
    assert result["SPY"].action == "BUY"
    assert result["QQQ"].action == "HOLD"  # l'élément pourri retombe en HOLD, l'autre passe


def test_parse_batch_symbole_absent_devient_hold() -> None:
    text = '{"decisions": [{"symbol":"SPY","action":"BUY","quantity":5,"confidence":0.6,"rationale":"x","intent":"OPEN_LONG"}]}'
    result = parse_batch(text, ["SPY", "QQQ"], allow_context_request=False)
    assert result["SPY"].action == "BUY"
    assert result["QQQ"].action == "HOLD"
    assert "missing" in result["QQQ"].rationale


def test_parse_batch_accepte_request_context_par_element() -> None:
    text = (
        '{"decisions": ['
        '{"symbol":"SPY","action":"REQUEST_CONTEXT","rationale":"besoin z","requests":[{"symbol":"SPY","indicators":["z_score"],"timeframe":"1h"}]},'
        '{"symbol":"QQQ","action":"HOLD","quantity":0,"confidence":0.5,"rationale":"range"}'
        ']}'
    )
    result = parse_batch(text, ["SPY", "QQQ"], allow_context_request=True)
    assert isinstance(result["SPY"], ContextResearchRequest)
    assert result["SPY"].requests[0].indicators == ["z_score"]
    assert result["QQQ"].action == "HOLD"


def test_parse_batch_json_global_invalide_tout_en_hold() -> None:
    result = parse_batch("pas du json", ["SPY", "QQQ"], allow_context_request=False)
    assert result["SPY"].action == "HOLD"
    assert result["QQQ"].action == "HOLD"


def test_default_model_utilise_modele_acpx_annonce() -> None:
    assert DEFAULT_MODEL == "gpt-5.5"


def test_parse_decision_accepte_next_wake_in_minutes_optionnel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range","next_wake_in_minutes":45}',
        "SPY",
    )

    assert decision.next_wake_in_minutes == 45.0


def test_parse_decision_accepte_intent_et_exit_plan() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.8,'
        '"rationale":"breakout","intent":"OPEN_LONG",'
        '"exit_plan":{"hard_stop":{"type":"price","price":95},'
        '"take_profits":[{"name":"tp1","price":105,"fraction":0.5}]}}',
        "SPY",
    )

    assert decision.intent == "OPEN_LONG"
    assert decision.exit_plan["hard_stop"]["price"] == 95


def test_parse_decision_accepte_indicator_watch_multi_timeframe() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.7,'
        '"rationale":"attente breakout",'
        '"indicator_watch":{"ttl_minutes":90,"logic":"all","on_trigger":"ORDER",'
        '"conditions":[{"symbol":"SPY","indicator":"z_score","op":">=",'
        '"value":1.8,"interval":"15m","window":32},'
        '{"symbol":"QQQ","indicator":"return","op":">","value":0.01,'
        '"interval":"1h","window":24}],'
        '"order":{"action":"BUY","quantity":10,"intent":"OPEN_LONG"}}}',
        "SPY",
    )

    assert decision.indicator_watch["ttl_minutes"] == 90
    assert decision.indicator_watch["conditions"][0]["interval"] == "15m"
    assert decision.indicator_watch["order"]["action"] == "BUY"


def test_parse_decision_accepte_un_learning_optionnel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.6,'
        '"rationale":"range serre",'
        '"learning":"le range tient depuis 3 reveils, j attends une cassure nette"}',
        "SPY",
    )

    assert decision.learning == "le range tient depuis 3 reveils, j attends une cassure nette"


def test_parse_decision_accepte_un_reason_code_structure() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.6,'
        '"rationale":"j attends un retest","decision_reason_code":"waiting_pullback"}',
        "SPY",
    )

    assert decision.decision_reason_code == "WAITING_PULLBACK"


def test_parse_decision_reason_code_inconnu_devient_unknown() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.6,'
        '"rationale":"range","decision_reason_code":"maybe_later"}',
        "SPY",
    )

    assert decision.decision_reason_code == "UNKNOWN"


def test_parse_decision_sans_reason_code_reste_tolere_et_utilise_le_fallback_legacy() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"BUY","quantity":10,"confidence":0.8,'
        '"rationale":"cassure exploitable","intent":"OPEN_LONG"}',
        "SPY",
    )

    assert decision.action == "BUY"
    assert decision.quantity == 10.0
    assert decision.decision_reason_code == "ENTRY_SIGNAL"


def test_parse_decision_ignore_un_learning_non_textuel() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,'
        '"rationale":"r","learning":{"a":1}}',
        "SPY",
    )

    assert decision.learning is None


def test_parse_decision_borne_la_longueur_du_learning() -> None:
    huge = "x" * 5000
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.5,'
        f'"rationale":"r","learning":"{huge}"}}',
        "SPY",
    )

    assert decision.learning is not None
    assert len(decision.learning) <= 1000


def test_parse_decision_learning_absent_vaut_none() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range"}',
        "SPY",
    )

    assert decision.learning is None


def test_parse_decision_garde_compatibilite_sans_next_wake() -> None:
    decision = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.8,'
        '"rationale":"range"}',
        "SPY",
    )

    assert decision.next_wake_in_minutes is None


def test_parse_decision_or_context_request_accepte_une_requete_indicateurs() -> None:
    response = parse_decision_or_context_request(
        '{"symbol":"SPY","action":"REQUEST_CONTEXT","rationale":"besoin de confirmer",'
        '"requests":[{"symbol":"QQQ","indicators":["z_score","spread_zscore"],'
        '"timeframe":"4h","lookback":"1mo","window":24}]}',
        "SPY",
    )

    assert isinstance(response, ContextResearchRequest)
    assert response.requests[0].symbol == "QQQ"
    assert response.requests[0].indicators == ["z_score", "spread_zscore"]
    assert response.requests[0].timeframe == "4h"
    assert response.requests[0].lookback == "1mo"
    assert response.requests[0].as_of == "latest"
    assert response.requests[0].window == 24


def test_parse_decision_or_context_request_accepte_indicator_singulier() -> None:
    response = parse_decision_or_context_request(
        '{"symbol":"SPY","action":"REQUEST_CONTEXT","rationale":"besoin de confirmer",'
        '"requests":[{"symbol":"SPY","indicator":"z_score","timeframe":"1h"}]}',
        "SPY",
    )

    assert isinstance(response, ContextResearchRequest)
    assert response.requests[0].indicators == ["z_score"]
    assert response.requests[0].timeframe == "1h"


def test_decide_attache_les_metadonnees_llm() -> None:
    class StubRouter:
        def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
            return LlmCompletion(
                provider="ollama-cloud",
                model="nemotron-3-nano:30b-cloud",
                fallback_reason="acpx:rate_limited",
                text=(
                    '{"symbol":"SPY","action":"HOLD","quantity":0,'
                    '"confidence":0.9,"rationale":"flat"}'
                ),
            )

    decision = decide(
        symbol="SPY",
        mandate="# Mandat",
        memory="# Memoire",
        context={},
        llm_router=StubRouter(),
    )

    assert decision.llm_provider == "ollama-cloud"
    assert decision.llm_model == "nemotron-3-nano:30b-cloud"
    assert decision.llm_fallback_reason == "acpx:rate_limited"


# ── Task 7 : parse_batch_or_tool_calls + BatchToolCallRequest ─────────────────

def test_parse_batch_or_tool_calls_detecte_une_tournee():
    raw = '{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=True)
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert out.calls[0]["tool"] == "get_freshness"


def test_parse_batch_or_tool_calls_ignore_les_elements_non_objets():
    raw = '{"tool_calls": [42, {"id": "c1", "tool": "t", "args": {}}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=True)
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert len(out.calls) == 1


def test_parse_batch_or_tool_calls_tombe_sur_les_decisions():
    raw = '{"decisions": [{"symbol": "2330.TW", "action": "HOLD", "quantity": 0, "confidence": 0.1, "rationale": "r", "decision_reason_code": "NO_EDGE"}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)
    assert out["2330.TW"].action == "HOLD"


def test_parse_batch_or_tool_calls_json_invalide_hold_global():
    out = codex_client.parse_batch_or_tool_calls("pas du json", ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)
    assert out["2330.TW"].rationale == "batch_bad_output"


def test_parse_batch_or_tool_calls_tool_calls_vides_ne_declenchent_pas():
    raw = '{"tool_calls": [], "decisions": [{"symbol": "2330.TW", "action": "HOLD", "quantity": 0, "confidence": 0, "rationale": "r", "decision_reason_code": "NO_EDGE"}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)


# ── Task 8 : catalogue d'outils au prompt + decide_batch(allow_tool_calls) ────

def test_build_batch_prompt_sans_flag_ne_mentionne_pas_les_outils():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "2330.TW"}],
        allow_context_request=True,
    )
    assert "tool_calls" not in prompt


def test_build_batch_prompt_avec_flag_expose_le_catalogue():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "2330.TW"}],
        allow_context_request=True, allow_tool_calls=True,
    )
    assert '"tool_calls"' in prompt
    assert "get_freshness" in prompt
    assert "get_indicator_context" in prompt


def test_decide_batch_retourne_la_tournee_quand_le_llm_la_demande(monkeypatch):
    class _Router:
        def complete(self, prompt, *, timeout_s):
            return LlmCompletion(
                provider="acpx", model="gpt-5.5",
                text='{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}]}',
                fallback_reason=None,
            )
    out = codex_client.decide_batch(
        symbols=["2330.TW"], mandate="m", memory="mem", shared_context={},
        per_symbol={"2330.TW": {}}, llm_router=_Router(), allow_tool_calls=True,
    )
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert out.llm_provider == "acpx"


def test_decide_batch_sans_flag_ignore_les_tool_calls(monkeypatch):
    class _Router:
        def complete(self, prompt, *, timeout_s):
            return LlmCompletion(
                provider="acpx", model="gpt-5.5",
                text='{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {}}]}',
                fallback_reason=None,
            )
    out = codex_client.decide_batch(
        symbols=["2330.TW"], mandate="m", memory="mem", shared_context={},
        per_symbol={"2330.TW": {}}, llm_router=_Router(), allow_tool_calls=False,
    )
    # flag éteint => parse_batch classique => pas de clé decisions => HOLD fail-safe
    assert isinstance(out, dict)
    assert out["2330.TW"].action == "HOLD"


# ---------------------------------------------------------------------------
# Task 11 : outils sémantiques dans le catalogue prompt
# ---------------------------------------------------------------------------


def test_catalogue_prompt_expose_les_outils_semantiques():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "SPY"}],
        allow_context_request=True, allow_tool_calls=True)
    assert "describe_data" in prompt
    assert "find_indicators" in prompt
