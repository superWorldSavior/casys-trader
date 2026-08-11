from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.process_trace import ProcessIdentity
from trader.runtime import process_pilot
from trader.runtime.process_lifecycle import ProcessAttemptContext
from trader.runtime.process_pilot import ProcessPilot, ProcessPilotBindingError
from trader.runtime.process_pilot import build_process_pilot, closure_evidence_complete


def _base(**overrides):
    decision = {
        "symbol": "SPY",
        "process_instance_id": "instance-1",
        "attempt_id": "attempt-1",
        "decision_id": "decision-1",
        "decision_readback": {
            "status": "verified",
            "symbol": "SPY",
            "process_instance_id": "instance-1",
            "attempt_id": "attempt-1",
            "decision_id": "decision-1",
        },
        "schedule_effect": {"status": "verified", "next_wake": None},
        "admission_status": "not_required",
        "executed": False,
        "reason": "hold",
    }
    decision.update(overrides)
    return decision


def test_hold_or_rejection_closes_only_with_scheduler_readback() -> None:
    assert closure_evidence_complete(_base()) is True
    assert closure_evidence_complete(_base(schedule_effect={"status": "unavailable"})) is False


def test_hold_plan_review_requires_its_plan_readback() -> None:
    assert closure_evidence_complete(_base(plan_review_recorded=True, plan_effect={"status": "unavailable"})) is False
    assert (
        closure_evidence_complete(
            _base(
                plan_review_recorded=True,
                plan_effect={
                    "status": "verified",
                    "expected_mutation": {"kind": "last_llm_review", "review": {}},
                    "expected_last_llm_review": {},
                    "observed_last_llm_reviews": [{"plan_id": "plan-1", "last_llm_review": {}}],
                },
            )
        )
        is True
    )


def test_all_plan_receipts_must_be_verified_when_mutations_are_composed() -> None:
    assert (
        closure_evidence_complete(
            _base(
                plan_review_recorded=True,
                exit_update={"hard_stop": 97.0},
                plan_effect={"status": "verified"},
                plan_effects=[
                    {"status": "mismatch", "reason": "plan_receipt_mismatch"},
                    {"status": "verified"},
                ],
            )
        )
        is False
    )


def test_in_memory_decision_id_without_exact_ledger_readback_never_completes() -> None:
    assert closure_evidence_complete(_base(decision_readback=None)) is False
    assert (
        closure_evidence_complete(
            _base(
                decision_readback={
                    "status": "verified",
                    "symbol": "SPY",
                    "process_instance_id": "instance-1",
                    "attempt_id": "other-attempt",
                    "decision_id": "decision-1",
                }
            )
        )
        is False
    )


def test_admitted_execution_requires_exact_fill_and_plan_readback() -> None:
    plan = {"id": "plan-1", "symbol": "SPY", "remaining_quantity": 1.0}
    decision = _base(
        admission_status="admitted",
        executed=True,
        effect_status="verified",
        intent="OPEN_LONG",
        trade_plan_created=True,
        effect_refs={
            "broker_fill": {
                "symbol": "SPY",
                "process_instance_id": "instance-1",
                "attempt_id": "attempt-1",
                "decision_id": "decision-1",
            }
        },
        trade_plan=plan,
        plan_effect={
            "status": "verified",
            "open_plans": [{"plan_id": "plan-1"}],
            "expected_mutation": {"kind": "upsert", "plan": plan},
            "expected_plan": plan,
            "observed_plan": plan,
        },
    )

    assert closure_evidence_complete(decision) is True
    assert closure_evidence_complete({**decision, "plan_effect": {"status": "unavailable"}}) is False
    mismatch = {
        **decision,
        "effect_refs": {"broker_fill": {**decision["effect_refs"]["broker_fill"], "attempt_id": "other"}},
    }
    assert closure_evidence_complete(mismatch) is False
    missing_plan = {**decision, "plan_effect": {**decision["plan_effect"], "observed_plan": None}}
    assert closure_evidence_complete(missing_plan) is False
    wrong_symbol = {
        **decision,
        "effect_refs": {"broker_fill": {**decision["effect_refs"]["broker_fill"], "symbol": "QQQ"}},
    }
    assert closure_evidence_complete(wrong_symbol) is False


def test_admitted_non_effect_and_unknown_effect_never_complete() -> None:
    assert closure_evidence_complete(_base(admission_status="admitted", effect_status="not_applied")) is False
    assert closure_evidence_complete(_base(effect_status="unknown", process_state="recovery_required")) is False


def test_dry_run_is_an_explicit_terminal_non_application() -> None:
    assert (
        closure_evidence_complete(
            _base(
                admission_status="admitted",
                action="BUY",
                reason="ok",
                execution_mode="dry_run",
                effect_status="not_applied_dry_run",
            )
        )
        is True
    )


def test_builder_migrates_a_fresh_database_before_first_admission(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        process_pilot,
        "current_governance_version",
        lambda _root: {
            "process_id": "casys-trader.paper-decision-cycle",
            "process_version": "0.1",
            "bundle_sha256": "bundle-1",
            "runtime_binding_status": "integrated",
        },
    )
    pilot = build_process_pilot(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_run_id="runtime-1",
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    pilot.admit_many(["SPY"], causes_by_symbol={"SPY": []})

    assert pilot.decision_fields("SPY")["process_instance_id"]


def test_builder_refuses_a_false_binding_before_opening_state(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        process_pilot,
        "current_governance_version",
        lambda _root: {
            "process_id": "casys-trader.paper-decision-cycle",
            "process_version": "0.1",
            "bundle_sha256": "bundle-1",
            "runtime_binding_status": "not_integrated",
        },
    )

    def forbidden_open(_path):
        raise AssertionError("state DB must not open for a false binding")

    monkeypatch.setattr(process_pilot, "open_state_db", forbidden_open)

    with pytest.raises(ProcessPilotBindingError, match="runtime_binding.status=integrated"):
        build_process_pilot(
            repo_root=tmp_path,
            state_dir=tmp_path / "state",
            runtime_run_id="runtime-1",
            clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        )


def test_recovery_deferral_preserves_unresolved_causal_refs() -> None:
    unresolved = ({"type": "execute_task", "task_id": 42},)
    attempt = ProcessAttemptContext(
        identity=ProcessIdentity(
            process_instance_id="instance-1",
            attempt_id="attempt-2",
            runtime_run_id="runtime-1",
        ),
        process_type="casys-trader.paper-decision-cycle",
        process_version="0.1",
        work_object_type="paper-symbol",
        work_object_key="SPY",
        version_pins={"bundle_sha256": "bundle-1"},
        recovery_required=True,
        prior_effect_refs=unresolved,
    )

    class RecordingLifecycle:
        def __init__(self) -> None:
            self.deferred: list[dict] = []

        def admit_or_resume(self, _symbol, *, caused_by=()):
            return attempt

        def defer(self, context, **kwargs) -> None:
            self.deferred.append({"context": context, **kwargs})

    lifecycle = RecordingLifecycle()
    pilot = ProcessPilot(lifecycle=lifecycle, governance_ref={})  # type: ignore[arg-type]
    pilot.admit_many(["SPY"], causes_by_symbol={"SPY": []})

    pilot.defer_recovery("SPY")

    assert lifecycle.deferred == [
        {
            "context": attempt,
            "outcome_code": "recovery_required",
            "effect_status": "unknown",
            "effect_refs": unresolved,
        }
    ]
