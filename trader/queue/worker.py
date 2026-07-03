"""Worker : claim → acquire ressource → handler → complete/fail → release."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class RetryableError(Exception):
    """Le handler signale un échec transitoire → requeue avec backoff."""


class Worker:
    def __init__(self, ledger, pools, handlers, worker_id,
                 lease_ms=1_800_000, backoff_base_ms=1000):
        self._ledger = ledger
        self._pools = pools
        self._handlers = handlers
        self._id = worker_id
        self._lease_ms = lease_ms
        self._backoff_base_ms = backoff_base_ms

    def run_once(self, *, now_ms, token) -> bool:
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
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error="resource_unavailable", retryable=True,
                              backoff_base_ms=self._backoff_base_ms)
            return True
        try:
            self._handlers[task["kind"]](task)
            self._ledger.complete(task_id=task["id"], token=token, now_ms=now_ms)
            log.debug("[queue.worker] complete id=%s kind=%s", task["id"], task["kind"])
            if resource:
                self._pools.on_success(resource)
        except RetryableError as exc:
            log.warning("[queue.worker] retryable fail id=%s: %s", task["id"], exc)
            if resource:
                self._pools.on_overload(resource)
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error=str(exc), retryable=True,
                              backoff_base_ms=self._backoff_base_ms)
        except Exception as exc:  # noqa: BLE001 — frontière handler
            log.warning("[queue.worker] fatal fail id=%s: %s", task["id"],
                        f"{type(exc).__name__}: {exc}")
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error=f"{type(exc).__name__}: {exc}",
                              retryable=False, backoff_base_ms=self._backoff_base_ms)
        finally:
            if resource:
                self._pools.release(resource)
        return True
