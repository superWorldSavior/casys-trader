"""Shared protocols for runtime collaborators."""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "CycleReportWriter",
    "LoggerLike",
    "RecoverableLedger",
    "StartablePool",
    "Stoppable",
]


class LoggerLike(Protocol):
    def debug(self, *args: object, **kwargs: object) -> None: ...
    def info(self, *args: object, **kwargs: object) -> None: ...
    def warning(self, *args: object, **kwargs: object) -> None: ...
    def exception(self, *args: object, **kwargs: object) -> None: ...


class Stoppable(Protocol):
    def stop(self) -> None: ...


class CycleReportWriter(Protocol):
    def write_last_report(self, report: dict) -> None: ...
    def append_cycle_history(self, report: dict) -> None: ...


class RecoverableLedger(Protocol):
    path: object

    def recover_on_boot(self, *, now_ms: int) -> None: ...


class StartablePool(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
