from trader import trade_plan
from trader.codex_client import decide_batch
from trader.llm import LlmCompletion


class CapturingRouter:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion:
        self.prompt = prompt
        return LlmCompletion(
            provider="spark",
            model="gpt-5.3-codex-spark/medium",
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
