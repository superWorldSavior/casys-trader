"""Runtime cycle report persistence helpers."""

from __future__ import annotations

from trader.runtime.protocols import CycleReportWriter


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
