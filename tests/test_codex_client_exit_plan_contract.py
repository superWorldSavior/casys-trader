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
    assert "min_pct/max_pct sont des bornes de validation" in contract
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
    assert "min_pct/max_pct sont des bornes de validation" in armed_contract
    assert "ne déplace jamais le hard_stop" in armed_contract
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

    assert "hard_stop peut être en prix OU relatif" in prompt
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
    start = armed.index("Le hard_stop relatif est résolu")
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
              "tool": "propose_order",
              "args": {
                "intent": "OPEN_LONG",
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
        "propose_order",
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


def test_batch_parse_close_avec_side_produit_action_sell() -> None:
    """F2 : REDUCE/CLOSE/REVERSE exigent une side explicite. Avec side, le contrat
    compile vers l'action broker (SELL) tout en gardant l'intent CLOSE."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "these invalidee",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "CLOSE", "side": "SELL", "qty": 10}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "SELL"
    assert parsed.intent == "CLOSE"
    assert parsed.quantity == 10.0


def test_batch_parse_close_sans_side_produit_resolve_from_position() -> None:
    """L2 : CLOSE sans side → resolve_from_position=True ; le daemon résout depuis la
    position au lieu de tomber en HOLD dès le parsing. qty=10 ignoré (dérivé de |pos|)."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "these invalidee",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "CLOSE", "qty": 10}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "CLOSE"
    # action=HOLD provisoire — le daemon le remplacera par BUY ou SELL


def test_batch_parse_rejette_melange_legacy_et_calls() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "action": "BUY", "quantity": 1, "confidence": 0.8,
       "rationale": "ambigu", "calls": []}
    ]}
    """

    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "mixed_legacy_and_tools" in parsed.rationale


def test_batch_contract_documente_voir_et_corriger_ses_plans() -> None:
    """L'agent doit savoir qu'il VOIT ses plans actifs (`active_watches`) et peut
    les CORRIGER (`cancel_watch_ids` = annuler + reposer), au lieu d'empiler."""
    prompt = _batch_prompt_from_decide_batch()

    assert "active_watches" in prompt
    assert "cancel_watch_ids" in prompt
    low = prompt.lower()
    assert "annul" in low and "repose" in low  # corriger = annuler + reposer


def test_batch_contract_tools_par_symbole_remplace_le_schema_legacy_visible() -> None:
    prompt = _symbol_calls_prompt_from_decide_batch()

    assert '"calls":[' in prompt
    assert "propose_order" in prompt
    assert "protect" in prompt
    assert "lock_r" in prompt
    assert '"action":"BUY|SELL|HOLD"' not in prompt
    assert '"exit_plan":<object|null>' not in prompt
    assert 'Chaque <obj>: {"symbol":"<SYM>","action"' not in prompt


def test_symbol_calls_contract_laisse_l_agent_pull_au_premier_tour() -> None:
    """AX : au 1er passage (allow_tool_calls), le contrat symbol_calls ne DOIT PAS
    interdire la tournée read-only. L'agent choisit lui-même : tool_calls d'abord,
    OU directement le contrat final. Le « Réponds UNIQUEMENT » bridait ce choix."""
    prompt = _symbol_calls_prompt_from_decide_batch()  # allow_tool_calls=True

    assert 'Réponds UNIQUEMENT par {"decisions"' not in prompt
    assert '"tool_calls"' in prompt  # le pull reste offert
    assert "premier tour" in prompt.lower()  # clause explicite du choix


def test_symbol_calls_contract_documente_l2_position_aware() -> None:
    """L2 : le prompt doit indiquer que CLOSE/REDUCE dérivent side+qty de la position,
    et que REVERSE dérive la side (qty cible reste requise). `side` reste optionnel."""
    prompt = _symbol_calls_prompt_from_decide_batch()

    assert "CLOSE" in prompt
    assert "REDUCE" in prompt
    assert "REVERSE" in prompt
    # Le prompt indique que side est optionnel/dérivé pour CLOSE/REDUCE/REVERSE
    assert "fraction" in prompt  # REDUCE accepte fraction


def test_batch_parse_reduce_fraction_sans_side_produit_resolve_from_position() -> None:
    """L2 : REDUCE avec fraction=0.5 sans side → resolve_from_position=True,
    reduce_fraction=0.5. Le daemon calculera side=opposé, qty=0.5×|pos|."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "REDUCE", "fraction": 0.5}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "REDUCE"
    assert parsed.reduce_fraction == 0.5


def test_batch_parse_reduce_qty_abs_sans_side_produit_resolve_from_position() -> None:
    """L2 : REDUCE avec qty absolue sans side → resolve_from_position=True,
    reduce_fraction=None. Le daemon calculera side=opposé, qty=min(qty, |pos|)."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "scale-out partiel",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "REDUCE", "qty": 5}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "REDUCE"
    assert parsed.reduce_fraction is None
    assert parsed.quantity == 5.0


def test_batch_parse_reverse_sans_side_avec_qty_produit_resolve_from_position() -> None:
    """L2 : REVERSE sans side mais avec qty → resolve_from_position=True.
    qty de la nouvelle jambe est conservée ; le daemon dérivera la side."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "flip position",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "REVERSE", "qty": 20}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "REVERSE"
    assert parsed.quantity == 20.0


def test_batch_parse_reverse_sans_side_ni_qty_tombe_en_hold() -> None:
    """L2 : REVERSE sans qty reste une erreur — la jambe cible est ambiguë.
    Pas de résolution possible → HOLD tracé."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "flip position",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "REVERSE"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "order_qty_required" in parsed.rationale


def test_batch_parse_close_side_explicite_reste_inchange() -> None:
    """L2 compat : si side est fourni explicitement, comportement actuel maintenu.
    La présence de resolve_from_position=False confirme l'absence d'inférence."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.7, "rationale": "these invalidee",
       "decision_reason_code": "EXIT_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "CLOSE", "side": "SELL", "qty": 10}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "SELL"
    assert parsed.intent == "CLOSE"
    assert parsed.resolve_from_position is False


def test_batch_parse_add_sans_side_produit_resolve_from_position() -> None:
    """L4 : ADD sans side → resolve_from_position=True, qty conservée.
    Le daemon dérivera side = même sens que la position (BUY si long, SELL si short)."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.75, "rationale": "renforcement breakout",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "ADD", "qty": 5}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "ADD"
    assert parsed.quantity == 5.0


def test_batch_parse_add_sans_qty_tombe_en_hold() -> None:
    """L4 : ADD sans qty est une erreur — la taille du renforcement est requise."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.75, "rationale": "renforcement",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "ADD"}}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.action == "HOLD"
    assert "order_qty_required" in parsed.rationale


def test_batch_parse_add_avec_exit_plan_preserve_exit_plan() -> None:
    """L4 : ADD peut fournir un exit_plan (stop combiné) — il est conservé."""
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "pyramiding",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {
         "intent": "ADD", "qty": 3,
         "exit": {"stop": {"price": 145.0}}
       }}]}
    ]}
    """
    parsed = codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"]

    assert parsed.resolve_from_position is True
    assert parsed.intent == "ADD"
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
