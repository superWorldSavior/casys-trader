from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trader.runtime import cycle_dispatch


@dataclass(frozen=True)
class FakeCommissionModel:
    name: str = "fake"


def test_dispatch_run_cycle_forwarde_le_contexte_runtime() -> None:
    now = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    calls: list[dict] = []
    commission_model = FakeCommissionModel()
    sched = object()
    data_source = object()
    task_ledger = object()
    execute_ledger = object()
    worker_cycle_context = object()

    def run_cycle_fn(**kwargs):
        calls.append(kwargs)
        return {"ts": now.isoformat(), "decisions": []}

    context = cycle_dispatch.RunCycleRuntimeContext(
        dry_run=True,
        sched=sched,
        data_source=data_source,
        default_wake_minutes=30,
        min_wake_minutes=5,
        max_wake_minutes=180,
        max_context_requests_per_symbol=2,
        max_indicators_per_request=4,
        max_model_calls_per_cycle=9,
        indicator_triggers=[{"symbol": "SPY"}],
        wake_reasons=[{"symbol": "SPY", "reason": "watch_expired"}],
        learning_consolidation_threshold=12,
        consolidator_acpx_bin="acpx",
        consolidator_acpx_agent="codex",
        consolidator_model="gpt-5",
        consolidator_timeout_s=60,
        decision_timeout_s=120,
        decision_batch_size=7,
        decision_batch_parallelism=3,
        commission_model=commission_model,
        agent_tools_enabled=True,
        queue_decide_enabled=True,
        task_ledger=task_ledger,
        queue_execute_enabled=True,
        execute_ledger=execute_ledger,
        worker_cycle_context=worker_cycle_context,
    )

    report = cycle_dispatch.dispatch_run_cycle(
        run_cycle_fn=run_cycle_fn,
        context=context,
        now=now,
        symbols_filter=["SPY", "QQQ"],
    )

    assert report == {"ts": now.isoformat(), "decisions": []}
    assert calls == [
        {
            "dry_run": True,
            "now": now,
            "symbols_filter": ["SPY", "QQQ"],
            "sched": sched,
            "data_source": data_source,
            "default_wake_minutes": 30,
            "min_wake_minutes": 5,
            "max_wake_minutes": 180,
            "max_context_requests_per_symbol": 2,
            "max_indicators_per_request": 4,
            "max_model_calls_per_cycle": 9,
            "indicator_triggers": [{"symbol": "SPY"}],
            "wake_reasons": [{"symbol": "SPY", "reason": "watch_expired"}],
            "learning_consolidation_threshold": 12,
            "consolidator_acpx_bin": "acpx",
            "consolidator_acpx_agent": "codex",
            "consolidator_model": "gpt-5",
            "consolidator_timeout_s": 60,
            "decision_timeout_s": 120,
            "decision_batch_size": 7,
            "decision_batch_parallelism": 3,
            "commission_model": commission_model,
            "agent_tools_enabled": True,
            "queue_decide_enabled": True,
            "task_ledger": task_ledger,
            "queue_execute_enabled": True,
            "execute_ledger": execute_ledger,
            "worker_cycle_context": worker_cycle_context,
        }
    ]
