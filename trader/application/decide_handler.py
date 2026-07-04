"""Handler decide grain-symbole pour le Worker de la file de tâches.

Format exact du payload (JSON encodé dans task["payload"]) :

    {
      "symbol":              str,          # ex. "AAPL"
      "mandate":             str,          # texte brut du mandat
      "memory":              str,          # texte brut de la mémoire
      "shared_context":      dict,         # contexte partagé du cycle (JSON-sérialisable)
      "per_symbol_facts":    dict,         # faits calculés pour ce symbole uniquement
      "decision_timeout_s":  int,          # plafond d'appel LLM (secondes)
      "agent_tools_enabled": bool          # True → use_symbol_calls_contract=True
    }

La brique productrice (queue_dispatch, brique suivante) DOIT produire exactement
ce format — pas de champ supplémentaire requis par ce handler.

RetryableError levée par decide_one remonte telle quelle vers le Worker, qui
appelle fail(retryable=True) + on_overload(resource) si is_overload=True.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable

from trader.application.decide_one import decide_one

log = logging.getLogger(__name__)


def make_decide_handler(
    *,
    codex_client,
) -> Callable[[dict], "str | None"]:
    """Fabrique le handler 'decide' compatible Worker.

    Parameters
    ----------
    codex_client:
        Module ou objet exposant decide_batch — même interface qu'attendu par
        decide_one. Injectable pour les tests.

    Returns
    -------
    Callable[[dict], str | None]
        Handler : reçoit le dict task, retourne la Decision sérialisée en JSON
        (str) → ira dans task.result via ledger.complete(result=...).
        Laisse remonter RetryableError sans l'attraper (le Worker gère le retry).
    """

    def handler(task: dict) -> str:
        payload = json.loads(task.get("payload") or "{}")
        symbol: str = payload["symbol"]
        log.debug("[decide_handler] start symbol=%s task_id=%s", symbol, task.get("id"))

        decision = decide_one(
            symbol=symbol,
            mandate=payload["mandate"],
            memory=payload["memory"],
            shared_context=payload["shared_context"],
            per_symbol_facts=payload["per_symbol_facts"],
            decision_timeout_s=int(payload["decision_timeout_s"]),
            agent_tools_enabled=bool(payload["agent_tools_enabled"]),
            codex_client=codex_client,
        )

        result_json = json.dumps(asdict(decision))
        log.debug(
            "[decide_handler] done symbol=%s action=%s task_id=%s",
            symbol, decision.action, task.get("id"),
        )
        return result_json

    return handler
