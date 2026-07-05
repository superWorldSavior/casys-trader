"""Worker : claim → acquire ressource → handler → complete/fail → release.

Cas auto-complete (execute_order_handler)
-----------------------------------------
Certains handlers (ex. ``execute_order_handler``) appellent
``execute_order_unit`` qui exécute ``ledger.complete_in_tx`` DANS sa
transaction SQLite — en écrivant aussi le Fill JSON sérialisé dans ``result``
(FIX 1 : fill atomique).  Quand le Worker appelle ensuite ``complete(result=None)``,
le fencing token ne matche plus (``claim_token=NULL``, ``status='done'``) →
``complete`` retourne ``False`` (no-op).

Le handler retourne ``None`` : le fill est déjà lisible via
``ledger.get(task_id)["result"]``.  Aucun patch post-complétion n'est requis.

Rétro-compat decide/shadow : pour ces handlers, ``complete`` retourne ``True``
(ils ne font pas de complete interne) → pas d'impact.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)



class RetryableError(Exception):
    """Le handler signale un échec transitoire → requeue avec backoff.

    Paramètre ``is_overload`` : True seulement si l'erreur est due à une
    saturation de la ressource externe (ex. rate-limit, queue pleine).
    Laisser False pour les erreurs transitoires sans rapport avec la charge
    (ex. timeout réseau, erreur HTTP 503 ponctuelle) — cela évite de baisser
    la limite AIMD pour des raisons non-liées à la capacité.
    """

    def __init__(self, *args, is_overload: bool = False):
        super().__init__(*args)
        self.is_overload = is_overload


class Worker:
    def __init__(self, ledger, pools, handlers, worker_id,
                 lease_ms=1_800_000, backoff_base_ms=1000,
                 now_fn: Callable[[], float] = time.time):
        self._ledger = ledger
        self._pools = pools
        self._handlers = handlers
        self._id = worker_id
        self._lease_ms = lease_ms
        self._backoff_base_ms = backoff_base_ms
        self._now_fn = now_fn

    def run_once(self, *, now_ms, token) -> bool:
        """Tente de claimer et d'exécuter une tâche.

        Séquence : free_resources → claim → try_acquire → handler →
        complete/fail → release. Sur resource miss (course entre
        ``free_resources`` et ``claim``) : unclaim neutre via ``release_claim``
        sans brûler de tentative.
        Retourne ``True`` si une tâche a été tentée (même en resource miss),
        ``False`` si rien n'était à claimer.
        """
        free = self._pools.free_resources()
        task = self._ledger.claim(worker_id=self._id, token=token, now_ms=now_ms,
                                  lease_ms=self._lease_ms, free_resources=free)
        if task is None:
            return False
        log.debug("[queue.worker] claim id=%s kind=%s", task["id"], task["kind"])
        resource = task["resource"]
        acquired = self._pools.try_acquire(resource) if resource else True
        if resource and not acquired:
            # course rare : la ressource a été prise entre free_resources et claim.
            # On rend le claim sans brûler de tentative ni appliquer de backoff.
            self._ledger.release_claim(task_id=task["id"], token=token, now_ms=now_ms)
            log.debug("[queue.worker] resource miss id=%s resource=%s → unclaim",
                      task["id"], resource)
            return True
        def heartbeat():
            return self._ledger.heartbeat(
                task_id=task["id"],
                token=token,
                now_ms=int(self._now_fn() * 1000),
                lease_ms=self._lease_ms,
            )

        try:
            result = self._handlers[task["kind"]](task, heartbeat=heartbeat)
            finish_now_ms = int(self._now_fn() * 1000)
            _completed = self._ledger.complete(
                task_id=task["id"], token=token, now_ms=finish_now_ms, result=result
            )
            if _completed:
                log.debug("[queue.worker] complete id=%s kind=%s", task["id"], task["kind"])
            else:
                # Handler auto-completed (ex. execute_order_unit inside the handler).
                # complete() a retourné False (fencing : task déjà done, token invalide).
                # FIX 1 : le Fill est déjà dans task.result (écrit atomiquement par UoW).
                # Aucun patch supplémentaire nécessaire.
                log.debug("[queue.worker] auto-complete id=%s kind=%s (fencing ok)", task["id"], task["kind"])
            if resource:
                self._pools.on_success(resource)
        except RetryableError as exc:
            finish_now_ms = int(self._now_fn() * 1000)
            log.warning("[queue.worker] retryable fail id=%s: %s", task["id"], exc)
            if resource and exc.is_overload:
                self._pools.on_overload(resource)
            self._ledger.fail(task_id=task["id"], token=token, now_ms=finish_now_ms,
                              error=str(exc), retryable=True,
                              backoff_base_ms=self._backoff_base_ms)
        except Exception as exc:  # noqa: BLE001 — frontière handler
            finish_now_ms = int(self._now_fn() * 1000)
            log.warning("[queue.worker] fatal fail id=%s: %s", task["id"],
                        f"{type(exc).__name__}: {exc}")
            self._ledger.fail(task_id=task["id"], token=token, now_ms=finish_now_ms,
                              error=f"{type(exc).__name__}: {exc}",
                              retryable=False, backoff_base_ms=self._backoff_base_ms)
        finally:
            if resource:
                self._pools.release(resource)
        return True
