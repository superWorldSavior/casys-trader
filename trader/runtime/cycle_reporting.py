"""Runtime cycle report persistence helpers."""

from __future__ import annotations

from typing import Protocol


class CycleReportWriter(Protocol):
    def write_last_report(self, report: dict) -> None: ...

    def append_cycle_history(self, report: dict) -> None: ...


def has_cycle_activity(report: dict) -> bool:
    return bool(report.get("planned_exits") or report.get("decisions") or report.get("exit_watch_triggers"))


def persist_cycle_report(
    report: dict,
    *,
    writer: CycleReportWriter,
    only_if_active: bool = False,
) -> bool:
    if only_if_active and not has_cycle_activity(report):
        return False
    writer.write_last_report(report)
    writer.append_cycle_history(report)
    return True
