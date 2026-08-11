from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.runtime.process_lifecycle import (
    ClosedProcessInstanceError,
    OpenProcessVersionMismatch,
    ProcessLifecycle,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import (
    BROKER_MIGRATION,
    PROCESS_TRACE_MIGRATION,
)
from trader.infrastructure.state_db.process_event_store import ProcessEventStore


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 9, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.now
        self.now += timedelta(seconds=1)
        return value


def _lifecycle(
    tmp_path: Path,
    *,
    bundle: str | None = "bundle-1",
    runtime_run_id: str = "runtime-1",
    store: ProcessEventStore | None = None,
) -> tuple[ProcessLifecycle, ProcessEventStore]:
    if store is None:
        db = StateDb(tmp_path / "casys.db")
        db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])
        store = ProcessEventStore(db)
    return (
        ProcessLifecycle(
            store=store,
            process_type="casys-trader.paper-decision-cycle",
            process_version="0.1",
            runtime_run_id=runtime_run_id,
            version_pins={"bundle_sha256": bundle},
            clock=Clock(),
        ),
        store,
    )


def _decision(attempt, **overrides):
    decision = {
        **attempt.decision_fields(),
        "decision_id": "decision-1",
        "reason": "hold",
        "schedule_effect": {"status": "verified", "next_wake": None},
    }
    decision.update(overrides)
    if "decision_readback" not in overrides:
        decision["decision_readback"] = {
            "status": "verified",
            "process_instance_id": decision.get("process_instance_id"),
            "attempt_id": decision.get("attempt_id"),
            "decision_id": decision.get("decision_id"),
        }
    return decision


def test_explicit_receipt_closes_a_completed_instance(tmp_path: Path) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("AAPL")

    lifecycle.finish_from_decision(
        attempt,
        _decision(attempt),
        closure_evidence_complete=True,
    )

    events = store.read_instance(attempt.identity.process_instance_id)
    assert [event.event_type for event in events] == [
        "instance_admitted",
        "instance_closed",
    ]
    assert events[-1].terminal_result == "completed"
    assert events[0].ts < events[1].ts


def test_incomplete_or_unknown_effect_enters_recovery_and_blocks_normal_resume(
    tmp_path: Path,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("AAPL")
    lifecycle.finish_from_decision(
        first,
        _decision(
            first,
            reason="queue_execute_timeout",
            effect_status="unknown",
            queue_task_id=42,
        ),
        closure_evidence_complete=False,
    )

    resumed = lifecycle.admit_or_resume("AAPL")

    assert resumed.recovery_required is True
    assert resumed.identity.process_instance_id == first.identity.process_instance_id
    assert resumed.identity.attempt_id != first.identity.attempt_id
    assert {ref["type"] for ref in resumed.prior_effect_refs} == {
        "decision",
        "decision_readback",
        "execute_task",
        "schedule_readback",
    }
    assert all(
        ref["process_instance_id"] == first.identity.process_instance_id
        and ref["attempt_id"] == first.identity.attempt_id
        and ref["decision_id"] == "decision-1"
        for ref in resumed.prior_effect_refs
    )
    assert store.read_instance(first.identity.process_instance_id)[-1].event_type == "attempt_started"


def test_interrupted_observational_attempt_resumes_without_blocking_decision(tmp_path: Path) -> None:
    lifecycle, _store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("MSFT")

    resumed = lifecycle.admit_or_resume("MSFT")

    assert resumed.recovery_required is False
    assert resumed.identity.process_instance_id == first.identity.process_instance_id
    assert resumed.identity.attempt_id != first.identity.attempt_id


def test_legacy_empty_recovery_marker_resumes_without_blocking_decision(tmp_path: Path) -> None:
    lifecycle, _store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("MSFT")
    lifecycle.defer(
        first,
        outcome_code="recovery_required",
        effect_status="unknown",
        effect_refs=(),
    )

    resumed = lifecycle.admit_or_resume("MSFT")

    assert resumed.recovery_required is False
    assert resumed.prior_effect_refs == ()
    assert resumed.identity.process_instance_id == first.identity.process_instance_id
    assert resumed.identity.attempt_id != first.identity.attempt_id


def test_normal_defer_can_be_retried_without_recovery(tmp_path: Path) -> None:
    lifecycle, _store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("NVDA")
    lifecycle.defer(first, outcome_code="no_price_deferred")

    retried = lifecycle.admit_or_resume("NVDA")

    assert retried.recovery_required is False
    assert retried.identity.process_instance_id == first.identity.process_instance_id


def test_dry_run_can_close_only_with_an_explicit_receipt(tmp_path: Path) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("TSLA")
    lifecycle.finish_from_decision(
        attempt,
        _decision(
            attempt,
            decision_id="d-dry",
            reason="ok",
            effect_status="not_applied_dry_run",
            execution_mode="dry_run",
        ),
        closure_evidence_complete=True,
    )

    events = store.read_instance(attempt.identity.process_instance_id)
    assert events[-1].terminal_result == "completed"
    assert {ref["type"] for ref in events[-1].effect_refs} == {
        "decision",
        "decision_readback",
        "schedule_readback",
        "execution_mode",
    }


def test_incomplete_dry_run_receipt_stays_open_without_claiming_unknown_effect(
    tmp_path: Path,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("TSLA")

    lifecycle.finish_from_decision(
        attempt,
        _decision(
            attempt,
            effect_status="not_applied_dry_run",
            execution_mode="dry_run",
        ),
        closure_evidence_complete=False,
    )

    latest = store.read_instance(attempt.identity.process_instance_id)[-1]
    assert latest.event_type == "attempt_finished"
    assert latest.terminal_result is None
    assert latest.outcome_code == "recovery_required"
    assert latest.effect_status == "not_applied_dry_run"


def test_open_instance_cannot_silently_change_governance_bundle(tmp_path: Path) -> None:
    lifecycle, store = _lifecycle(tmp_path, bundle="bundle-1")
    lifecycle.admit_or_resume("AAPL")
    changed, _ = _lifecycle(
        tmp_path,
        bundle="bundle-2",
        runtime_run_id="runtime-2",
        store=store,
    )

    with pytest.raises(OpenProcessVersionMismatch):
        changed.admit_or_resume("AAPL")


@pytest.mark.parametrize(
    ("first_bundle", "resumed_bundle"),
    [(None, "bundle-1"), ("bundle-1", None), ("   ", "bundle-1")],
)
def test_open_instance_resume_requires_a_non_empty_bundle_pin(
    tmp_path: Path,
    first_bundle: str | None,
    resumed_bundle: str | None,
) -> None:
    lifecycle, store = _lifecycle(tmp_path, bundle=first_bundle)
    lifecycle.admit_or_resume("AAPL")
    resumed, _ = _lifecycle(
        tmp_path,
        bundle=resumed_bundle,
        runtime_run_id="runtime-2",
        store=store,
    )

    with pytest.raises(OpenProcessVersionMismatch):
        resumed.admit_or_resume("AAPL")


def test_closed_instance_rejects_late_events_and_next_admission_gets_new_id(
    tmp_path: Path,
) -> None:
    lifecycle, _store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("AAPL")
    lifecycle.finish_from_decision(
        first,
        _decision(first, decision_id="d1"),
        closure_evidence_complete=True,
    )
    with pytest.raises(ClosedProcessInstanceError):
        lifecycle.defer(first, outcome_code="late")

    second = lifecycle.admit_or_resume("AAPL")
    assert second.identity.process_instance_id != first.identity.process_instance_id


def test_unknown_execute_task_cannot_be_closed_by_an_unrelated_schedule_receipt(
    tmp_path: Path,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("AAPL")
    lifecycle.finish_from_decision(
        first,
        _decision(
            first,
            reason="queue_execute_timeout",
            effect_status="unknown",
            queue_task_id=42,
        ),
        closure_evidence_complete=False,
    )
    resumed = lifecycle.admit_or_resume("AAPL")

    with pytest.raises(NotImplementedError, match="causal recovery receipt protocols"):
        lifecycle.close_after_recovery(
            resumed,
            terminal_result="completed",
            outcome_code="reconciled",
            effect_status="verified",
            effect_refs=[
                {
                    "type": "schedule_readback",
                    "status": "verified",
                    "process_instance_id": first.identity.process_instance_id,
                    "attempt_id": first.identity.attempt_id,
                    "decision_id": "decision-1",
                }
            ],
        )

    events = store.read_instance(first.identity.process_instance_id)
    assert events[-1].event_type == "attempt_started"
    assert all(event.event_type != "instance_closed" for event in events)


@pytest.mark.parametrize("terminal_result", ["failed", "cancelled", "escalated"])
def test_unimplemented_recovery_terminal_protocols_fail_closed_even_with_fake_refs(
    tmp_path: Path,
    terminal_result: str,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("AAPL")
    fake_refs = [
        {
            "type": "recovery_readback",
            "status": "verified",
            "process_instance_id": attempt.identity.process_instance_id,
            "attempt_id": attempt.identity.attempt_id,
            "decision_id": "decision-1",
        }
    ]

    with pytest.raises(NotImplementedError, match="receipt protocols"):
        lifecycle.close_after_recovery(
            attempt,
            terminal_result=terminal_result,
            outcome_code="fake_reconciliation",
            effect_status="verified",
            effect_refs=fake_refs,
        )

    assert store.read_instance(attempt.identity.process_instance_id)[-1].event_type == "instance_admitted"


@pytest.mark.parametrize(
    "decision",
    [
        {"reason": "hold", "schedule_effect": {"status": "verified"}},
        {
            "decision_id": "decision-1",
            "process_instance_id": "wrong-instance",
            "attempt_id": "attempt-1",
            "reason": "hold",
            "schedule_effect": {"status": "verified"},
        },
        {
            "decision_id": "decision-1",
            "process_instance_id": "instance-placeholder",
            "attempt_id": "attempt-placeholder",
            "reason": "hold",
        },
    ],
)
def test_completed_is_refused_without_minimum_correlated_receipt(
    tmp_path: Path,
    decision: dict,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("AAPL")
    if decision.get("process_instance_id") == "instance-placeholder":
        decision = {
            **decision,
            "process_instance_id": attempt.identity.process_instance_id,
            "attempt_id": attempt.identity.attempt_id,
        }

    lifecycle.finish_from_decision(
        attempt,
        decision,
        closure_evidence_complete=True,
    )

    events = store.read_instance(attempt.identity.process_instance_id)
    assert [event.event_type for event in events] == [
        "instance_admitted",
        "attempt_finished",
    ]
    assert events[-1].terminal_result is None
    assert events[-1].outcome_code == "recovery_required"


def test_exact_looking_recovery_readback_stays_open_until_protocol_is_defined(
    tmp_path: Path,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    first = lifecycle.admit_or_resume("AAPL")
    lifecycle.finish_from_decision(
        first,
        _decision(
            first,
            reason="queue_execute_timeout",
            effect_status="unknown",
            queue_task_id=42,
        ),
        closure_evidence_complete=False,
    )
    resumed = lifecycle.admit_or_resume("AAPL")

    with pytest.raises(NotImplementedError, match="causal recovery receipt protocols"):
        lifecycle.close_after_recovery(
            resumed,
            terminal_result="completed",
            outcome_code="reconciled",
            effect_status="verified",
            effect_refs=[
                {
                    "type": "recovery_readback",
                    "status": "verified",
                    "effect_status": "applied",
                    "process_instance_id": first.identity.process_instance_id,
                    "attempt_id": first.identity.attempt_id,
                    "decision_id": "decision-1",
                    "task_id": 42,
                }
            ],
        )

    assert store.read_instance(first.identity.process_instance_id)[-1].event_type == "attempt_started"


def test_decision_id_and_effect_receipt_without_decision_readback_stay_open(
    tmp_path: Path,
) -> None:
    lifecycle, store = _lifecycle(tmp_path)
    attempt = lifecycle.admit_or_resume("AAPL")

    lifecycle.finish_from_decision(
        attempt,
        {
            **attempt.decision_fields(),
            "decision_id": "decision-1",
            "reason": "hold",
            "schedule_effect": {"status": "verified", "next_wake": None},
        },
        closure_evidence_complete=True,
    )

    latest = store.read_instance(attempt.identity.process_instance_id)[-1]
    assert latest.event_type == "attempt_finished"
    assert latest.outcome_code == "recovery_required"
