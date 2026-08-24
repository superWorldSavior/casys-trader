"""Bounded deadline owner for source-only macro port operations.

One process-wide slot cap. Callers get a typed timeout without a new thread per
source beyond that cap, and without a ThreadPoolExecutor atexit join. Hung
workers occupy a slot until the underlying call returns; the cap is the slot
count, not the number of sources attempted. Worker threads are daemon so a
timed-out hang cannot block interpreter shutdown.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")
_SOURCE_ONLY_MAX_WORKERS = 8
_LOCK = threading.Lock()
_DEADLINE: BoundedSourceDeadline | None = None


class BoundedSourceDeadline:
    """Single owner of source-only read_facts deadlines."""

    def __init__(self, *, max_workers: int = _SOURCE_ONLY_MAX_WORKERS) -> None:
        workers = int(max_workers)
        if workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = workers
        self._slots = threading.BoundedSemaphore(workers)

    def run(self, operation: Callable[[], _T], *, timeout_s: float) -> _T:
        timeout = float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        deadline = time.monotonic() + timeout
        if not self._slots.acquire(timeout=timeout):
            raise TimeoutError("timeout")
        box: dict[str, object] = {}
        finished = threading.Event()

        def _run() -> None:
            try:
                try:
                    box["value"] = operation()
                except Exception as exc:  # noqa: BLE001 - preserve source-specific failure
                    box["error"] = exc
            finally:
                self._slots.release()
                finished.set()

        try:
            threading.Thread(target=_run, daemon=True, name="world-macro-src").start()
        except Exception:
            self._slots.release()
            raise
        remaining = max(0.0, deadline - time.monotonic())
        if not finished.wait(remaining):
            raise TimeoutError("timeout")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        if "value" not in box:
            raise TimeoutError("timeout")
        return box["value"]  # type: ignore[return-value]


def source_only_deadline() -> BoundedSourceDeadline:
    global _DEADLINE
    with _LOCK:
        if _DEADLINE is None:
            _DEADLINE = BoundedSourceDeadline(max_workers=_SOURCE_ONLY_MAX_WORKERS)
        return _DEADLINE


__all__ = ["BoundedSourceDeadline", "source_only_deadline"]
