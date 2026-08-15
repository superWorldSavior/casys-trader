from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from trader.agent.protocol.types import Decision
from trader.infrastructure.files.decision_ledger import DecisionLedgerStore
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler
from trader.runtime import daemon
from trader.runtime.cycle_process_state import CycleProcessState
from tests.conftest import write_runtime_config as _write_runtime_config


class _FreshDataSource:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        del symbol, lookback
        ts = self._now.date().isoformat() if interval == "1d" else self._now.isoformat()
        return [Bar(ts=ts, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0)]


class _StaleDataSource:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        del symbol, lookback
        ts = (
            (self._now - timedelta(days=10)).date().isoformat()
            if interval == "1d"
            else (self._now - timedelta(minutes=90)).isoformat()
        )
        return [Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)]


class _EmptyDataSource:
    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        del symbol, lookback, interval
        return []


class _FakeProcessPilot:
    governance_ref = {"process_id": "pilot", "bundle_sha256": "bundle"}

    def __init__(self, *, recovery_symbols: set[str] | None = None) -> None:
        self._recovery_symbols = recovery_symbols or set()
        self.admissions: list[tuple[list[str], dict[str, list[dict]]]] = []
        self.finished: list[tuple[str, dict]] = []
        self.deferred: list[tuple[str, dict]] = []
        self.recovery_deferred: list[str] = []

    def admit_many(self, symbols, *, causes_by_symbol) -> None:
        self.admissions.append((list(symbols), deepcopy(dict(causes_by_symbol))))

    def recovery_symbols(self) -> set[str]:
        return set(self._recovery_symbols)

    def defer_recovery(self, symbol: str) -> None:
        self.recovery_deferred.append(symbol)

    def decision_fields(self, symbol: str) -> dict:
        return {
            "process_instance_id": f"instance-{symbol}",
            "attempt_id": f"attempt-{symbol}",
            "runtime_run_id": "runtime-1",
            "governance_version": dict(self.governance_ref),
        }

    def finish_decision(self, symbol: str, decision: dict) -> None:
        self.finished.append((symbol, deepcopy(decision)))

    def defer(self, symbol: str, **kwargs) -> None:
        self.deferred.append((symbol, deepcopy(kwargs)))


def test_decision_readback_rejects_wrong_runtime_identity(tmp_path) -> None:
    store = DecisionLedgerStore(tmp_path / "decisions.jsonl")
    store.append(
        {
            "decision_id": "decision-1",
            "symbol": "SPY",
            "process": {
                "process_instance_id": "instance-SPY",
                "attempt_id": "attempt-SPY",
                "runtime_run_id": "wrong-runtime",
                "decision_id": "decision-1",
            },
        }
    )

    receipt = daemon._decision_readback(
        decision_ledger_store=store,
        decision_entry={
            "symbol": "SPY",
            "decision_id": "decision-1",
            "process_instance_id": "instance-SPY",
            "attempt_id": "attempt-SPY",
            "runtime_run_id": "runtime-1",
        },
    )

    assert receipt["status"] == "mismatch"
    assert receipt["observed"]["runtime_run_id"] == "wrong-runtime"


def test_recovery_metadata_never_removes_symbols_from_dispatch() -> None:
    pilot = _FakeProcessPilot(recovery_symbols={"QQQ"})

    dispatchable = daemon._admit_governed_symbols(
        process_pilot=pilot,
        symbols=["SPY", "QQQ"],
        triggers_by_symbol={"SPY": [{"watch_id": "watch-1", "order": {"secret": True}}]},
        wake_reasons_by_symbol={},
    )

    assert dispatchable == ["SPY", "QQQ"]
    assert pilot.recovery_deferred == []
    assert pilot.admissions[0][1] == {
        "SPY": [{"type": "indicator_trigger", "watch_id": "watch-1"}],
        "QQQ": [{"type": "scheduled_due"}],
    }


def test_quiet_hold_gets_exact_schedule_and_ledger_readbacks(
    monkeypatch,
    tmp_path,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    process_state = CycleProcessState(last_llm_at={(str(state_dir), "SPY"): now})
    pilot = _FakeProcessPilot()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=_FreshDataSource(now),
        process_state=process_state,
        process_pilot=pilot,
    )

    assert report["decisions"][0]["reason"] == "quiet_gate"
    assert len(pilot.finished) == 1
    decision = pilot.finished[0][1]
    assert decision["schedule_effect"]["status"] == "verified"
    assert decision["decision_readback"] == {
        "status": "verified",
        "symbol": "SPY",
        "decision_id": decision["decision_id"],
        "process_decision_id": decision["decision_id"],
        "process_instance_id": "instance-SPY",
        "attempt_id": "attempt-SPY",
        "runtime_run_id": "runtime-1",
    }
    assert decision["admission_status"] == "not_required"
    assert decision["effect_status"] == "not_applied"


def test_stale_backoff_without_decision_is_explicitly_deferred(
    monkeypatch,
    tmp_path,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 2)
    pilot = _FakeProcessPilot()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=_StaleDataSource(now),
        process_pilot=pilot,
    )

    assert report["decisions"] == []
    assert pilot.finished == []
    assert pilot.deferred == [
        (
            "SPY",
            {
                "outcome_code": "stale_backoff",
                "effect_status": "verified",
                "effect_refs": (
                    {
                        "type": "schedule_readback",
                        "symbol": "SPY",
                        "status": "verified",
                        "next_wake": sched.next_wake("SPY").isoformat(),
                        "active_watch_ids": [],
                    },
                ),
            },
        )
    ]


def test_missing_price_without_decision_is_explicitly_deferred(
    monkeypatch,
    tmp_path,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    pilot = _FakeProcessPilot()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=_EmptyDataSource(),
        process_pilot=pilot,
    )

    assert report["decisions"] == []
    assert pilot.deferred == [("SPY", {"outcome_code": "no_price"})]


def test_queue_undecided_symbol_is_explicitly_deferred(
    monkeypatch,
    tmp_path,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    pilot = _FakeProcessPilot()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    def dispatch_decisions(request, **_kwargs):
        return daemon.decision_dispatch_runtime.DecisionDispatchResult(
            decisions_by_symbol={},
            model_calls_used=0,
            undecided_symbols={"SPY"},
            streamed_decision_symbols=set(),
            execution_state=request.execution_state,
        )

    monkeypatch.setattr(daemon.decision_dispatch_runtime, "dispatch_decisions", dispatch_decisions)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=_FreshDataSource(now),
        queue_decide_enabled=True,
        task_ledger=object(),
        process_pilot=pilot,
    )

    assert report["decisions"] == []
    assert pilot.deferred == [("SPY", {"outcome_code": "queue_decide_deferred"})]


def test_queue_process_correlation_is_observational_and_reaches_decision_ledger(
    monkeypatch,
    tmp_path,
) -> None:
    """Le lien admission -> retry -> résultat -> décision est explicite et passif."""
    import json

    from trader.application.decide import handler as decide_handler_module
    from trader.application.decide.handler import make_decide_handler
    from trader.application.queue.contracts import RetryableError
    from trader.queue.ledger import TaskLedger
    from trader.queue.pools import ResourcePools
    from trader.queue.worker import Worker

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    pilot = _FakeProcessPilot()
    expected_process = {
        "process_instance_id": "instance-SPY",
        "attempt_id": "attempt-SPY",
        "runtime_run_id": "runtime-1",
    }
    queue_ledger = TaskLedger(tmp_path / "task_ledger.db")
    task_id = queue_ledger.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=1_000,
        now_ms=1_000,
        dedup_key=f"{now.isoformat()}:SPY",
        partition_key="SPY",
        resource="acpx",
        payload=json.dumps(
            {
                "symbol": "SPY",
                "mandate": "m",
                "memory": "m",
                "shared_context": {},
                "per_symbol_facts": {},
                "decision_timeout_s": 60,
                "agent_tools_enabled": False,
                "cycle_id": now.isoformat(),
                "symbols_universe": ["SPY"],
                "process": expected_process,
            }
        ),
    )
    assert task_id is not None
    outcomes = [RetryableError("transient"), (Decision.hold("SPY", "queue_process_trace"), 1)]

    def flaky_decide_one(**_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(decide_handler_module, "decide_one", flaky_decide_one)
    clock = [1.0]
    worker = Worker(
        queue_ledger,
        ResourcePools({"acpx": 1}),
        {"decide": make_decide_handler(codex_client=object())},
        worker_id="queue-pilot-test",
        now_fn=lambda: clock[0],
    )
    assert worker.run_once(now_ms=1_000, token="first") is True
    retried = queue_ledger.get(task_id)
    assert retried is not None
    assert retried["status"] == "pending"
    assert retried["attempts"] == 1
    assert json.loads(retried["payload"])["process"] == expected_process
    clock[0] = 2.0
    assert worker.run_once(now_ms=int(retried["scheduled_at"]), token="second") is True
    completed = queue_ledger.get(task_id)
    assert completed is not None
    assert completed["status"] == "done"
    assert completed["attempts"] == 2
    assert json.loads(completed["result"])["process"] == expected_process

    captured: dict[str, object] = {}
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    original_queue_results_iterator = daemon.decision_dispatch_runtime.queue_dispatch.iter_decide_results_via_queue

    def queue_results_iterator(**kwargs):
        captured["decidable"] = list(kwargs["decidable"])
        captured["symbols_universe"] = list(kwargs["symbols_universe"])
        captured["process_identity_by_symbol"] = deepcopy(kwargs["process_identity_by_symbol"])
        yield from original_queue_results_iterator(**kwargs)

    monkeypatch.setattr(
        daemon.decision_dispatch_runtime.queue_dispatch,
        "iter_decide_results_via_queue",
        queue_results_iterator,
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=_FreshDataSource(now),
        queue_decide_enabled=True,
        task_ledger=queue_ledger,
        process_pilot=pilot,
    )

    assert captured == {
        "decidable": ["SPY"],
        "symbols_universe": ["SPY"],
        "process_identity_by_symbol": {"SPY": expected_process},
    }
    assert [decision["symbol"] for decision in report["decisions"]] == ["SPY"]
    assert report["decisions"][0]["action"] == "HOLD"

    decision_id = report["decisions"][0]["decision_id"]
    durable = DecisionLedgerStore(state_dir / "decisions.jsonl").read_by_decision_id(decision_id)
    assert durable is not None
    assert {field: durable["process"][field] for field in expected_process} == expected_process
    assert durable["process"]["decision_id"] == decision_id
    assert durable["process"]["governance_version"] == pilot.governance_ref


def test_main_builds_one_default_pilot_after_pid_and_bootstrap(
    monkeypatch,
    tmp_path,
) -> None:
    from trader.runtime import pid_file

    _write_runtime_config(tmp_path)
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    state_dir = tmp_path / "state"
    calls: list[str] = []
    pilot = object()
    original_bootstrap = daemon.daemon_bootstrap.bootstrap_runtime_state

    def claim_pid_file(**_kwargs) -> bool:
        calls.append("pid")
        return True

    def bootstrap_runtime_state(**kwargs):
        calls.append("bootstrap")
        return original_bootstrap(**kwargs)

    def runtime_run_id() -> str:
        calls.append("runtime_id")
        return "runtime-unique"

    def build_default_pilot(**kwargs):
        calls.append("pilot")
        assert kwargs == {
            "repo_root": daemon._SOURCE_ROOT,
            "state_dir": state_dir,
            "runtime_run_id": "runtime-unique",
            "clock": monkeypatch_now,
        }
        return pilot

    def run_cycle(**kwargs):
        calls.append("cycle")
        assert kwargs["process_pilot"] is pilot
        return {
            "ts": now.isoformat(),
            "dry_run": True,
            "symbols_due": ["SPY"],
            "planned_exits": [],
            "decisions": [],
            "portfolio": {"equity": 100000.0, "cash": 100000.0},
            "prices": {},
        }

    def monkeypatch_now() -> datetime:
        return now

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setenv("CASYS_QUEUE_DECIDE_ENABLED", "0")
    monkeypatch.setenv("CASYS_QUEUE_EXECUTE_ENABLED", "0")
    monkeypatch.setattr(pid_file, "claim_pid_file", claim_pid_file)
    monkeypatch.setattr(pid_file, "release_pid_file", lambda **_kwargs: None)
    monkeypatch.setattr(daemon.daemon_bootstrap, "bootstrap_runtime_state", bootstrap_runtime_state)
    monkeypatch.setattr(daemon, "new_runtime_run_id", runtime_run_id)
    monkeypatch.setattr(daemon, "build_process_pilot", build_default_pilot)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(daemon, "connect_ib", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon.market_rotation_runtime, "tick_market_rotation", lambda **_kwargs: None)

    daemon.main(["--once"], now_fn=monkeypatch_now, sleep_fn=lambda _seconds: None)

    assert calls.count("pilot") == 1
    assert calls.index("pid") < calls.index("bootstrap") < calls.index("runtime_id")
    assert calls.index("runtime_id") < calls.index("pilot") < calls.index("cycle")
