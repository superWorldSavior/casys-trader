"""Runtime startup bootstrap helpers for daemon.main()."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from trader.reporting.ledger import decision_ledger
from trader.runtime import ledger_rotation
from trader.support.config.portfolio import load_starting_cash
from trader.infrastructure.state_db.broker_factory import (
    bootstrap_state_backend,
    make_scheduler,
)


class LoggerLike(Protocol):
    def info(self, *args: object) -> None: ...
    def warning(self, *args: object) -> None: ...


RotateMonthlyFn = Callable[..., dict]
LoadStartingCashFn = Callable[[Path], float]
BootstrapStateBackendFn = Callable[..., None]
MakeSchedulerFn = Callable[..., object]


@dataclass(frozen=True)
class RuntimeStateBootstrap:
    scheduler: object
    archive_dir: Path


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def bootstrap_runtime_state(
    *,
    state_dir: Path,
    config_dir: Path,
    state_backend: str,
    commission_model: object,
    now: datetime,
    logger: LoggerLike | None = None,
    rotate_monthly_fn: RotateMonthlyFn = ledger_rotation.rotate_monthly,
    load_starting_cash_fn: LoadStartingCashFn = load_starting_cash,
    bootstrap_state_backend_fn: BootstrapStateBackendFn = bootstrap_state_backend,
    make_scheduler_fn: MakeSchedulerFn = make_scheduler,
) -> RuntimeStateBootstrap:
    log = logger or _default_logger()
    archive_dir = state_dir / "archive"
    for path, ts_key in (
        (state_dir / decision_ledger.DEFAULT_LEDGER_FILENAME, "cycle_ts"),
        (state_dir / "events.jsonl", "ts"),
    ):
        try:
            rotation_result = rotate_monthly_fn(
                path,
                archive_dir,
                now=now,
                ts_key=ts_key,
            )
            if rotation_result["archived"]:
                log.info(
                    "[rotation] %s : archived=%d kept=%d files=%s",
                    path.name,
                    rotation_result["archived"],
                    rotation_result["kept"],
                    rotation_result["files"],
                )
        except Exception as exc:  # noqa: BLE001 - startup rotation is best-effort
            log.warning("[rotation] échec sur %s : %s", path.name, exc)

    bootstrap_state_backend_fn(
        state_dir=state_dir,
        starting_cash=load_starting_cash_fn(config_dir),
        commission_model=commission_model,
        backend=state_backend,
    )
    scheduler = make_scheduler_fn(state_dir=state_dir, backend=state_backend)
    return RuntimeStateBootstrap(scheduler=scheduler, archive_dir=archive_dir)
