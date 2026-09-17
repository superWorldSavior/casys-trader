"""Best-effort daemon shutdown helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from trader.runtime.protocols import LoggerLike, Stoppable


DisconnectFn = Callable[[object], None]


class ReleasePidFileFn(Protocol):
    def __call__(self, *, pid_file: Path, pid: int) -> None: ...


def _stop_pool(pool: Stoppable | None, *, message: str, logger: LoggerLike) -> None:
    if pool is None:
        return
    try:
        pool.stop()
        logger.info(message)
    except Exception:  # noqa: BLE001 - best-effort shutdown must never block exit
        pass


def shutdown_runtime_resources(
    *,
    universe_intelligence_runner: Stoppable | None,
    news_macro_runner: Stoppable | None,
    decide_pool: Stoppable | None,
    execute_pool: Stoppable | None,
    data_source: object | None,
    pid_file: Path,
    pid: int,
    disconnect_quietly: DisconnectFn,
    release_pid_file: ReleasePidFileFn,
    logger: LoggerLike,
    company_intelligence_runner: Stoppable | None = None,
    learning_sync_runner: Stoppable | None = None,
    world_about_runner: Stoppable | None = None,
) -> None:
    _stop_pool(
        learning_sync_runner,
        message="[learnings_sync] runner arrêté",
        logger=logger,
    )
    _stop_pool(
        world_about_runner,
        message="[world_about] runner arrêté",
        logger=logger,
    )
    _stop_pool(
        company_intelligence_runner,
        message="[company_intelligence] runtime arrêté",
        logger=logger,
    )
    _stop_pool(
        universe_intelligence_runner,
        message="[universe_intelligence] runner arrêté",
        logger=logger,
    )
    _stop_pool(news_macro_runner, message="[news_macro] runner arrêté", logger=logger)
    _stop_pool(decide_pool, message="[queue_decide] pool arrêté", logger=logger)
    _stop_pool(execute_pool, message="[queue_execute] pool arrêté", logger=logger)
    if data_source is not None:
        disconnect_quietly(data_source)
    try:
        release_pid_file(pid_file=pid_file, pid=pid)
    except Exception:  # noqa: BLE001 - best-effort shutdown must never block exit
        pass
