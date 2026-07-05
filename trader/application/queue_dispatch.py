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

TOUR D'OUTILS (T4, issue #2) : quand agent_tools est actif ET que les services
boot sont câblés côté handler, celui-ci orchestre le tour d'outils grain-1
(get_indicator_context / get_active_plans / recall_learnings / get_freshness) —
fin du mode dégradé Lot A. Le dispatch enfile tous les symboles décidables ;
model_calls_used reste une métrique d'observabilité issue des enveloppes handler.
Source : decide_one.py (ToolRoundServices), decide_handler.make_decide_handler.
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
_POLL_SLEEP_MAX_S: float = 0.5


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
    now_fn: Callable[[], float],
    symbols_universe: list[str] | None = None,
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
    now_fn:
        ``Callable[[], float]`` retournant epoch secondes. Injectable pour les tests.

    Returns
    -------
    tuple[dict[str, Decision], int, set[str]]
        ``(decisions_by_symbol, model_calls_used, undecided_symbols)`` où :

        - ``decisions_by_symbol`` : décisions collectées ce cycle.
        - ``model_calls_used`` : appels LLM réellement consommés (somme des
          ``model_calls`` remontés par les handlers ; 1/décision sans round,
          2+/décision avec tour d'outils).
        - ``undecided_symbols`` : symboles non décidés ce cycle = skippés
          (dead / lease expiré / task introuvable). Ces symboles ne doivent
          PAS recevoir un HOLD synthétique — ils seront redécidés quand la file
          les rendra à nouveau admissibles.
    """
    now_ms = int(now_fn() * 1000)

    # -----------------------------------------------------------------------
    # 1. Purge des tâches decide périmées (cycles précédents)
    # -----------------------------------------------------------------------
    # Les pending d'anciens cycles sont supprimées ; un running vivant reste en
    # place et bloque sa partition, ce qui reporte simplement le symbole.
    stale_deleted = ledger.delete_stale_decide(current_cycle_id=cycle_id, now_ms=now_ms)
    if stale_deleted:
        log.debug(
            "[queue_dispatch] purged stale decide tasks cycle=%s deleted=%d",
            cycle_id,
            stale_deleted,
        )

    # -----------------------------------------------------------------------
    # 2. Enfilage — 1 tâche par symbole décidable
    # -----------------------------------------------------------------------
    task_ids: dict[str, int] = {}
    enqueue_skipped_syms: set[str] = set()
    for sym in decidable:
        payload = {
            "symbol": sym,
            "mandate": mandate,
            "memory": memory,
            "shared_context": shared_context,
            "per_symbol_facts": symbol_facts_by_sym.get(sym, {}),
            "decision_timeout_s": decision_timeout_s,
            "agent_tools_enabled": agent_tools_enabled,
            "symbols_universe": symbols_universe or [],
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
                    "[queue_dispatch] enqueue skipped sym=%s dedup=%s — active partition or missing dedup",
                    sym,
                    dedup_key,
                )
                enqueue_skipped_syms.add(sym)

    # -----------------------------------------------------------------------
    # 3. Collecte — polling jusqu'à état terminal
    # -----------------------------------------------------------------------
    decisions_by_symbol: dict[str, Decision] = {}
    model_calls_by_sym: dict[str, int] = {}
    pending_syms = set(task_ids)
    # Symboles non résolus (dead/lease expiré/introuvable) — seront dans undecided.
    skipped_syms: set[str] = set(enqueue_skipped_syms)
    poll_sleep_s = _POLL_SLEEP_S

    while pending_syms:
        resolved = set()
        poll_now_ms = int(now_fn() * 1000)
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
                        # Enveloppe T4 {"decision": ..., "model_calls": n} ;
                        # rétro-compat : ancien format plat = asdict(Decision) direct.
                        if isinstance(data, dict) and "decision" in data:
                            decision_data = data["decision"]
                            # Clamp >=1 (E4c) : la valeur vient de la DB, ne pas
                            # laisser un 0/négatif fausser le fusible.
                            calls = max(1, int(data.get("model_calls") or 1))
                        else:
                            decision_data = data
                            calls = 1
                        decisions_by_symbol[sym] = Decision(**decision_data)
                        model_calls_by_sym[sym] = calls
                        log.debug(
                            "[queue_dispatch] collected sym=%s action=%s calls=%d",
                            sym,
                            decisions_by_symbol[sym].action,
                            calls,
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
            elif status == "running":
                lease_expires_at = task.get("lease_expires_at")
                if lease_expires_at is not None and int(lease_expires_at) < poll_now_ms:
                    log.warning(
                        "[queue_dispatch] task lease expiré sym=%s task_id=%s — skip",
                        sym,
                        tid,
                    )
                    skipped_syms.add(sym)
                    resolved.add(sym)

        pending_syms -= resolved

        if resolved:
            poll_sleep_s = _POLL_SLEEP_S
        if pending_syms:
            _time.sleep(poll_sleep_s)
            if not resolved:
                poll_sleep_s = min(_POLL_SLEEP_MAX_S, poll_sleep_s * 2)

    # FIX 2 : undecided = skippés (dead/lease expiré/introuvable).
    # run_cycle EXCLUT ces symboles du fallback HOLD synthétique en mode queue.
    undecided_symbols = set(skipped_syms)

    # T4 : appels LLM réellement consommés (2 par décision avec tour d'outils),
    # remontés par le handler dans l'enveloppe — plus len(decisions).
    model_calls_used = sum(model_calls_by_sym.values())
    log.info(
        "[queue_dispatch] cycle=%s symbols=%d decided=%d calls=%d undecided=%d skipped=%d",
        cycle_id,
        len(decidable),
        len(decisions_by_symbol),
        model_calls_used,
        len(undecided_symbols),
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
