from trader import trade_plan
from trader import codex_client
from trader.codex_client import decide_batch
from trader.llm import LlmCompletion


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


def _trailing_stop_trail_types() -> tuple[str, ...]:
    assert hasattr(trade_plan, "TRAILING_STOP_TRAIL_TYPES")
    return tuple(trade_plan.TRAILING_STOP_TRAIL_TYPES)


def test_batch_contract_expose_les_trail_type_valides_derives_du_validateur() -> None:
    prompt = _batch_prompt_from_decide_batch()
    trail_type_enum = "|".join(_trailing_stop_trail_types())

    assert trail_type_enum in prompt


def test_batch_contract_detaille_hard_stop_et_take_profits() -> None:
    prompt = _batch_prompt_from_decide_batch()

    assert '`hard_stop`: nombre > 0 OU objet {type:"price", price:<requis, >0>}' in prompt
    assert (
        "`take_profits`: liste d'OBJETS "
        "{price:<requis, >0>, fraction:<optionnel, >0>}"
    ) in prompt


def test_exit_plan_contract_immediat_reste_en_prix_absolus() -> None:
    contract = codex_client._exit_plan_contract()

    assert '`hard_stop`: nombre > 0 OU objet {type:"price", price:<requis, >0>}' in contract
    assert "{type:\"percent\"" not in contract
    assert "{type:\"volatility_multiple\"" not in contract
    assert "{type:\"risk_multiple\"" not in contract


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
