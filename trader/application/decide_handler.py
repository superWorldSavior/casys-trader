"""Handler decide grain-symbole pour le Worker de la file de tâches.

Format exact du payload (JSON encodé dans task["payload"]) :

    {
      "symbol":              str,          # ex. "AAPL"
      "mandate":             str,          # texte brut du mandat
      "memory":              str,          # texte brut de la mémoire
      "shared_context":      dict,         # contexte partagé du cycle (JSON-sérialisable)
      "per_symbol_facts":    dict,         # faits calculés pour ce symbole uniquement
      "decision_timeout_s":  int,          # plafond d'appel LLM (secondes)
      "agent_tools_enabled": bool,         # True → use_symbol_calls_contract=True (+ tools si services)
      "symbols_universe":    list[str]?    # univers du cycle (resolver d'indicateurs, optionnel)
    }

La brique productrice (queue_dispatch) DOIT produire exactement ce format.

Format du résultat (task.result) — ENVELOPPE depuis T4 :

    {"decision": {...asdict(Decision)...}, "model_calls": int}

``model_calls`` = appels LLM réellement consommés (1 sans round, 2+ avec tour
d'outils) — consommé par queue_dispatch pour l'observabilité. Le lecteur
(queue_dispatch) accepte aussi l'ancien format plat (rétro-compat transitoire).

RetryableError levée par decide_one remonte telle quelle vers le Worker, qui
appelle fail(retryable=True) + on_overload(resource) si is_overload=True.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable

from trader.application.decide_one import ToolRoundServices, decide_one

log = logging.getLogger(__name__)


def make_decide_handler(
    *,
    codex_client,
    tool_services: "ToolRoundServices | None" = None,
    session_backends: list | None = None,
) -> Callable[[dict], "str | None"]:
    """Fabrique le handler 'decide' compatible Worker.

    Parameters
    ----------
    codex_client:
        Module ou objet exposant decide_batch — même interface qu'attendu par
        decide_one. Injectable pour les tests.
    tool_services:
        Services au boot pour le tour d'outils (spec queue tool-round §4) :
        get_bars (indirection thread-safe), recall provider, bornes resolver.
        ``None`` = mode dégradé historique (aucun outil).

    Returns
    -------
    Callable[[dict], str | None]
        Handler : reçoit le dict task, retourne l'enveloppe résultat JSON
        (str) → ira dans task.result via ledger.complete(result=...).
        Laisse remonter RetryableError sans l'attraper (le Worker gère le retry).
    """

    def handler(task: dict) -> str:
        payload = json.loads(task.get("payload") or "{}")
        symbol: str = payload["symbol"]
        log.debug("[decide_handler] start symbol=%s task_id=%s", symbol, task.get("id"))
        task_id = str(task.get("id"))

        decision, model_calls = decide_one(
            symbol=symbol,
            mandate=payload["mandate"],
            memory=payload["memory"],
            shared_context=payload["shared_context"],
            per_symbol_facts=payload["per_symbol_facts"],
            decision_timeout_s=int(payload["decision_timeout_s"]),
            agent_tools_enabled=bool(payload["agent_tools_enabled"]),
            codex_client=codex_client,
            tool_services=tool_services,
            symbols_universe=payload.get("symbols_universe"),
            session_backends=session_backends,
            task_id=task_id,
        )

        result_json = json.dumps({"decision": asdict(decision), "model_calls": model_calls})
        log.debug(
            "[decide_handler] done symbol=%s action=%s calls=%d task_id=%s",
            symbol, decision.action, model_calls, task.get("id"),
        )
        return result_json

    return handler
