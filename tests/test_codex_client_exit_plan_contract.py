import pytest

from trader.agent import client as codex_client
from trader.agent.client import decide_batch
from trader.agent.llm import LlmCompletion
from trader.planning import trade_plan


class CapturingRouter:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
        self.prompt = prompt
        return LlmCompletion(
            provider="acpx",
            model="gpt-5.5/medium",
            text=(
                '{"decisions":[{"symbol":"SPY","action":"HOLD","quantity":0,'
                '"confidence":0.5,"rationale":"attente"}]}'
            ),
        )


def _batch_prompt_from_decide_batch() -> str:
    router = CapturingRouter()
    decide_batch(
        symbols=["SPY"],
        mandate="(mandat)",
        memory="(memoire)",
        shared_context={"cockpit": {}},
        per_symbol={},
        llm_router=router,
    )
    return router.prompt


def _symbol_calls_prompt_from_decide_batch() -> str:
    router = CapturingRouter()
    decide_batch(
        symbols=["SPY"],
        mandate="(mandat)",
        memory="(memoire)",
        shared_context={"cockpit": {}},
        per_symbol={},
        allow_tool_calls=True,
        use_symbol_calls_contract=True,
        llm_router=router,
    )
    return router.prompt


def _symbol_calls_final_prompt_from_decide_batch() -> str:
    """Tour final : plus de tournée d'outils (allow_tool_calls=False)."""
    router = CapturingRouter()
    decide_batch(
        symbols=["SPY"],
        mandate="(mandat)",
        memory="(memoire)",
        shared_context={"cockpit": {}},
        per_symbol={},
        allow_tool_calls=False,
        use_symbol_calls_contract=True,
        llm_router=router,
    )
    return router.prompt


def _trailing_stop_trail_types() -> tuple[str, ...]:
    assert hasattr(trade_plan, "TRAILING_STOP_TRAIL_TYPES")
    return tuple(trade_plan.TRAILING_STOP_TRAIL_TYPES)


def test_batch_contract_expose_les_trail_type_valides_derives_du_validateur() -> None:
    prompt = _batch_prompt_from_decide_batch()
    trail_type_enum = "|".join(_trailing_stop_trail_types())

    assert trail_type_enum in prompt


def test_batch_contract_detaille_hard_stop_et_take_profits() -> None:
    prompt = _batch_prompt_from_decide_batch()

    assert 'objet {type:"price|percent|volatility_multiple|structural", ...}' in prompt
    assert "résolu mécaniquement au tir" in prompt
    assert (
        "`take_profits`: liste d'OBJETS "
        "{price:<requis, >0>, fraction:<optionnel, >0>} "
        'OU {type:"risk_multiple", r:<requis, >0>, fraction?}'
    ) in prompt


def test_exit_plan_contract_immediat_autorise_relatif_sans_rappel_llm() -> None:
    contract = codex_client._exit_plan_contract()

    assert 'objet {type:"price|percent|volatility_multiple|structural", ...}' in contract
    assert '{type:"percent", percent:<0..1>, min_pct?, max_pct?}' in contract
    assert '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?}' in contract
    assert (
        '{type:"structural", anchor:"swing_low|swing_high|vwap", '
        "window:<requis, >0>, buffer_pct?|buffer_atr?, min_pct?, max_pct?}"
    ) in contract
    assert '{type:"risk_multiple", r:<requis, >0>, fraction?}' in contract
    assert "résolu mécaniquement au tir" in contract
    assert "sans rappel LLM" in contract
    assert 'type:"price"' in contract
    assert "pass-through" in contract
    assert "min_pct/max_pct sont des bornes indicatives" in contract
    assert "signalé en warning, pas rejeté" in contract
    assert "ne déplace jamais le hard_stop" in contract


def test_section_plans_armes_documente_hard_stop_relatif_et_take_profit_en_r() -> None:
    armed_contract = codex_client._indicator_watch_vocabulary()

    assert '{type:"percent", percent:<0..1>, min_pct?, max_pct?}' in armed_contract
    assert '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?}' in armed_contract
    assert (
        '{type:"structural", anchor:"swing_low|swing_high|vwap", '
        "window:<requis, >0>, buffer_pct?|buffer_atr?, min_pct?, max_pct?}"
    ) in armed_contract
    assert "résolu en prix au déclenchement" in armed_contract
    assert "barres FRAÎCHES" in armed_contract
    assert "window est en barres du timeframe runtime" in armed_contract
    assert "niveau d'invalidation chartiste" in armed_contract
    assert "min_pct/max_pct sont des bornes indicatives" in armed_contract
    assert "signalé en warning, pas rejeté" in armed_contract
    assert "ne déplace jamais le stop" in armed_contract
    assert "swing_low" in armed_contract
    assert "swing_high" in armed_contract
    assert "vwap" in armed_contract
    assert '{type:"risk_multiple", r:<requis, >0>, fraction?}' in armed_contract


def test_batch_contract_ne_privilegie_aucune_forme_de_hard_stop_relatif() -> None:
    """Les formes relatives (percent / volatility_multiple / structural) sont des
    primitives à égalité : le contrat ne doit en RECOMMANDER aucune (anti-biais —
    le LLM choisit selon son régime). Le prix absolu n'est pas interdit, juste
    décrit factuellement (non recalibré au tir) — le choix reste à l'agent."""
    prompt = _batch_prompt_from_decide_batch()

    assert "`exit.stop` peut être en prix OU relatif" in prompt
    # plus aucune recommandation d'une forme particulière
    assert "RECOMMANDE volatility_multiple" not in prompt
    # les trois formes présentées à égalité, résolues au tir
    assert "percent, volatility_multiple ET structural, à égalité" in prompt
    # le prix absolu : fait mécanique, PAS une interdiction
    assert "n'est PAS recalibré au tir" in prompt
    assert "utilise-le seulement si c'est vraiment ton niveau d'invalidation" in prompt
    assert "à éviter pour un plan armé" not in prompt


def test_section_hard_stop_arme_ne_contient_aucun_terme_directif() -> None:
    """Anti-biais robuste : le bloc qui décrit le hard_stop d'un plan armé ne doit
    contenir AUCUN terme qui privilégie une forme (recommande/conseille/privilégie/
    préfère/plus robuste), pour les trois primitives. Chope toute réintroduction de
    biais sous une formulation différente, pas seulement la string d'origine."""
    armed = codex_client._indicator_watch_vocabulary()
    # borne le bloc hard_stop : de son intro jusqu'à la mention du TTL (exclut la
    # ligne « préfère un plan armé à un réveil court », biais D7 assumé hors scope)
    start = armed.index("Le stop relatif est résolu")
    end = armed.index("TTL max 4 h", start)
    stop_block = armed[start:end].lower()

    for terme in ("recommand", "conseill", "privilégi", "préfèr", "plus robuste", "de préférence"):
        assert terme not in stop_block, f"terme directif « {terme} » dans le bloc hard_stop"
    assert "à égalité" in stop_block
    for forme in ("percent", "volatility_multiple", "structural"):
        assert forme in stop_block


def test_batch_contract_clarifie_les_unites_du_trailing_stop() -> None:
    prompt = _batch_prompt_from_decide_batch()

    assert "percent = fraction" in prompt
    assert "0.004 = 0.4%" in prompt
    assert "price = distance absolue en prix" in prompt
    assert "volatility_multiple = multiple de la volatilité récente" in prompt
    # pas de fourchette de valeur suggérée : on n'oriente pas le choix de l'agent
    assert "1.5-3" not in prompt
    assert "ne s'arme qu'une fois en profit" in prompt


def test_decision_parse_cancel_watch_ids() -> None:
    """Le champ de correction `cancel_watch_ids` : absent → liste vide ; présent →
    liste de str (valeurs non-str ignorées défensivement)."""
    base = {"symbol": "SPY", "action": "HOLD", "quantity": 0,
            "confidence": 0.5, "rationale": "x"}

    assert codex_client._decision_from_dict(dict(base), "SPY").cancel_watch_ids == []

    parsed = codex_client._decision_from_dict(
        {**base, "cancel_watch_ids": ["SPY:abc", 123, "SPY:def"]}, "SPY"
    )
    assert parsed.cancel_watch_ids == ["SPY:abc", "SPY:def"]

    # conteneur non-liste (string/dict) → [] et NON une itération sur les caractères
    assert codex_client._decision_from_dict({**base, "cancel_watch_ids": "SPY:abc"}, "SPY").cancel_watch_ids == []
    assert codex_client._decision_from_dict({**base, "cancel_watch_ids": {"x": 1}}, "SPY").cancel_watch_ids == []


def test_batch_parse_compile_calls_par_symbole_en_decision_interne() -> None:
    raw = """
    {
      "decisions": [
        {
          "symbol": "DASH",
          "confidence": 0.74,
          "rationale": "breakout propre",
          "decision_reason_code": "ENTRY_SIGNAL",
          "calls": [
            {
              "tool": "strategy_entry",
              "args": {
                "direction": "long",
                "qty": 20,
                "exit": {
                  "stop": {"struct": "swing_low", "window": 24, "buffer_pct": 0.004},
                  "tp": [{"r": 1.4, "fraction": 0.5}, {"r": 2.2, "fraction": 0.5}],
                  "trail": {"type": "percent", "value": 0.018},
                  "protect": {"arm_r": 1.0, "giveback": 0.35, "close_fraction": 0.5, "lock_r": 0.25}
                }
              }
            },
            {"tool": "set_next_wake", "args": {"minutes": 15}},
            {"tool": "record_learning", "args": {"note": "breakout stretched: surveiller le stop structural"}}
          ]
        }
      ]
    }
    """

    parsed = codex_client.parse_batch(raw, ["DASH"], allow_context_request=False)["DASH"]

    assert parsed.action == "BUY"
    assert parsed.intent == "OPEN_LONG"
    assert parsed.quantity == 20.0
    assert parsed.next_wake_in_minutes == 15.0
    assert parsed.learning == "breakout stretched: surveiller le stop structural"
    assert parsed.exit_plan == {
        "hard_stop": {"type": "structural", "anchor": "swing_low", "window": 24, "buffer_pct": 0.004},
        "take_profits": [{"type": "risk_multiple", "r": 1.4, "fraction": 0.5}, {"type": "risk_multiple", "r": 2.2, "fraction": 0.5}],
        "trailing_stop": {"trail_type": "percent", "trail_value": 0.018},
        "profit_protection": {
            "arm_at_r": 1.0,
            "trigger_on_giveback_pct": 0.35,
            "close_fraction": 0.5,
            "lock_r": 0.25,
        },
    }
    assert parsed.domain_tools is not None
    assert [call["tool"] for call in parsed.domain_tools["tool_calls"]] == [
        "strategy_entry",
        "set_next_wake",
        "record_learning",
    ]


def test_batch_parse_calls_vides_signifie_hold_explicite() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.61, "rationale": "setup trop tendu",
       "decision_reason_code": "NO_EDGE", "calls": []}
    ]}
    """

    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "HOLD"
    assert parsed.quantity == 0.0
    assert parsed.confidence == 0.61
    assert parsed.rationale == "setup trop tendu"
    assert parsed.domain_tools == {"tool_rounds": 0, "tool_calls": []}


def test_batch_parse_strategy_entry_compile_en_open_long() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.82, "rationale": "breakout",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "strategy_entry", "args": {
         "id": "long",
         "direction": "long",
         "qty": 20,
         "exit": {"id": "bracket", "limit": 110.0, "stop": 95.0}
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "BUY"
    assert parsed.intent == "OPEN_LONG"
    assert parsed.quantity == 20.0
    assert parsed.resolve_from_position is True
    assert parsed.exit_plan == {
        "hard_stop": 95.0,
        "take_profits": [{"type": "price", "price": 110.0, "fraction": 1.0, "name": "bracket"}],
    }
    assert parsed.domain_tools is not None
    assert parsed.domain_tools["tool_calls"][0]["tool"] == "strategy_entry"


def test_batch_parse_strategy_exit_compile_en_exit_update_limit_only() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_exit", "args": {
         "id": "tp1",
         "from_entry": "long",
         "limit": 112.0,
         "qty_percent": 50
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "HOLD"
    assert parsed.exit_update == {
        "take_profits": [{"type": "price", "price": 112.0, "fraction": 0.5, "name": "tp1"}],
    }
    assert parsed.domain_tools is not None
    assert parsed.domain_tools["tool_calls"][0]["tool"] == "strategy_exit"


def test_batch_parse_strategy_exit_rejette_bracket_partiel_non_supporte() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "partial bracket",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_exit", "args": {
         "id": "tp1",
         "limit": 112.0,
         "stop": 96.0,
         "qty_percent": 50
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "partial_bracket_exit_not_supported" in parsed.rationale


def test_batch_parse_strategy_exit_rejette_stop_partiel_non_supporte() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "partial stop",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_exit", "args": {
         "id": "stop-half",
         "stop": 96.0,
         "qty_percent": 50
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "partial_stop_exit_not_supported" in parsed.rationale


def test_batch_parse_strategy_exit_rejette_conflit_avec_strategy_entry() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "mixed",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [
         {"tool": "strategy_entry", "args": {"direction": "long", "qty": 10}},
         {"tool": "strategy_exit", "args": {"limit": 112.0}}
       ]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "strategy_exit_conflicts_with_strategy_entry" in parsed.rationale
    assert "strategy_exit_conflicts_with_position_order" not in parsed.rationale


def test_batch_parse_strategy_close_compile_en_close_position_aware() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "invalidated",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {"id": "long"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "CLOSE"
    assert parsed.quantity == 0.0
    assert parsed.resolve_from_position is True
    assert parsed.domain_tools is not None
    assert parsed.domain_tools["tool_calls"][0]["tool"] == "strategy_close"


def test_batch_parse_strategy_close_qty_percent_compile_en_reduce_fraction() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale down",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {"qty_percent": 50}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "REDUCE"
    assert parsed.resolve_from_position is True
    assert parsed.reduce_fraction == 0.5
    assert parsed.domain_tools is not None
    assert parsed.domain_tools["tool_calls"][0]["tool"] == "strategy_close"


def test_batch_parse_close_avec_side_explicite_ignore_side_et_resout_position() -> None:
    """strategy_close dérive depuis la position et ignore les champs side/action."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "these invalidee",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {"side": "SELL"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "CLOSE"
    assert parsed.quantity == 0.0
    assert parsed.resolve_from_position is True


def test_batch_parse_close_sans_side_produit_resolve_from_position() -> None:
    """L2 : CLOSE sans side → resolve_from_position=True ; le daemon résout depuis la
    position au lieu de tomber en HOLD dès le parsing."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "these invalidee",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "CLOSE"
    # action=HOLD provisoire — le daemon le remplacera par BUY ou SELL


def test_batch_parse_rejette_melange_inline_et_calls() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "action": "BUY", "quantity": 1, "confidence": 0.8,
       "rationale": "ambigu", "calls": []}
    ]}
    """

    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "mixed_inline_decision_and_tools" in parsed.rationale


def test_batch_parse_protege_intent_relatif_inline_avec_action_explicite() -> None:
    """Un intent position-aware inline ignore action et passe en résolution position."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "action": "SELL", "quantity": 20, "confidence": 0.8,
       "rationale": "inline flip", "intent": "FLIP",
       "decision_reason_code": "REVERSAL"}
    ]}
    """

    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert parsed.intent == "FLIP"
    assert parsed.quantity == 20.0
    assert parsed.resolve_from_position is True


def test_batch_contract_documente_voir_et_corriger_ses_plans() -> None:
    """L'agent doit savoir qu'il VOIT ses plans actifs (`active_watches`) et peut
    les CORRIGER (`cancel_watch` = annuler + reposer), au lieu d'empiler."""
    prompt = _batch_prompt_from_decide_batch()

    assert "active_watches" in prompt
    assert "cancel_watch" in prompt
    assert "cancel_watch_ids" not in prompt
    assert "propose_indicator_watch" in prompt
    low = prompt.lower()
    assert "annul" in low and "repose" in low  # corriger = annuler + reposer


def test_batch_contract_tools_par_symbole_remplace_le_schema_legacy_visible() -> None:
    prompt = _symbol_calls_prompt_from_decide_batch()

    assert '"calls":[' in prompt
    assert "strategy_entry" in prompt
    assert "strategy_exit" in prompt
    assert "strategy_close" in prompt
    assert '"action":"BUY|SELL|HOLD"' not in prompt
    assert '"exit_plan":<object|null>' not in prompt
    assert 'Chaque <obj>: {"symbol":"<SYM>","action"' not in prompt
    assert "propose_order" not in prompt
    assert "exit_update" not in prompt


def test_symbol_calls_contract_expose_une_grammaire_trading_canonique() -> None:
    prompt = _symbol_calls_final_prompt_from_decide_batch()

    assert "Grammaire Pine-like JSON officielle" in prompt
    assert "MCP JSON inspiré de Pine Script" in prompt
    assert "strategy.entry/strategy.exit/strategy.close" in prompt
    assert "jamais du code Pine Script" in prompt
    assert "position intent = changer l'exposition" in prompt
    assert "exit rule = règle attachée à une position ouverte" in prompt
    assert "review wake = reconsultation par le LLM" in prompt
    assert "armed plan = exécution daemon sans reconsultation" in prompt
    assert "strategy_entry = position intent" in prompt
    assert "strategy_exit = exit rule" in prompt
    assert "strategy_close = sortie marché immédiate" in prompt
    assert "set_next_wake = review wake" in prompt
    assert "propose_indicator_watch = armed plan" in prompt
    assert "strategy.exit Pine" in prompt
    assert "même sens = renforcement" in prompt
    assert "sens opposé = retournement" in prompt
    assert "limit+stop dans strategy_exit = bracket de sortie" in prompt
    assert "ne combine pas strategy_entry et strategy_exit sur le même symbole" in prompt
    assert "aliases acceptés" not in prompt


def test_symbol_calls_contract_laisse_l_agent_pull_au_premier_tour() -> None:
    """AX : au 1er passage (allow_tool_calls), le contrat symbol_calls ne DOIT PAS
    interdire la tournée read-only. L'agent choisit lui-même : tool_calls d'abord,
    OU directement le contrat final. Le « Réponds UNIQUEMENT » bridait ce choix."""
    prompt = _symbol_calls_prompt_from_decide_batch()  # allow_tool_calls=True

    assert 'Réponds UNIQUEMENT par {"decisions"' not in prompt
    assert '"tool_calls"' in prompt  # le pull reste offert
    assert "premier tour" in prompt.lower()  # clause explicite du choix


def test_symbol_calls_contract_documente_l2_position_aware() -> None:
    """L2 : le prompt doit indiquer que strategy_close dérive side+qty de la position."""
    prompt = _symbol_calls_prompt_from_decide_batch()

    assert "strategy_close" in prompt
    assert "sortie marché immédiate" in prompt
    assert "Sans taille, ferme toute la position" in prompt
    assert "qty_percent<100 réduit une fraction" in prompt
    assert "qty réduit une quantité absolue" in prompt
    assert "side/action" not in prompt
    assert "il est rejeté" not in prompt
    assert "fraction" in prompt


def test_symbol_calls_contract_documente_strategy_exit_pine_like() -> None:
    prompt = _symbol_calls_prompt_from_decide_batch()

    assert "strategy_exit" in prompt
    assert "from_entry" in prompt
    assert "qty_percent" in prompt
    assert "limit+stop dans strategy_exit = bracket de sortie" in prompt
    assert "partial_bracket_exit_not_supported" in prompt
    assert "qty_percent ne s'applique qu'à limit seul" in prompt
    assert "partial_stop_exit_not_supported" in prompt
    assert "un stop structurel peut aussi protéger un gain" in prompt
    assert "du bon côté du prix courant" in prompt


def test_batch_parse_reduce_fraction_sans_side_produit_resolve_from_position() -> None:
    """L2 : REDUCE avec fraction=0.5 sans side → resolve_from_position=True,
    reduce_fraction=0.5. Le daemon calculera side=opposé, qty=0.5×|pos|."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {"qty_percent": 50}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "REDUCE"
    assert parsed.reduce_fraction == 0.5


@pytest.mark.parametrize("qty_percent", [-10, 0, 120])
def test_batch_parse_reduce_fraction_hors_borne_tombe_en_hold(qty_percent: int) -> None:
    raw = f"""
    {{"decisions": [
      {{"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{{"tool": "strategy_close", "args": {{"qty_percent": {qty_percent}}}}}]}}
    ]}}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "qty_percent_out_of_range" in parsed.rationale


def test_batch_parse_reduce_qty_abs_sans_side_produit_resolve_from_position() -> None:
    """L2 : REDUCE avec qty absolue sans side → resolve_from_position=True,
    reduce_fraction=None. Le daemon calculera side=opposé, qty=min(qty, |pos|)."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out partiel",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "strategy_close", "args": {"qty": 5}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "REDUCE"
    assert parsed.reduce_fraction is None
    assert parsed.quantity == 5.0


def test_batch_parse_strategy_entry_opposee_prepare_flip_position_aware() -> None:
    """strategy_entry prépare un changement d'exposition ; le daemon dérivera FLIP si position opposée."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "flip position",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "short", "qty": 20}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "OPEN_SHORT"
    assert parsed.quantity == 20.0


def test_batch_parse_strategy_entry_sans_qty_tombe_en_hold() -> None:
    """strategy_entry sans qty/risk_pct ne peut pas dimensionner la jambe cible."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "flip position",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "short"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "order_qty_required" in parsed.rationale


@pytest.mark.parametrize("direction,qty,expected_intent", [
    ("short", 20, "OPEN_SHORT"),
    ("long", 5, "OPEN_LONG"),
])
def test_batch_parse_strategy_entry_position_aware(
    direction: str,
    qty: int,
    expected_intent: str,
) -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "relative intent",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "%s", "qty": %d}}]}
    ]}
    """ % (direction, qty)
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action in {"BUY", "SELL"}
    assert parsed.intent == expected_intent
    assert parsed.quantity == pytest.approx(qty)
    assert parsed.resolve_from_position is True


@pytest.mark.parametrize("qty", [0, -1])
def test_batch_parse_strategy_entry_rejette_qty_non_positive(qty: int) -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "bad qty",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "long", "qty": %d}}]}
    ]}
    """ % qty
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "order_qty_must_be_positive" in parsed.rationale


def test_batch_parse_strategy_entry_prepare_scale_in_position_aware() -> None:
    """strategy_entry dans le même sens prépare un SCALE_IN dans l'admission position-aware."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.75, "rationale": "renforcement breakout",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "long", "qty": 5}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "OPEN_LONG"
    assert parsed.quantity == 5.0


def test_batch_parse_strategy_entry_sans_qty_tombe_en_hold_aussi_pour_scale_in() -> None:
    """strategy_entry sans qty/risk_pct reste une erreur avant résolution SCALE_IN."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.75, "rationale": "renforcement",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "strategy_entry", "args": {"direction": "long"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "order_qty_required" in parsed.rationale


def test_batch_parse_strategy_entry_avec_exit_plan_preserve_exit_plan() -> None:
    """strategy_entry peut fournir un exit_plan (stop combiné) — il est conservé."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "pyramiding",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "strategy_entry", "args": {
         "direction": "long", "qty": 3,
         "exit": {"stop": {"price": 145.0}}
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "OPEN_LONG"
    assert parsed.exit_plan is not None
    assert parsed.exit_plan["hard_stop"]["price"] == 145.0


def test_symbol_calls_contract_impose_decisions_au_tour_final() -> None:
    """Au tour final (allow_tool_calls=False), plus de tournée : le contrat impose
    la réponse `decisions` et ne propose plus le catalogue d'outils."""
    prompt = _symbol_calls_final_prompt_from_decide_batch()

    assert 'Réponds UNIQUEMENT par {"decisions"' in prompt
    assert "# Outils domaine" not in prompt  # catalogue read-only absent au tour final


def test_batch_contract_et_validate_exit_plan_utilisent_la_meme_constante(monkeypatch) -> None:
    trail_types = _trailing_stop_trail_types()
    extended_trail_types = trail_types + ("synthetic_test_trail_type",)
    monkeypatch.setattr(trade_plan, "TRAILING_STOP_TRAIL_TYPES", extended_trail_types)

    prompt = _batch_prompt_from_decide_batch()

    assert "|".join(extended_trail_types) in prompt
    trade_plan.validate_exit_plan(
        {
            "hard_stop": 95.0,
            "trailing_stop": {
                "trail_type": "synthetic_test_trail_type",
                "trail_value": 1.0,
            },
        }
    )
