"""shadow — sonde d'orchestration shadow-queue (Phase 2).

Vérifie, en prod et sans risque, que la file de tâches achemine le même
ensemble de symboles que le chemin synchrone, une fois chacun, sans perte
ni doublon.

Le shadow ne re-appelle JAMAIS le LLM. Il vérifie uniquement que
l'ORCHESTRATION est correcte (tous les symboles decidables sont enfilés,
claimés et drainés exactement une fois).

Isolation garantie : DB dédiée (shadow_queue.db), jamais la prod.
Toute exception interne est capturée → rapport {"error": ..., "identical": False}.

Purge par cycle : DELETE FROM tasks en début de run() — la DB shadow ne
contient jamais que les jobs du cycle courant (pas d'historique).
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from trader.infrastructure.queue.ledger import TaskLedger
from trader.infrastructure.queue.pools import ResourcePools
from trader.infrastructure.queue.worker import Worker

log = logging.getLogger(__name__)

_SHADOW_KIND = "shadow_decide"
_LEASE_MS = 60_000          # 60 s : plus que suffisant pour un no-op
_SAFETY_MULTIPLIER = 2      # borne = 2×N+10 itérations de drain
_SAFETY_EXTRA = 10


class ShadowQueueProbe:
    """Sonde d'orchestration shadow-queue.

    Parameters
    ----------
    db_path:
        Chemin vers la DB SQLite DÉDIÉE (ex. ``state/shadow_queue.db``).
        La DB est créée si elle n'existe pas. Ne jamais pointer vers la
        DB de production.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._ledger = TaskLedger(self._db_path)
        # Pools sans ressource nommée : le shadow utilise resource=None
        self._pools = ResourcePools({})
        self._worker = Worker(
            ledger=self._ledger,
            pools=self._pools,
            handlers={_SHADOW_KIND: self._noop_handler},
            worker_id="shadow-probe",
            lease_ms=_LEASE_MS,
            backoff_base_ms=1000,
        )
        self._drained_symbols: list[str] = []

    # ------------------------------------------------------------------
    # Handler no-op
    # ------------------------------------------------------------------

    def _noop_handler(self, task: dict, *, heartbeat=None) -> None:
        """Collecte le symbole du payload sans rien faire d'autre."""
        try:
            payload = json.loads(task.get("payload") or "{}")
            sym = payload.get("symbol")
            if sym:
                self._drained_symbols.append(sym)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        cycle_ts: str,
        decidable_symbols: list[str],
        decided_symbols: list[str],
        now_ms: int,
    ) -> dict:
        """Enfile, draine et compare.

        Parameters
        ----------
        cycle_ts:
            Horodatage ISO du cycle (ex. ``now.isoformat()``). Utilisé dans
            le ``dedup_key`` pour garantir l'idempotence intra-run.
        decidable_symbols:
            Symboles que la file DEVRAIT acheminer (source indépendante —
            ex. ``decidable`` du daemon, avant le LLM). La sonde enfile
            ceux-ci, pas ``decided_symbols``.
        decided_symbols:
            Symboles réellement décidés par le LLM (clés de
            ``decisions_by_symbol``). Utilisés UNIQUEMENT pour le champ
            ``decided_vs_decidable`` (comparaison ensembliste, non pour
            l'acheminement).
        now_ms:
            Epoch en millisecondes (int). Injecté pour déterminisme.

        Returns
        -------
        dict
            Rapport machine-readable. ``identical=True`` ssi
            ``drained_symbols == sorted(decidable_symbols)``, sans missing
            ni duplicated ni dead.
        """
        try:
            return self._run_internal(
                cycle_ts=cycle_ts,
                decidable_symbols=decidable_symbols,
                decided_symbols=decided_symbols,
                now_ms=now_ms,
            )
        except Exception as exc:  # noqa: BLE001 — frontière chemin chaud
            log.warning("[shadow-queue] exception interne: %s", exc)
            return {"error": str(exc), "identical": False}

    # ------------------------------------------------------------------
    # Implémentation
    # ------------------------------------------------------------------

    def _run_internal(
        self,
        *,
        cycle_ts: str,
        decidable_symbols: list[str],
        decided_symbols: list[str],
        now_ms: int,
    ) -> dict:
        self._drained_symbols = []

        # ── 0. Purge : repart propre à chaque cycle ──────────────────────
        # Aucun historique gardé → dead_count/drained ne concernent QUE ce cycle.
        self._ledger.purge_all()

        # ── 1. Enqueue decidable_symbols (source indépendante) ────────────
        enqueued = 0
        deduped = 0
        for sym in decidable_symbols:
            dedup_key = f"shadow:{cycle_ts}:{sym}"
            task_id = self._ledger.enqueue(
                kind=_SHADOW_KIND,
                priority=0,
                scheduled_at_ms=now_ms,
                now_ms=now_ms,
                partition_key=sym,
                dedup_key=dedup_key,
                payload=json.dumps({"symbol": sym, "cycle_ts": cycle_ts}),
            )
            if task_id is None:
                deduped += 1
            else:
                enqueued += 1

        # ── 2. Drain ─────────────────────────────────────────────────────
        safety = _SAFETY_MULTIPLIER * len(decidable_symbols) + _SAFETY_EXTRA
        drained = 0
        token = uuid.uuid4().hex
        for _ in range(safety):
            claimed = self._worker.run_once(now_ms=now_ms, token=token)
            if not claimed:
                break
            drained += 1
            # Renouvelle le token à chaque tâche (fencing par tâche)
            token = uuid.uuid4().hex

        # ── 3. Compter les tâches mortes (dead) ──────────────────────────
        dead_count = self._ledger.count_by_status("dead", kind=_SHADOW_KIND)

        # ── 4. Rapport acheminement (decidable vs drained) ───────────────
        expected = sorted(decidable_symbols)
        drained_sorted = sorted(self._drained_symbols)

        expected_set = set(expected)
        drained_set = set(drained_sorted)
        missing = sorted(expected_set - drained_set)
        # Doublons = symboles drainés plus d'une fois
        seen: dict[str, int] = {}
        for s in self._drained_symbols:
            seen[s] = seen.get(s, 0) + 1
        duplicated = sorted(s for s, n in seen.items() if n > 1)

        identical = (
            drained_sorted == expected
            and not missing
            and not duplicated
            and dead_count == 0
        )

        # ── 5. decided_vs_decidable : comparaison ensembliste ────────────
        decided_set = set(decided_symbols)
        decidable_set = set(decidable_symbols)
        decided_vs_decidable = {
            "decided": sorted(decided_symbols),
            "decidable": sorted(decidable_symbols),
            "only_decided": sorted(decided_set - decidable_set),
            "only_decidable": sorted(decidable_set - decided_set),
        }

        report = {
            "cycle_ts": cycle_ts,
            "enqueued": enqueued,
            "deduped": deduped,
            "drained": drained,
            "expected": expected,
            "drained_symbols": drained_sorted,
            "missing": missing,
            "duplicated": duplicated,
            "dead": dead_count,
            "identical": identical,
            "decided_vs_decidable": decided_vs_decidable,
        }
        log.info(
            "[shadow-queue] cycle=%s enq=%d drain=%d identical=%s missing=%s decided_vs_decidable=%s",
            cycle_ts,
            enqueued,
            drained,
            identical,
            missing or "[]",
            decided_vs_decidable,
        )
        return report
