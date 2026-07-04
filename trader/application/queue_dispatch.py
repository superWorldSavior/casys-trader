"""Producteur + collecte de décisions grain-symbole via la file de tâches.

Ce module est le pendant de batch_decide pour le mode file (CASYS_QUEUE_DECIDE_ENABLED).
Il enfile une tâche ``decide`` par symbole et collecte les résultats par polling
jusqu'à épuisement du budget ou que toutes les tâches soient terminées.

Contrat du payload task (JSON) — aligné sur decide_handler.make_decide_handler :

    {
      "symbol":              str,
      "mandate":             str,
      "memory":              str,
      "shared_context":      dict,
      "per_symbol_facts":    dict,   # pré-construit par l'appelant (build_symbol_facts)
      "decision_timeout_s":  int,
      "agent_tools_enabled": bool
    }

AX §8 (Structured Outputs) : aucun spinner, retour machine-readable.
AX §5 (Fast Fail Early) : les tâches stale des cycles précédents sont purgées
avant l'enfilage pour libérer l'index uniq_active_kind_partition.
AX §6 (Deterministic Outputs) : now_fn injectable pour déterminisme des tests.

COMPROMIS MODE QUEUE — DÉCISION DÉGRADÉE (Lot A) :
  Le handler decide_handler appelle decide_batch avec allow_context_request=False
  et allow_tool_calls=False, ce qui désactive : REQUEST_CONTEXT, tool round,
  recall_learnings.  La parité complète avec le mode batch (context_request,
  tools, recall) est une itération future hors périmètre Lot A.
  Source : decide_one.py, decide_handler.make_decide_handler.
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import Callable

from trader.agent.protocol.types import Decision

log = logging.getLogger(__name__)

# Intervalle de pause entre deux polls de collecte (secondes).
# Petit devant la latence LLM (>10s) ; évite le spin sans retarder la réponse.
_POLL_SLEEP_S: float = 0.1


def dispatch_decide_via_queue(
    *,
    ledger,
    decidable: list[str],
    mandate: str,
    memory: str,
    shared_context: dict,
    symbol_facts_by_sym: dict[str, dict],
    decision_timeout_s: int,
    agent_tools_enabled: bool,
    cycle_id: str,
    budget_s: float,
    now_fn: Callable[[], float],
    max_model_calls: int = 999,
) -> tuple[dict[str, Decision], int, set[str]]:
    """Enfile les décisions grain-symbole et collecte les résultats via polling.

    Parameters
    ----------
    ledger:
        ``TaskLedger`` dédié à la file decide (``task_ledger.db``).
    decidable:
        Liste des symboles à décider ce cycle (déjà priorisés en amont).
    mandate, memory, shared_context:
        Contexte partagé transmis tel quel au handler decide.
    symbol_facts_by_sym:
        Faits pré-construits par ``build_symbol_facts`` (+ indicator_triggers),
        un dict par symbole. Clé = symbole.
    decision_timeout_s:
        Plafond de l'appel LLM interne (passé dans le payload).
    agent_tools_enabled:
        Transmis au handler (use_symbol_calls_contract).
    cycle_id:
        Identifiant unique du cycle courant (ex. ``now.isoformat()``).
        Préfixe les dedup_keys : ``{cycle_id}:{sym}``.
    budget_s:
        Budget de collecte total en secondes (= CASYS_DECISION_TIMEOUT_S).
        Les symboles non résolus dans ce délai sont skippés.
    now_fn:
        ``Callable[[], float]`` retournant epoch secondes. Injectable pour les tests.
    max_model_calls:
        Nombre maximum de tâches enfilées ce cycle (fusible coût acpx).
        Les symboles au-delà de la limite sont REPORTÉS (non enfilés) — ils
        reviendront décidables au tick suivant.  Ordre = ordre de ``decidable``
        (déjà priorisé en amont, AX §6 déterminisme).  Défaut 999 = pas de
        limite effective (rétrocompatibilité si non passé).

    Returns
    -------
    tuple[dict[str, Decision], int, set[str]]
        ``(decisions_by_symbol, model_calls_used, undecided_symbols)`` où :

        - ``decisions_by_symbol`` : décisions collectées ce cycle.
        - ``model_calls_used`` : nombre de tâches ``done`` (décisions réussies).
        - ``undecided_symbols`` : symboles non décidés ce cycle = reportés par
          le fusible + skippés (dead / budget épuisé / task introuvable).
          Ces symboles ne doivent PAS recevoir un HOLD synthétique — ils seront
          redécidés au prochain cycle.
    """
    now_ms = int(now_fn() * 1000)

    # -----------------------------------------------------------------------
    # 1. Purge des tâches decide périmées (cycles précédents)
    # -----------------------------------------------------------------------
    # L'index uniq_active_kind_partition (kind, partition_key) WHERE status IN
    # ('pending','running') bloquerait l'enfilage si une tâche de même partition
    # du cycle précédent est encore active. On la supprime d'abord.
    stale_deleted = ledger.delete_stale_decide(current_cycle_id=cycle_id)
    if stale_deleted:
        log.debug(
            "[queue_dispatch] purged stale decide tasks cycle=%s deleted=%d",
            cycle_id,
            stale_deleted,
        )

    # -----------------------------------------------------------------------
    # 2. Enfilage — 1 tâche par symbole, dans la limite du fusible
    # -----------------------------------------------------------------------
    # FIX 1 : on n'enfile qu'au plus `max_model_calls` symboles ce cycle.
    # Les symboles au-delà sont reportés (undecided) sans enfilage — ils
    # reviendront décidables au tick suivant (pas de HOLD synthétique).
    admitted = decidable[:max_model_calls]
    deferred_by_cap = set(decidable[max_model_calls:])
    if deferred_by_cap:
        log.info(
            "[queue_dispatch] fusible max_model_calls=%d : %d symboles reportés au cycle suivant",
            max_model_calls,
            len(deferred_by_cap),
        )

    task_ids: dict[str, int] = {}
    for sym in admitted:
        payload = {
            "symbol": sym,
            "mandate": mandate,
            "memory": memory,
            "shared_context": shared_context,
            "per_symbol_facts": symbol_facts_by_sym.get(sym, {}),
            "decision_timeout_s": decision_timeout_s,
            "agent_tools_enabled": agent_tools_enabled,
        }
        dedup_key = f"{cycle_id}:{sym}"
        tid = ledger.enqueue(
            kind="decide",
            priority=0,
            scheduled_at_ms=now_ms,
            now_ms=now_ms,
            partition_key=sym,
            resource="acpx",
            dedup_key=dedup_key,
            payload=json.dumps(payload),
        )
        if tid is not None:
            task_ids[sym] = tid
            log.debug(
                "[queue_dispatch] enqueued sym=%s task_id=%s dedup=%s",
                sym,
                tid,
                dedup_key,
            )
        else:
            # Conflit dedup avec même cycle_id (appel double) : chercher via dedup_key.
            existing = _get_by_dedup_key(ledger, dedup_key)
            if existing is not None:
                task_ids[sym] = existing["id"]
                log.debug(
                    "[queue_dispatch] dedup conflict sym=%s existing_id=%s",
                    sym,
                    existing["id"],
                )
            else:
                log.warning(
                    "[queue_dispatch] enqueue returned None mais task introuvable sym=%s dedup=%s",
                    sym,
                    dedup_key,
                )

    # -----------------------------------------------------------------------
    # 3. Collecte — polling jusqu'à done/dead ou budget épuisé
    # -----------------------------------------------------------------------
    deadline = now_fn() + budget_s
    decisions_by_symbol: dict[str, Decision] = {}
    pending_syms = set(task_ids)
    # Symboles enfilés mais non résolus (dead/budget) — seront dans undecided.
    skipped_syms: set[str] = set()

    while pending_syms and now_fn() < deadline:
        resolved = set()
        for sym in pending_syms:
            tid = task_ids[sym]
            task = ledger.get(tid)
            if task is None:
                log.warning(
                    "[queue_dispatch] task introuvable sym=%s task_id=%s — skip",
                    sym,
                    tid,
                )
                skipped_syms.add(sym)
                resolved.add(sym)
                continue

            status = task.get("status")
            if status == "done":
                result_json = task.get("result")
                if result_json:
                    try:
                        data = json.loads(result_json)
                        decisions_by_symbol[sym] = Decision(**data)
                        log.debug(
                            "[queue_dispatch] collected sym=%s action=%s",
                            sym,
                            decisions_by_symbol[sym].action,
                        )
                    except Exception as exc:  # noqa: BLE001 — skip au lieu de bloquer
                        log.warning(
                            "[queue_dispatch] désérialisation Decision échouée sym=%s: %s",
                            sym,
                            exc,
                        )
                        skipped_syms.add(sym)
                resolved.add(sym)
            elif status == "dead":
                log.warning(
                    "[queue_dispatch] task dead sym=%s task_id=%s — skip",
                    sym,
                    tid,
                )
                skipped_syms.add(sym)
                resolved.add(sym)

        pending_syms -= resolved

        if pending_syms:
            remaining = deadline - now_fn()
            if remaining > 0:
                _time.sleep(min(_POLL_SLEEP_S, remaining))

    # Symboles encore en attente au budget épuisé → skip avec warning
    for sym in pending_syms:
        log.warning(
            "[queue_dispatch] budget épuisé sym=%s budget_s=%.1f — skip",
            sym,
            budget_s,
        )
        skipped_syms.add(sym)

    # FIX 2 : undecided = reportés par le fusible + skippés (dead/budget/introuvable).
    # run_cycle EXCLUT ces symboles du fallback HOLD synthétique en mode queue.
    undecided_symbols = deferred_by_cap | skipped_syms

    model_calls_used = len(decisions_by_symbol)
    log.info(
        "[queue_dispatch] cycle=%s symbols=%d decided=%d undecided=%d (deferred=%d skipped=%d)",
        cycle_id,
        len(decidable),
        model_calls_used,
        len(undecided_symbols),
        len(deferred_by_cap),
        len(skipped_syms),
    )
    return decisions_by_symbol, model_calls_used, undecided_symbols


def _get_by_dedup_key(ledger, dedup_key: str) -> "dict | None":
    """Lookup par dedup_key via le _db sous-jacent du ledger (fallback dedup)."""
    try:
        row = ledger._db.query_one("SELECT * FROM tasks WHERE dedup_key=?", (dedup_key,))
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001 — fallback best-effort
        log.warning("[queue_dispatch] get_by_dedup_key failed: %s", exc)
        return None
