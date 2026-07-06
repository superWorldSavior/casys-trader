import pytest

from trader.agent.protocol.strategy_language import compile_strategy_call


def test_compile_strategy_entry_produit_primitive_position_pine_like() -> None:
    compiled = compile_strategy_call(
        "strategy_entry",
        {"direction": "long", "qty": 3, "exit": {"id": "bracket", "limit": 110, "stop": 95}},
    )

    assert compiled.primitive == "position_order"
    assert compiled.dialect == "pine_json"
    assert compiled.position_aware is True
    assert compiled.args == {
        "intent": "OPEN_LONG",
        "qty": 3,
        "exit": {
            "stop": 95,
            "tp": [{"type": "price", "price": 110, "fraction": 1.0, "name": "bracket"}],
        },
    }


def test_compile_strategy_exit_produit_primitive_exit_rule() -> None:
    compiled = compile_strategy_call("strategy_exit", {"id": "tp1", "limit": 112, "qty_percent": 50})

    assert compiled.primitive == "exit_rule"
    assert compiled.dialect == "pine_json"
    assert compiled.args == {
        "tp": [{"type": "price", "price": 112, "fraction": 0.5, "name": "tp1"}],
    }


def test_compile_strategy_close_produit_position_order_position_aware() -> None:
    compiled = compile_strategy_call("strategy_close", {"qty_percent": 50})

    assert compiled.primitive == "position_order"
    assert compiled.position_aware is True
    assert compiled.args == {"intent": "REDUCE", "fraction": 0.5}


@pytest.mark.parametrize("tool", ["propose_order", "exit_update"])
def test_compile_rejette_les_anciens_outils_action(tool: str) -> None:
    with pytest.raises(ValueError, match=f"unknown_action_tool:{tool}"):
        compile_strategy_call(tool, {"intent": "OPEN_LONG", "qty": 1})


def test_compile_rejette_outil_inconnu() -> None:
    with pytest.raises(ValueError, match="unknown_action_tool:bad_tool"):
        compile_strategy_call("bad_tool", {})
