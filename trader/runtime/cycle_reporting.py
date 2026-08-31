"""Runtime cycle report persistence helpers."""

from __future__ import annotations

from typing import Protocol

from trader.domain.planning.trigger_outbox import TRIGGER_OUTBOX_ID_FIELD
from trader.runtime.protocols import CycleReportWriter


class TriggerOutboxAcknowledger(Protocol):
    def ack_indicator_triggers(self, outbox_ids: list[str]) -> None: ...


def has_cycle_activity(report: dict) -> bool:
    return bool(report.get("planned_exits") or report.get("decisions") or report.get("exit_watch_triggers"))


def persist_cycle_report(
    report: dict,
    *,
    writer: CycleReportWriter,
    only_if_active: bool = False,
    trigger_scheduler: TriggerOutboxAcknowledger | None = None,
) -> bool:
    if only_if_active and not has_cycle_activity(report):
        return False
    writer.write_last_report(report)
    writer.append_cycle_history(report)
    if trigger_scheduler is not None:
        outbox_ids = list(
            dict.fromkeys(
                str(trigger[TRIGGER_OUTBOX_ID_FIELD])
                for trigger in report.get("indicator_triggers", [])
                if isinstance(trigger, dict)
                and trigger.get(TRIGGER_OUTBOX_ID_FIELD)
            )
        )
        if outbox_ids:
            trigger_scheduler.ack_indicator_triggers(outbox_ids)
    return True
