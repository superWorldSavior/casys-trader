from __future__ import annotations

from trader.runtime import cycle_reporting


class FakeWriter:
    def __init__(self) -> None:
        self.last_reports: list[dict] = []
        self.history_reports: list[dict] = []

    def write_last_report(self, report: dict) -> None:
        self.last_reports.append(report)

    def append_cycle_history(self, report: dict) -> None:
        self.history_reports.append(report)


class FakeTriggerScheduler:
    def __init__(self) -> None:
        self.acks: list[list[str]] = []

    def ack_indicator_triggers(self, outbox_ids: list[str]) -> None:
        self.acks.append(list(outbox_ids))


def _report(**overrides: object) -> dict:
    report = {
        "ts": "2026-07-05T10:00:00+00:00",
        "decisions": [],
        "planned_exits": [],
        "exit_watch_triggers": [],
    }
    report.update(overrides)
    return report


def test_persist_cycle_report_ecrit_last_report_et_history() -> None:
    writer = FakeWriter()
    report = _report(decisions=[{"symbol": "SPY"}])

    persisted = cycle_reporting.persist_cycle_report(report, writer=writer)

    assert persisted is True
    assert writer.last_reports == [report]
    assert writer.history_reports == [report]


def test_persist_cycle_report_skip_inactive_when_requested() -> None:
    writer = FakeWriter()

    persisted = cycle_reporting.persist_cycle_report(
        _report(),
        writer=writer,
        only_if_active=True,
    )

    assert persisted is False
    assert writer.last_reports == []
    assert writer.history_reports == []


def test_persist_cycle_report_considers_planned_exits_and_exit_watches_active() -> None:
    for report in (
        _report(planned_exits=[{"symbol": "SPY"}]),
        _report(exit_watch_triggers=[{"symbol": "SPY"}]),
    ):
        writer = FakeWriter()

        persisted = cycle_reporting.persist_cycle_report(report, writer=writer, only_if_active=True)

        assert persisted is True
        assert writer.last_reports == [report]
        assert writer.history_reports == [report]


def test_trigger_outbox_ack_happens_only_after_full_report_persistence() -> None:
    scheduler = FakeTriggerScheduler()
    report = _report(
        indicator_triggers=[
            {"watch_id": "SPY:w1", "trigger_outbox_id": "SPY:w1"},
        ]
    )

    cycle_reporting.persist_cycle_report(
        report,
        writer=FakeWriter(),
        trigger_scheduler=scheduler,
    )

    assert scheduler.acks == [["SPY:w1"]]


def test_trigger_outbox_is_not_acked_when_history_persistence_fails() -> None:
    class FailingHistoryWriter(FakeWriter):
        def append_cycle_history(self, report: dict) -> None:
            del report
            raise OSError("history unavailable")

    scheduler = FakeTriggerScheduler()
    report = _report(
        indicator_triggers=[
            {"watch_id": "SPY:w1", "trigger_outbox_id": "SPY:w1"},
        ]
    )

    import pytest

    with pytest.raises(OSError, match="history unavailable"):
        cycle_reporting.persist_cycle_report(
            report,
            writer=FailingHistoryWriter(),
            trigger_scheduler=scheduler,
        )

    assert scheduler.acks == []
