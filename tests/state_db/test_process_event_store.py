"""Tests unitaires du store append-only des événements de processus."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from trader.domain.process_trace import ProcessEvent, ProcessIdentity
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import BROKER_MIGRATION, PROCESS_TRACE_MIGRATION
from trader.infrastructure.state_db.process_event_store import (
    ProcessEventConflictError,
    ProcessEventStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> ProcessEventStore:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])
    return ProcessEventStore(db)


def _event(**overrides) -> ProcessEvent:
    values = {
        "event_id": "9fef28d7-5f8b-4b77-a503-671012f2db64",
        "process_type": "casys-trader.paper-decision",
        "process_version": "0.1",
        "process_instance_id": "b13e1b4d-1f39-46d8-af79-57081e81f9dc",
        "attempt_id": "9b3f9178-a5fb-4fc3-b44c-cb47888aa4ac",
        "runtime_run_id": "b9a6b902-45f5-4c29-a7e4-f89a0e1c750a",
        "work_object_type": "paper-symbol",
        "work_object_key": "AAPL",
        "event_type": "attempt_finished",
        "ts": "2026-08-09T10:30:00+08:00",
        "caused_by": ({"type": "scheduler_wake", "ref": "AAPL@2026-08-09T02:30:00Z"},),
        "outcome_code": "hold_recorded",
        "effect_status": "verified",
        "effect_refs": ({"type": "decision", "decision_id": "cycle|0|AAPL"},),
        "version_pins": {"process_definition": "casys-trader.paper-decision@0.1"},
    }
    values.update(overrides)
    return ProcessEvent(**values)


def test_append_and_read_round_trip(store: ProcessEventStore) -> None:
    event = _event()

    assert store.append(event) is True

    assert store.get(event.event_id) == event
    assert event.ts == "2026-08-09T02:30:00+00:00"


def test_identical_event_replay_is_an_idempotent_noop(store: ProcessEventStore) -> None:
    event = _event()

    assert store.append(event) is True
    assert store.append(event) is False
    assert store.read_instance(event.process_instance_id) == [event]


def test_event_id_reuse_with_different_content_fails_closed(store: ProcessEventStore) -> None:
    event = _event()
    store.append(event)

    with pytest.raises(ProcessEventConflictError, match="different content"):
        store.append(replace(event, outcome_code="different_outcome"))


def test_reads_an_instance_in_append_order_and_latest_exact_work(
    store: ProcessEventStore,
) -> None:
    admitted = _event(event_id="event-1", event_type="instance_admitted", outcome_code=None)
    finished = _event(event_id="event-2")
    other = _event(
        event_id="event-3",
        process_instance_id="another-instance",
        work_object_key="MSFT",
    )
    for event in (admitted, finished, other):
        store.append(event)

    assert store.read_instance(admitted.process_instance_id) == [admitted, finished]
    assert (
        store.latest_for_work(
            process_type=admitted.process_type,
            work_object_type=admitted.work_object_type,
            work_object_key=admitted.work_object_key,
        )
        == finished
    )


def test_identity_keeps_instance_and_renews_attempt() -> None:
    identity = ProcessIdentity.create()
    resumed = identity.next_attempt(runtime_run_id="new-runtime")

    UUID(identity.process_instance_id)
    UUID(identity.attempt_id)
    UUID(identity.runtime_run_id)
    assert resumed.process_instance_id == identity.process_instance_id
    assert resumed.attempt_id != identity.attempt_id
    assert resumed.runtime_run_id == "new-runtime"


def test_event_copies_nested_json_input() -> None:
    cause = {"type": "watch", "details": {"id": "watch-1"}}
    event = ProcessEvent.create(
        process_type="casys-trader.paper-decision",
        process_version="0.1",
        process_instance_id="instance-1",
        attempt_id="attempt-1",
        runtime_run_id="runtime-1",
        work_object_type="paper-symbol",
        work_object_key="AAPL",
        event_type="instance_admitted",
        caused_by=[cause],
    )

    cause["details"]["id"] = "mutated"
    assert event.caused_by[0]["details"]["id"] == "watch-1"


@pytest.mark.parametrize("terminal_result", ["completed", "failed", "cancelled", "escalated"])
def test_closed_event_accepts_governed_terminal_results(terminal_result: str) -> None:
    event = _event(event_type="instance_closed", terminal_result=terminal_result)
    assert event.terminal_result == terminal_result


def test_terminal_result_is_rejected_on_non_closure_event() -> None:
    with pytest.raises(ValueError, match="only valid on instance_closed"):
        _event(terminal_result="completed")


def test_closure_requires_a_terminal_result() -> None:
    with pytest.raises(ValueError, match="requires terminal_result"):
        _event(event_type="instance_closed")


def test_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="include a timezone"):
        _event(ts="2026-08-09T10:30:00")
