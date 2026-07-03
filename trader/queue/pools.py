"""Pools de ressources : bornage de concurrence par ressource externe + AIMD."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class ResourcePools:
    def __init__(self, limits: dict[str, int]):
        self._max = dict(limits)                       # plafond initial
        self._eff = dict(limits)                       # limite effective (AIMD)
        self._used = {r: 0 for r in limits}
        self._lock = threading.Lock()

    def effective_limit(self, resource) -> int:
        with self._lock:
            return self._eff[resource]

    def free_resources(self) -> list[str]:
        with self._lock:
            return [r for r in self._max
                    if self._used[r] < self._eff[r]]

    def try_acquire(self, resource) -> bool:
        with self._lock:
            if self._used[resource] < self._eff[resource]:
                self._used[resource] += 1
                return True
            return False

    def release(self, resource) -> None:
        with self._lock:
            if self._used[resource] > 0:
                self._used[resource] -= 1

    def on_overload(self, resource) -> None:
        with self._lock:
            self._eff[resource] = max(1, self._eff[resource] // 2)
            log.warning("[queue.pools] overload %s → limit=%d", resource, self._eff[resource])

    def on_success(self, resource) -> None:
        with self._lock:
            self._eff[resource] = min(self._max[resource], self._eff[resource] + 1)
            log.debug("[queue.pools] recover %s → limit=%d", resource, self._eff[resource])
