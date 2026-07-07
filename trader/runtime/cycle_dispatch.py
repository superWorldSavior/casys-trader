"""Runtime dispatch wrapper for daemon run_cycle calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable


RunCycleFn = Callable[..., dict]


@dataclass(frozen=True)
class RunCycleRuntimeContext:
    dry_run: bool
    sched: object
    data_source: object
    default_wake_minutes: float
    min_wake_minutes: float | None
    max_wake_minutes: float | None
    max_context_requests_per_symbol: int
    max_indicators_per_request: int
    max_model_calls_per_cycle: int
    indicator_triggers: list
    learning_consolidation_threshold: int
    consolidator_acpx_bin: str
    consolidator_acpx_agent: str
    consolidator_model: str
    consolidator_timeout_s: int
    decision_timeout_s: int
    decision_batch_size: int
    decision_batch_parallelism: int
    commission_model: object
    agent_tools_enabled: bool
    queue_decide_enabled: bool
    task_ledger: object | None
    queue_execute_enabled: bool
    execute_ledger: object | None
    plan_snapshot: object | None = None
    wake_reasons: list | None = None


def dispatch_run_cycle(
    *,
    run_cycle_fn: RunCycleFn,
    context: RunCycleRuntimeContext,
    now: datetime,
    symbols_filter: list[str],
) -> dict:
    return run_cycle_fn(
        dry_run=context.dry_run,
        now=now,
        symbols_filter=symbols_filter,
        sched=context.sched,
        data_source=context.data_source,
        default_wake_minutes=context.default_wake_minutes,
        min_wake_minutes=context.min_wake_minutes,
        max_wake_minutes=context.max_wake_minutes,
        max_context_requests_per_symbol=context.max_context_requests_per_symbol,
        max_indicators_per_request=context.max_indicators_per_request,
        max_model_calls_per_cycle=context.max_model_calls_per_cycle,
        indicator_triggers=context.indicator_triggers,
        wake_reasons=context.wake_reasons or [],
        learning_consolidation_threshold=context.learning_consolidation_threshold,
        consolidator_acpx_bin=context.consolidator_acpx_bin,
        consolidator_acpx_agent=context.consolidator_acpx_agent,
        consolidator_model=context.consolidator_model,
        consolidator_timeout_s=context.consolidator_timeout_s,
        decision_timeout_s=context.decision_timeout_s,
        decision_batch_size=context.decision_batch_size,
        decision_batch_parallelism=context.decision_batch_parallelism,
        commission_model=context.commission_model,
        agent_tools_enabled=context.agent_tools_enabled,
        queue_decide_enabled=context.queue_decide_enabled,
        task_ledger=context.task_ledger,
        queue_execute_enabled=context.queue_execute_enabled,
        execute_ledger=context.execute_ledger,
        plan_snapshot=context.plan_snapshot,
    )
