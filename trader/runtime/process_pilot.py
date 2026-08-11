"""Runtime adapter for the Casys Trader APE process evidence pilot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.runtime.process_lifecycle import (
    ProcessAttemptContext,
    ProcessLifecycle,
)
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.migrations import (
    BROKER_MIGRATION,
    PROCESS_TRACE_MIGRATION,
)
from trader.infrastructure.state_db.process_event_store import ProcessEventStore
from trader.support.metadata.governance_version import current_governance_version


class ProcessPilotBindingError(RuntimeError):
    """The runtime cannot consume a governance contract with a false binding status."""


class ProcessPilot:
    """Correlate one daemon run with governed per-symbol process instances."""

    def __init__(
        self,
        *,
        lifecycle: ProcessLifecycle,
        governance_ref: Mapping[str, Any],
    ) -> None:
        self._lifecycle = lifecycle
        self.governance_ref = dict(governance_ref)
        self._attempts: dict[str, ProcessAttemptContext] = {}

    def admit_many(
        self,
        symbols: Sequence[str],
        *,
        causes_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        self._attempts = {
            symbol: self._lifecycle.admit_or_resume(
                symbol,
                caused_by=causes_by_symbol.get(symbol, ()),
            )
            for symbol in symbols
        }

    def recovery_symbols(self) -> set[str]:
        return {symbol for symbol, attempt in self._attempts.items() if attempt.recovery_required}

    def decision_fields(self, symbol: str) -> dict[str, Any]:
        return self._attempts[symbol].decision_fields()

    def finish_decision(self, symbol: str, decision: Mapping[str, Any]) -> None:
        self._lifecycle.finish_from_decision(
            self._attempts[symbol],
            decision,
            closure_evidence_complete=closure_evidence_complete(decision),
        )

    def defer(
        self,
        symbol: str,
        *,
        outcome_code: str,
        effect_status: str = "not_applied",
        effect_refs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._lifecycle.defer(
            self._attempts[symbol],
            outcome_code=outcome_code,
            effect_status=effect_status,
            effect_refs=effect_refs,
        )

    def defer_recovery(self, symbol: str) -> None:
        """Finish the recovery attempt without losing its unresolved causal refs."""
        attempt = self._attempts[symbol]
        if not attempt.recovery_required:
            raise ValueError(f"{symbol} is not in recovery")
        self._lifecycle.defer(
            attempt,
            outcome_code="recovery_required",
            effect_status="unknown",
            effect_refs=attempt.prior_effect_refs,
        )


def build_process_pilot(
    *,
    repo_root: str | Path,
    state_dir: str | Path,
    runtime_run_id: str,
    clock: Callable[[], datetime],
) -> ProcessPilot:
    governance_ref = current_governance_version(repo_root)
    if governance_ref.get("runtime_binding_status") != "integrated":
        raise ProcessPilotBindingError(
            "process governance must declare runtime_binding.status=integrated before runtime consumption"
        )
    db = open_state_db(Path(state_dir) / "casys.db")
    db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])
    lifecycle = ProcessLifecycle(
        store=ProcessEventStore(db),
        process_type=str(governance_ref["process_id"]),
        process_version=str(governance_ref["process_version"]),
        runtime_run_id=runtime_run_id,
        version_pins=governance_ref,
        clock=clock,
    )
    return ProcessPilot(lifecycle=lifecycle, governance_ref=governance_ref)


def closure_evidence_complete(decision: Mapping[str, Any]) -> bool:
    """Evaluate the explicit minimum receipt required by the pilot Charter."""
    if not _decision_readback_is_exact(decision):
        return False
    schedule = decision.get("schedule_effect")
    if not isinstance(schedule, Mapping) or schedule.get("status") != "verified":
        return False
    plan_effects = decision.get("plan_effects")
    if isinstance(plan_effects, list) and any(
        not isinstance(effect, Mapping) or effect.get("status") != "verified" for effect in plan_effects
    ):
        return False
    if decision.get("process_state") == "recovery_required":
        return False
    if decision.get("effect_status") == "unknown":
        return False

    admission_status = decision.get("admission_status")
    if decision.get("execution_mode") == "dry_run":
        return (
            admission_status in {"admitted", "not_required", "rejected"}
            and decision.get("effect_status") == "not_applied_dry_run"
        )

    if decision.get("executed") is True:
        if admission_status != "admitted" or decision.get("effect_status") != "verified":
            return False
        refs = decision.get("effect_refs")
        fill_ref = refs.get("broker_fill") if isinstance(refs, Mapping) else None
        if not isinstance(fill_ref, Mapping):
            return False
        if any(
            fill_ref.get(field) != decision.get(field)
            for field in ("symbol", "process_instance_id", "attempt_id", "decision_id")
        ):
            return False
        if _plan_readback_required(decision):
            plan_effect = decision.get("plan_effect")
            if not isinstance(plan_effect, Mapping) or plan_effect.get("status") != "verified":
                return False
            if decision.get("trade_plan_created"):
                expected_plan = decision.get("trade_plan")
                if not isinstance(expected_plan, Mapping):
                    return False
                expected_mutation = plan_effect.get("expected_mutation")
                if (
                    not isinstance(expected_mutation, Mapping)
                    or expected_mutation.get("kind") != "upsert"
                    or expected_mutation.get("plan") != expected_plan
                ):
                    return False
                if plan_effect.get("expected_plan") != expected_plan:
                    return False
                if plan_effect.get("observed_plan") != expected_plan:
                    return False
        return True

    if admission_status == "admitted":
        # Once execution was admitted, a non-effect is a failure/recovery case,
        # never a successful completion inferred from executed=False.
        return False
    if admission_status not in {"not_required", "rejected"}:
        return False
    if decision.get("exit_update") or decision.get("plan_review_recorded"):
        plan_effect = decision.get("plan_effect")
        return isinstance(plan_effect, Mapping) and plan_effect.get("status") == "verified"
    return True


def _decision_readback_is_exact(decision: Mapping[str, Any]) -> bool:
    """Require a durable ledger readback, not an in-memory decision identifier."""
    readback = decision.get("decision_readback")
    if not isinstance(readback, Mapping) or readback.get("status") != "verified":
        return False
    return all(
        readback.get(field) == decision.get(field) and decision.get(field) not in {None, ""}
        for field in ("symbol", "process_instance_id", "attempt_id", "decision_id")
    )


def _plan_readback_required(decision: Mapping[str, Any]) -> bool:
    return bool(
        decision.get("trade_plan_created")
        or decision.get("exit_update")
        or decision.get("intent") in {"CLOSE", "REDUCE", "FLIP", "SCALE_OUT"}
    )


__all__ = [
    "ProcessPilot",
    "ProcessPilotBindingError",
    "build_process_pilot",
    "closure_evidence_complete",
]
