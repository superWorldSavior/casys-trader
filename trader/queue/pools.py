"""Pools de ressources : bornage de concurrence par ressource externe + AIMD."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class ResourcePools:
    # Nombre de succès consécutifs requis après un overload avant de recommencer
    # à incrémenter _eff (fenêtre de cooldown AIMD).  Constante nommée pour
    # respecter AX-7 (Explicit Over Implicit) : pas de nombre magique.
    COOLDOWN_SUCCESSES = 3

    def __init__(self, limits: dict[str, int]):
        self._max = dict(limits)                       # plafond initial
        self._eff = dict(limits)                       # limite effective (AIMD)
        self._used = {r: 0 for r in limits}
        # Streak initialisé au seuil : à l'état initial (sans overload passé),
        # chaque succès incrémente _eff directement.
        self._success_streak: dict[str, int] = {r: self.COOLDOWN_SUCCESSES for r in limits}

        # On utilise une Condition (plutôt qu'un Lock nu) pour permettre le
        # réveil sur libération de permit via wait_for_free().
        # SÉCURITÉ réentrance : la Condition wrap un threading.Lock (non réentrant
        # par défaut).  Les méthodes internes n'appellent PAS d'autres méthodes
        # qui re-prendraient self._cond : les prédicats wait_for() accèdent
        # directement aux attributs _used/_eff/_max pour éviter tout deadlock.
        self._cond = threading.Condition()

    # ------------------------------------------------------------------
    # Lecture d'état
    # ------------------------------------------------------------------

    def effective_limit(self, resource: str) -> int:
        with self._cond:
            return self._eff[resource]

    def free_resources(self) -> list[str]:
        with self._cond:
            return [r for r in self._max if self._used[r] < self._eff[r]]

    # ------------------------------------------------------------------
    # Gestion des permits
    # ------------------------------------------------------------------

    def try_acquire(self, resource: str) -> bool:
        with self._cond:
            if self._used[resource] < self._eff[resource]:
                self._used[resource] += 1
                return True
            return False

    def release(self, resource: str) -> None:
        with self._cond:
            if self._used[resource] > 0:
                self._used[resource] -= 1
            else:
                log.warning("[queue.pools] over-release ignoré resource=%s", resource)
            # Un slot libéré peut débloquer un waiter.
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Signaux AIMD
    # ------------------------------------------------------------------

    def on_overload(self, resource: str) -> None:
        with self._cond:
            self._eff[resource] = max(1, self._eff[resource] // 2)
            # Réinitialise le streak : il faut COOLDOWN_SUCCESSES succès
            # consécutifs avant de recommencer à incrémenter _eff.
            self._success_streak[resource] = 0
            log.warning("[queue.pools] overload %s → limit=%d", resource, self._eff[resource])
            # Notifie quand même : un changement d'état peut débloquer des logiques
            # de scheduling en attente (ex. wait_for_free avec une autre ressource).
            self._cond.notify_all()

    def on_success(self, resource: str) -> None:
        with self._cond:
            self._success_streak[resource] += 1
            if self._success_streak[resource] >= self.COOLDOWN_SUCCESSES:
                old = self._eff[resource]
                self._eff[resource] = min(self._max[resource], self._eff[resource] + 1)
                if self._eff[resource] > old:
                    # Capacité augmentée → peut débloquer des waiters.
                    self._cond.notify_all()
            log.debug(
                "[queue.pools] recover %s → limit=%d (streak=%d)",
                resource, self._eff[resource], self._success_streak[resource],
            )

    # ------------------------------------------------------------------
    # Attente de libération (FIX 3)
    # ------------------------------------------------------------------

    def wait_for_free(self, timeout: float | None = None) -> None:
        """Bloque jusqu'à ce qu'au moins une ressource soit disponible.

        Le prédicat accède directement à ``_used``/``_eff``/``_max`` — pas via
        ``free_resources()`` — pour éviter un deadlock : à l'intérieur de
        ``with self._cond:``, le lock est déjà acquis ; appeler ``free_resources()``
        tenterait de le ré-acquérir, ce qui bloquerait indéfiniment (un
        ``threading.Condition`` n'est pas réentrant par défaut).
        """
        with self._cond:
            self._cond.wait_for(
                lambda: any(self._used[r] < self._eff[r] for r in self._max),
                timeout,
            )
