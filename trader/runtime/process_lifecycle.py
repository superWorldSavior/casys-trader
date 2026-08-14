"""Lifecycle contract for the opt-in APE paper-decision pilot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trader.domain.process_trace import ProcessEvent, ProcessIdentity, new_attempt_id
from trader.infrastructure.state_db.process_event_store import ProcessEventStore


class OpenProcessVersionMismatch(RuntimeError):
    """An open instance cannot silently adopt another governance bundle."""


class ClosedProcessInstanceError(RuntimeError):
    """No event may be appended to an instance after its closure."""


@dataclass(frozen=True)
class ProcessAttemptContext:
    identity: ProcessIdentity
    process_type: str
    process_version: str
    work_object_type: str
    work_object_key: str
    version_pins: dict[str, Any]
    recovery_required: bool = False
    prior_effect_refs: tuple[dict[str, Any], ...] = ()

    def decision_fields(self) -> dict[str, Any]:
        return {
            "process_instance_id": self.identity.process_instance_id,
            "attempt_id": self.identity.attempt_id,
            "runtime_run_id": self.identity.runtime_run_id,
            "governance_version": dict(self.version_pins),
        }


class ProcessLifecycle:
    """Journal admission, attempts and evidence-backed terminal outcomes."""

    def __init__(
        self,
        *,
        store: ProcessEventStore,
        process_type: str,
        process_version: str,
        runtime_run_id: str,
        version_pins: Mapping[str, Any],
        clock: Callable[[], datetime] | None = None,
        work_object_type: str = "paper-symbol",
    ) -> None:
        self._store = store
        self._process_type = str(process_type)
        self._process_version = str(process_version)
        self._runtime_run_id = str(runtime_run_id)
        self._version_pins = dict(version_pins)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._work_object_type = str(work_object_type)

    def admit_or_resume(
        self,
        work_object_key: str,
        *,
        caused_by: Sequence[Mapping[str, Any]] = (),
    ) -> ProcessAttemptContext:
        latest = self._store.latest_for_work(
            process_type=self._process_type,
            work_object_type=self._work_object_type,
            work_object_key=work_object_key,
        )
        open_instance = latest is not None and latest.event_type != "instance_closed"
        history: list[ProcessEvent] = []
        recovery_required = False
        prior_effect_refs: tuple[dict[str, Any], ...] = ()
        if open_instance:
            history = self._store.read_instance(latest.process_instance_id)
            try:
                self._validate_open_version(latest)
            except OpenProcessVersionMismatch:
                if not self._can_supersede_without_effect(latest, history):
                    raise
                self._close_superseded_instance(latest)
                open_instance = False

        if open_instance:
            unresolved = next(
                (
                    event
                    for event in reversed(history)
                    if event.effect_status == "unknown" or event.outcome_code == "recovery_required"
                ),
                None,
            )
            # The process pilot is an observability layer, not a dispatch gate.
            # A daemon can stop after admission/attempt start without having
            # produced any committing effect.  In that case the durable trace
            # is enough to resume the same instance with a fresh attempt; it is
            # not evidence of an unknown business effect.
            #
            # Recovery remains explicit only when the trace carries causal
            # effect references that genuinely need reconciliation.
            recovery_required = unresolved is not None and bool(unresolved.effect_refs)
            if unresolved is not None:
                prior_effect_refs = unresolved.effect_refs

        if open_instance:
            assert latest is not None
            identity = ProcessIdentity(
                process_instance_id=latest.process_instance_id,
                attempt_id=new_attempt_id(),
                runtime_run_id=self._runtime_run_id,
            )
            event_type = "attempt_started"
        else:
            identity = ProcessIdentity.create(runtime_run_id=self._runtime_run_id)
            event_type = "instance_admitted"

        context = ProcessAttemptContext(
            identity=identity,
            process_type=self._process_type,
            process_version=self._process_version,
            work_object_type=self._work_object_type,
            work_object_key=work_object_key,
            version_pins=dict(self._version_pins),
            recovery_required=recovery_required,
            prior_effect_refs=prior_effect_refs,
        )
        self._append(context, event_type=event_type, caused_by=caused_by)
        return context

    def finish_from_decision(
        self,
        context: ProcessAttemptContext,
        decision: Mapping[str, Any],
        *,
        closure_evidence_complete: bool,
    ) -> None:
        effect_status = str(decision.get("effect_status") or "not_applied")
        refs = _effect_refs(context, decision)
        evidence_correlated = _decision_closure_evidence_is_correlated(
            context,
            decision,
            refs,
        )
        recovery = (
            context.recovery_required
            or decision.get("process_state") == "recovery_required"
            or effect_status == "unknown"
            or not closure_evidence_complete
            or not evidence_correlated
        )
        outcome_code = str(decision.get("reason") or "decision_recorded")
        if recovery:
            recovery_effect_status = (
                "unknown"
                if context.recovery_required
                or decision.get("process_state") == "recovery_required"
                or effect_status == "unknown"
                else effect_status
            )
            self._append(
                context,
                event_type="attempt_finished",
                outcome_code="recovery_required",
                effect_status=recovery_effect_status,
                effect_refs=refs,
            )
            return
        # A terminal result is one atomic append. A separate attempt_finished
        # followed by instance_closed would create a crash window between them.
        self._append(
            context,
            event_type="instance_closed",
            terminal_result="completed",
            outcome_code=outcome_code,
            effect_status=effect_status,
            effect_refs=refs,
        )

    def defer(
        self,
        context: ProcessAttemptContext,
        *,
        outcome_code: str,
        effect_status: str = "not_applied",
        effect_refs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Finish an attempt without claiming a terminal process result."""
        self._append(
            context,
            event_type="attempt_finished",
            outcome_code=outcome_code,
            effect_status=effect_status,
            effect_refs=effect_refs,
        )

    def close_after_recovery(
        self,
        context: ProcessAttemptContext,
        *,
        terminal_result: str,
        outcome_code: str,
        effect_status: str,
        effect_refs: Sequence[Mapping[str, Any]],
    ) -> None:
        """Refuse recovery closure until causal readback protocols are defined.

        Correlation fields alone cannot prove that every unresolved prior
        effect was reconciled. The opt-in MVP therefore keeps the instance
        open instead of accepting a receipt shape that is not yet governed.
        """
        raise NotImplementedError(
            "causal recovery receipt protocols are not implemented; the process instance must remain open"
        )

    def _validate_open_version(self, latest: ProcessEvent) -> None:
        latest_bundle = _bundle_sha256(latest.version_pins)
        current_bundle = _bundle_sha256(self._version_pins)
        if (
            latest.process_version != self._process_version
            or not latest_bundle
            or not current_bundle
            or latest_bundle != current_bundle
        ):
            raise OpenProcessVersionMismatch(
                "open instance is pinned to another governance version; "
                "reconcile or explicitly migrate it before resuming"
            )

    def _append(
        self,
        context: ProcessAttemptContext,
        *,
        event_type: str,
        caused_by: Sequence[Mapping[str, Any]] = (),
        terminal_result: str | None = None,
        outcome_code: str | None = None,
        effect_status: str | None = None,
        effect_refs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        history = self._store.read_instance(context.identity.process_instance_id)
        if history and history[-1].event_type == "instance_closed":
            raise ClosedProcessInstanceError(context.identity.process_instance_id)
        self._store.append(
            ProcessEvent.create(
                process_type=context.process_type,
                process_version=context.process_version,
                process_instance_id=context.identity.process_instance_id,
                attempt_id=context.identity.attempt_id,
                runtime_run_id=context.identity.runtime_run_id,
                work_object_type=context.work_object_type,
                work_object_key=context.work_object_key,
                event_type=event_type,
                ts=self._clock().isoformat(),
                caused_by=caused_by,
                terminal_result=terminal_result,
                outcome_code=outcome_code,
                effect_status=effect_status,
                effect_refs=effect_refs,
                version_pins=context.version_pins,
            )
        )

    def _can_supersede_without_effect(
        self,
        latest: ProcessEvent,
        history: Sequence[ProcessEvent],
    ) -> bool:
        """Allow a new bundle only after explicitly cancelling inert old work."""

        if not _bundle_sha256(latest.version_pins) or not _bundle_sha256(self._version_pins):
            return False
        return bool(history) and all(
            event.terminal_result is None
            and event.outcome_code != "recovery_required"
            and event.effect_status in {None, "not_applied", "not_applied_dry_run"}
            and not event.effect_refs
            for event in history
        )

    def _close_superseded_instance(self, latest: ProcessEvent) -> None:
        """Record an explicit terminal event before admitting the new bundle."""

        context = ProcessAttemptContext(
            identity=ProcessIdentity(
                process_instance_id=latest.process_instance_id,
                attempt_id=latest.attempt_id or new_attempt_id(),
                runtime_run_id=self._runtime_run_id,
            ),
            process_type=latest.process_type,
            process_version=latest.process_version,
            work_object_type=latest.work_object_type,
            work_object_key=latest.work_object_key,
            version_pins=dict(latest.version_pins or {}),
        )
        self._append(
            context,
            event_type="instance_closed",
            terminal_result="cancelled",
            outcome_code="governance_version_superseded",
            effect_status="not_applied",
        )


def _effect_refs(
    context: ProcessAttemptContext,
    decision: Mapping[str, Any],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    decision_id = str(decision.get("decision_id") or "").strip()
    correlation = {
        "process_instance_id": context.identity.process_instance_id,
        "attempt_id": context.identity.attempt_id,
        "decision_id": decision_id,
    }
    if decision_id:
        refs.append({"type": "decision", **correlation})
    if isinstance(decision.get("decision_readback"), Mapping):
        refs.append(
            {
                **dict(decision["decision_readback"]),
                "type": "decision_readback",
            }
        )
    if decision.get("queue_task_id") is not None:
        refs.append(
            {
                "type": "execute_task",
                "task_id": decision["queue_task_id"],
                **correlation,
            }
        )
    if isinstance(decision.get("schedule_effect"), Mapping):
        refs.append(
            {
                "type": "schedule_readback",
                **correlation,
                **dict(decision["schedule_effect"]),
            }
        )
    plan_effects = decision.get("plan_effects")
    if isinstance(plan_effects, list):
        for plan_effect in plan_effects:
            if isinstance(plan_effect, Mapping):
                refs.append(
                    {
                        "type": "plan_readback",
                        **correlation,
                        **dict(plan_effect),
                    }
                )
    elif isinstance(decision.get("plan_effect"), Mapping):
        refs.append(
            {
                "type": "plan_readback",
                **correlation,
                **dict(decision["plan_effect"]),
            }
        )
    if isinstance(decision.get("effect_refs"), Mapping):
        for ref_type, raw_ref in decision["effect_refs"].items():
            if isinstance(raw_ref, Mapping):
                refs.append({"type": str(ref_type), **dict(raw_ref)})
    if decision.get("execution_mode") == "dry_run":
        refs.append(
            {
                "type": "execution_mode",
                "mode": "dry_run",
                "status": "not_applied",
                **correlation,
            }
        )
    return refs


def _bundle_sha256(version_pins: Mapping[str, Any] | None) -> str | None:
    if not isinstance(version_pins, Mapping):
        return None
    value = version_pins.get("bundle_sha256")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _decision_closure_evidence_is_correlated(
    context: ProcessAttemptContext,
    decision: Mapping[str, Any],
    refs: Sequence[Mapping[str, Any]],
) -> bool:
    decision_id = str(decision.get("decision_id") or "").strip()
    if not decision_id:
        return False
    if decision.get("process_instance_id") != context.identity.process_instance_id:
        return False
    if decision.get("attempt_id") != context.identity.attempt_id:
        return False
    expected = {
        "process_instance_id": context.identity.process_instance_id,
        "attempt_id": context.identity.attempt_id,
        "decision_id": decision_id,
    }
    return _has_minimum_correlated_receipt(refs, expected=expected)


def _has_minimum_correlated_receipt(
    refs: Sequence[Mapping[str, Any]],
    *,
    expected: Mapping[str, str],
) -> bool:
    found_decision_readback = False
    found_effect_receipt = False
    correlation_fields = tuple(expected)
    for ref in refs:
        present_fields = [field for field in correlation_fields if ref.get(field) is not None]
        if present_fields and any(ref.get(field) != expected[field] for field in correlation_fields):
            return False
        if not all(ref.get(field) == expected[field] for field in correlation_fields):
            continue
        ref_type = str(ref.get("type") or "")
        if ref_type == "decision_readback":
            found_decision_readback = found_decision_readback or ref.get("status") == "verified"
        elif ref_type in {"schedule_readback", "plan_readback"}:
            found_effect_receipt = found_effect_receipt or ref.get("status") == "verified"
        elif ref_type == "broker_fill":
            found_effect_receipt = True
        elif ref_type == "execution_mode":
            found_effect_receipt = found_effect_receipt or (
                ref.get("mode") == "dry_run" and ref.get("status") == "not_applied"
            )
    return found_decision_readback and found_effect_receipt


__all__ = [
    "ClosedProcessInstanceError",
    "OpenProcessVersionMismatch",
    "ProcessAttemptContext",
    "ProcessLifecycle",
]
