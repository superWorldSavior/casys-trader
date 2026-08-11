"""Pool de workers-threads pour traiter la file de tâches (Lot A phase 3).

Chaque worker tourne dans son propre thread daemon (Python) ; ils partagent le
même TaskLedger et le même ResourcePools (tous deux thread-safe).

Heartbeat / bail
----------------
Le ``Worker`` transmet un callback ``heartbeat`` aux handlers. Les décisions en
session le déclenchent après l'ouverture acpx puis après chaque appel modèle pour
renouveler un bail court ; les handlers rapides peuvent l'ignorer.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Callable

from trader.infrastructure.queue.worker import Worker

log = logging.getLogger(__name__)

# Durée de pause entre deux tentatives infructueuses (rien à claimer).
# Evite le spin. Doit être petit devant la latence LLM (900s) ; 50ms est
# un bon compromis : réactivité + overhead CPU négligeable.
_IDLE_SLEEP_S: float = 0.05


class DecidePool:
    """Pool de N workers-threads drainant la file de tâches.

    Parameters
    ----------
    ledger:
        ``TaskLedger`` partagé (thread-safe via StateDb).
    pools:
        ``ResourcePools`` partagé (thread-safe via Condition).
    handlers:
        ``dict[str, Callable[..., str | None]]`` — handlers par kind.
        Un handler retournant ``None`` écrit ``result=None`` dans la task
        (rétro-compat shadow / no-op).
    num_workers:
        Nombre de threads à démarrer.
    now_fn:
        ``Callable[[], float]`` — epoch en secondes (ex. ``time.time``).
        Injecté pour déterminisme dans les tests.
    lease_ms:
        Durée du bail en ms (défaut 30 min).
    backoff_base_ms:
        Base du backoff exponentiel sur retry (ms, défaut 1000).
    backoff_max_ms:
        Plafond optionnel du backoff exponentiel (ms). ``None`` conserve le
        comportement historique non borné.
    """

    def __init__(
        self,
        *,
        ledger,
        pools,
        handlers: dict[str, Callable[[dict], "str | None"]],
        num_workers: int,
        now_fn: Callable[[], float],
        lease_ms: int = 1_800_000,
        backoff_base_ms: int = 1000,
        backoff_max_ms: int | None = None,
    ):
        self._ledger = ledger
        self._pools = pools
        self._handlers = handlers
        self._num_workers = num_workers
        self._now_fn = now_fn
        self._lease_ms = lease_ms
        self._backoff_base_ms = backoff_base_ms
        self._backoff_max_ms = backoff_max_ms
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Démarre les N threads workers.

        Lève ``RuntimeError`` si des threads issus d'un start précédent sont
        encore vivants (stop incomplet). Un start propre (aucun thread vivant)
        purge les références mortes avant de créer les nouveaux threads.
        """
        alive = [t for t in self._threads if t.is_alive()]
        if alive:
            raise RuntimeError(
                f"[decide_pool] start() refusé : {len(alive)} threads encore actifs — "
                "appeler stop(timeout_s=...) et attendre leur fin avant de redémarrer"
            )
        self._threads = []  # purge les références mortes résiduelles
        self._stop_event.clear()
        for i in range(self._num_workers):
            t = threading.Thread(
                target=self._run_worker,
                args=(f"pool-worker-{i}",),
                daemon=True,
                name=f"decide-pool-{i}",
            )
            t.start()
            self._threads.append(t)
        log.info("[decide_pool] started num_workers=%d", self._num_workers)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Signale l'arrêt et attend la fin des threads.

        Chaque thread vérifie ``_stop_event`` en tête de boucle, après au plus
        ``_IDLE_SLEEP_S`` de pause. Arrêt effectif en ≤ ``_IDLE_SLEEP_S`` +
        durée du ``run_once`` en cours (≤ lease_ms dans le pire cas, mais le
        test utilise des handlers mockés qui retournent immédiatement).

        Après les join, seuls les threads RÉELLEMENT terminés sont retirés de
        ``_threads``. Les threads encore vivants (handler LLM long) y restent
        référencés — leurs permits acpx restent détenus, et ``start()`` les
        détectera et refusera de démarrer.

        Parameters
        ----------
        timeout_s:
            Délai max d'attente par thread (secondes). Passé ce délai, les threads
            encore actifs sont conservés dans ``_threads`` (daemon=True : ils
            mourront avec le processus principal si non rejoints).
        """
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=timeout_s)
        still_alive = [t for t in self._threads if t.is_alive()]
        finished_count = len(self._threads) - len(still_alive)
        self._threads = still_alive
        if still_alive:
            log.warning(
                "[decide_pool] stop: %d threads encore actifs après timeout (%.1fs) — "
                "permits acpx potentiellement détenus",
                len(still_alive), timeout_s,
            )
        log.info("[decide_pool] stopped finished=%d alive=%d", finished_count, len(still_alive))

    # ------------------------------------------------------------------
    # Boucle worker
    # ------------------------------------------------------------------

    def _run_worker(self, worker_id: str) -> None:
        """Boucle principale d'un worker-thread.

        Séquence : claim → handler → complete/fail, répété jusqu'à stop_event.
        Quand rien à claimer (run_once → False) : pause courte (_IDLE_SLEEP_S)
        pour éviter le spin sans bloquer sur une Condition externe.
        """
        worker = Worker(
            ledger=self._ledger,
            pools=self._pools,
            handlers=self._handlers,
            worker_id=worker_id,
            lease_ms=self._lease_ms,
            backoff_base_ms=self._backoff_base_ms,
            backoff_max_ms=self._backoff_max_ms,
            now_fn=self._now_fn,
        )
        log.debug("[decide_pool] worker started id=%s", worker_id)

        while not self._stop_event.is_set():
            token = uuid.uuid4().hex
            now_ms = int(self._now_fn() * 1000)
            try:
                did_work = worker.run_once(now_ms=now_ms, token=token)
            except Exception:  # noqa: BLE001 — frontière thread, ne pas tuer le thread
                log.exception("[decide_pool] worker %s exception non gérée", worker_id)
                did_work = False

            if not did_work:
                # Rien à claimer — pause courte pour éviter le spin.
                # Event.wait garde le worker découplé du time.sleep global,
                # souvent patché par les tests de boucle daemon.
                self._stop_event.wait(_IDLE_SLEEP_S)

        log.debug("[decide_pool] worker stopped id=%s", worker_id)
