"""Garde-fou d'invariant : tout action tool final du contrat symbol_calls doit
être couvert par la finalisation d'outcome (tool_trace._ACTION_TOOLS), sinon son
outcome dans runtime.tool_calls reste brut "ok" — trou d'audit SILENCIEUX.

Cette dette s'est déjà re-creusée une fois (amend_exit ajouté par L3 sans être
finalisé). Ce test la verrouille : quand tu ajoutes un action tool à
`_decision_from_symbol_calls`, ajoute-le ICI, dans `tool_trace._ACTION_TOOLS`,
et pense à `build_decision_row` pour l'audit durable.
"""
from trader.agent import client as codex_client
from trader.reporting import tool_trace

# Les action tools FINAUX (par symbole) reconnus par le contrat symbol_calls.
_CONTRACT_ACTION_TOOLS = {
    "propose_order",
    "set_next_wake",
    "propose_indicator_watch",
    "cancel_watch",
    "record_learning",
    "amend_exit",
}


def _rationale_for_tool(tool: str) -> str:
    raw = (
        '{"decisions":[{"symbol":"SPY","confidence":0.5,"rationale":"x",'
        '"decision_reason_code":"NO_EDGE","calls":[{"tool":"' + tool + '","args":{}}]}]}'
    )
    return codex_client.parse_batch(raw, ["SPY"], allow_context_request=False)["SPY"].rationale


def test_action_tools_du_contrat_sont_reconnus_par_le_parsing() -> None:
    for tool in _CONTRACT_ACTION_TOOLS:
        assert "unknown_action_tool" not in _rationale_for_tool(tool), (
            f"{tool} n'est plus reconnu par _decision_from_symbol_calls"
        )
    # Sanity : un tool inconnu EST bien rejeté (sinon le test ci-dessus ne prouve rien).
    assert "unknown_action_tool" in _rationale_for_tool("definitely_not_a_tool")


def test_action_tools_du_contrat_sont_finalises_pour_l_audit() -> None:
    missing = _CONTRACT_ACTION_TOOLS - tool_trace._ACTION_TOOLS
    assert not missing, (
        f"action tools sans finalisation d'outcome (trou d'audit silencieux) : {missing}"
    )
