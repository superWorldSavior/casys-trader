from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trader.agent.protocol.types import Decision
from trader.infrastructure.files.decision_ledger import DecisionLedgerStore
from trader.infrastructure.llm.acpx_backend import AcpxBackend
from trader.infrastructure.llm.openai_backend import OpenAICompatibleBackend
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


def test_missing_price_is_recorded_as_stale_decision(
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

    assert len(report["decisions"]) == 1
    decision = report["decisions"][0]
    assert decision["symbol"] == "SPY"
    assert decision["action"] == "HOLD"
    assert decision["reason"] == "stale_market_data"
    assert decision["stale_reason"] == "no_data"
    assert decision["last_bar_ts"] is None
    assert decision["data_age_minutes"] is None
    assert pilot.deferred == []
    assert len(pilot.finished) == 1
    assert pilot.finished[0][0] == "SPY"
    assert pilot.finished[0][1]["decision_readback"]["status"] == "verified"


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
    experiment_runtime_identity = {
        "code_version": {"git_commit": "a" * 40, "git_tracked_dirty": False},
        "model_preset": "codex-luna-medium",
        "model_profiles": {},
    }
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
        assert kwargs["experiment_runtime_identity"] is experiment_runtime_identity
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
    monkeypatch.setattr(
        daemon,
        "_capture_experiment_runtime_identity",
        lambda: calls.append("experiment_identity") or experiment_runtime_identity,
    )
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(daemon, "connect_ib", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon.market_rotation_runtime, "tick_market_rotation", lambda **_kwargs: None)

    daemon.main(["--once"], now_fn=monkeypatch_now, sleep_fn=lambda _seconds: None)

    assert calls.count("pilot") == 1
    assert calls.count("experiment_identity") == 1
    assert calls.index("pid") < calls.index("bootstrap") < calls.index("runtime_id")
    assert calls.index("runtime_id") < calls.index("pilot") < calls.index("experiment_identity")
    assert calls.index("experiment_identity") < calls.index("cycle")


def _patch_claimed_boot(monkeypatch, tmp_path, calls: list[str]) -> None:
    from trader.runtime import pid_file

    state_dir = tmp_path / "state"

    def claim_pid_file(*, pid_file, pid) -> bool:
        assert pid_file == state_dir / "daemon.pid"
        assert isinstance(pid, int)
        calls.append("claim")
        return True

    def release_pid_file(*, pid_file, pid) -> None:
        assert pid_file == state_dir / "daemon.pid"
        assert isinstance(pid, int)
        calls.append("release")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.llm, "load_dotenv", lambda: None)
    monkeypatch.setattr(daemon.news_feed, "set_default_news_archive", lambda _path: None)
    monkeypatch.setattr(daemon.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pid_file, "claim_pid_file", claim_pid_file)
    monkeypatch.setattr(pid_file, "release_pid_file", release_pid_file)


def test_main_releases_claimed_pid_when_runtime_bootstrap_raises(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    _patch_claimed_boot(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        daemon.daemon_bootstrap,
        "bootstrap_runtime_state",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bootstrap failed")),
    )

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        daemon.main(["--once"])

    assert calls == ["claim", "release"]


def test_main_installs_sigterm_handler_before_pid_claim(
    monkeypatch,
    tmp_path,
) -> None:
    from trader.runtime import pid_file

    calls: list[str] = []
    installed: dict[str, object] = {}
    _patch_claimed_boot(monkeypatch, tmp_path, calls)

    def install_handler(signum, handler) -> None:
        assert signum == daemon.signal.SIGTERM
        calls.append("install_handler")
        installed["handler"] = handler

    def interrupted_claim(*, pid_file, pid) -> bool:
        del pid_file, pid
        calls.append("claim_published")
        handler = installed["handler"]
        assert callable(handler)
        handler(daemon.signal.SIGTERM, None)
        raise AssertionError("le handler SIGTERM doit interrompre le claim")

    monkeypatch.setattr(daemon.signal, "signal", install_handler)
    monkeypatch.setattr(pid_file, "claim_pid_file", interrupted_claim)

    with pytest.raises(SystemExit) as exc:
        daemon.main(["--once"])

    assert exc.value.code == 143
    assert calls == ["install_handler", "claim_published", "release"]


def test_main_releases_claimed_pid_when_experiment_capture_raises(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    _patch_claimed_boot(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        daemon.daemon_bootstrap,
        "bootstrap_runtime_state",
        lambda **_kwargs: SimpleNamespace(scheduler=object()),
    )
    monkeypatch.setattr(daemon, "build_process_pilot", lambda **_kwargs: object())
    monkeypatch.setattr(daemon, "_hydrate_last_llm_at", lambda _state: None)
    monkeypatch.setattr(
        daemon,
        "_capture_experiment_runtime_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("identity failed")),
    )

    with pytest.raises(RuntimeError, match="identity failed"):
        daemon.main(["--once"])

    assert calls == ["claim", "release"]


def _patch_boot_through_learning_runner(
    monkeypatch,
    calls: list[str],
) -> object:
    class Runner:
        def stop(self) -> None:
            calls.append("learning_stopped")

    runner = Runner()
    monkeypatch.setenv("CASYS_QUEUE_DECIDE_ENABLED", "1")
    monkeypatch.setenv("CASYS_QUEUE_EXECUTE_ENABLED", "1")
    monkeypatch.setattr(
        daemon.daemon_bootstrap,
        "bootstrap_runtime_state",
        lambda **_kwargs: SimpleNamespace(scheduler=object()),
    )
    monkeypatch.setattr(daemon, "build_process_pilot", lambda **_kwargs: object())
    monkeypatch.setattr(daemon, "_hydrate_last_llm_at", lambda _state: None)
    monkeypatch.setattr(
        daemon,
        "_capture_experiment_runtime_identity",
        lambda: {
            "code_version": {},
            "model_preset": None,
            "model_profiles": {},
        },
    )
    monkeypatch.setattr(
        daemon.learnings_sync_runtime,
        "LearningSyncRunner",
        lambda **_kwargs: runner,
    )
    monkeypatch.setattr(
        daemon.queue_runtime,
        "build_decide_tool_services",
        lambda **_kwargs: object(),
    )
    return runner


def test_main_stops_registered_boot_resources_when_queue_start_raises(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    _patch_claimed_boot(monkeypatch, tmp_path, calls)
    _patch_boot_through_learning_runner(monkeypatch, calls)
    monkeypatch.setattr(
        daemon.queue_runtime,
        "start_queue_runtimes",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("queue failed")),
    )

    with pytest.raises(RuntimeError, match="queue failed"):
        daemon.main(["--once"])

    assert calls == ["claim", "learning_stopped", "release"]


def test_main_stops_started_queue_pools_when_later_boot_step_raises(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    _patch_claimed_boot(monkeypatch, tmp_path, calls)
    _patch_boot_through_learning_runner(monkeypatch, calls)

    class Pool:
        def __init__(self, label: str) -> None:
            self.label = label

        def stop(self) -> None:
            calls.append(f"{self.label}_stopped")

    decide_pool = Pool("decide")
    execute_pool = Pool("execute")
    queue_runtimes = SimpleNamespace(
        decide=SimpleNamespace(enabled=True, ledger=object(), pool=decide_pool),
        execute=SimpleNamespace(enabled=True, ledger=object(), pool=execute_pool),
    )
    monkeypatch.setattr(
        daemon.queue_runtime,
        "start_queue_runtimes",
        lambda **_kwargs: queue_runtimes,
    )
    monkeypatch.setattr(
        daemon.news_macro_runtime,
        "NewsMacroAnalysisRunner",
        lambda: (_ for _ in ()).throw(RuntimeError("post-queue failed")),
    )

    with pytest.raises(RuntimeError, match="post-queue failed"):
        daemon.main(["--once"])

    assert calls == [
        "claim",
        "learning_stopped",
        "decide_stopped",
        "execute_stopped",
        "release",
    ]


def test_runtime_experiment_identity_detects_disk_drift_without_adopting_it(
    monkeypatch,
) -> None:
    captured_profiles = {
        "acpx": {
            "configured_model": "gpt-5.6-luna",
            "transport": "acpx",
            "agent": "codex",
            "reasoning_effort": "medium",
            "profile_fingerprint": "sha256:" + "b" * 64,
        }
    }
    captured = {
        "code_version": {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
        "model_preset": "codex-luna-medium",
        "model_profiles": captured_profiles,
    }
    current_code = {
        "git_commit": "b" * 40,
        "git_tracked_dirty": False,
    }
    monkeypatch.setattr(
        daemon.code_version,
        "current_code_version",
        lambda _root: dict(current_code),
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "active_model_preset",
        lambda _path: "codex-luna-high",
    )
    monkeypatch.setattr(
        daemon.llm,
        "build_default_router_from_env",
        lambda **_kwargs: type("Router", (), {"backends": []})(),
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "model_profiles_from_backends",
        lambda *_args, **_kwargs: {
            **captured_profiles,
            "acpx": {
                **captured_profiles["acpx"],
                "reasoning_effort": "high",
            },
        },
    )

    issues = daemon._experiment_runtime_identity_issues(captured)

    assert "runtime.git_commit:changed_since_boot" in issues
    assert "runtime.model_preset:changed_since_boot" in issues
    assert "runtime.model_profiles:changed_or_unverifiable_since_boot" in issues
    assert captured["code_version"]["git_commit"] == "a" * 40
    assert captured["model_preset"] == "codex-luna-medium"


def test_runtime_experiment_identity_detects_openai_endpoint_drift(
    monkeypatch,
    tmp_path,
) -> None:
    captured_backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        model="nemotron",
        api_key="secret-one",
        base_url="https://api.one.example/v1?token=secret-one",
    )
    current_backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        model="nemotron",
        api_key="secret-two",
        base_url="https://api.two.example/v1?token=secret-two",
    )
    captured = {
        "code_version": {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
        "model_preset": "ollama-nemotron",
        "model_profiles": daemon.experiment_metadata.model_profiles_from_backends(
            [captured_backend],
            repo_root=tmp_path,
        ),
    }
    monkeypatch.setattr(
        daemon.code_version,
        "current_code_version",
        lambda _root: {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "active_model_preset",
        lambda _path: "ollama-nemotron",
    )
    monkeypatch.setattr(
        daemon.llm,
        "build_default_router_from_env",
        lambda **_kwargs: type("Router", (), {"backends": [current_backend]})(),
    )

    issues = daemon._experiment_runtime_identity_issues(captured)

    assert issues == ["runtime.model_profiles:changed_or_unverifiable_since_boot"]


def test_runtime_experiment_identity_fails_closed_when_profile_refresh_raises(
    monkeypatch,
) -> None:
    captured = {
        "code_version": {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
        "model_preset": "codex-luna-medium",
        "model_profiles": {"acpx": {}},
    }
    monkeypatch.setattr(
        daemon.code_version,
        "current_code_version",
        lambda _root: {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "active_model_preset",
        lambda _path: "codex-luna-medium",
    )

    def raise_invalid_config(**_kwargs):
        raise ValueError("malformed runtime ACPX config")

    monkeypatch.setattr(
        daemon.llm,
        "build_default_router_from_env",
        raise_invalid_config,
    )

    issues = daemon._experiment_runtime_identity_issues(captured)

    assert issues == ["runtime.model_profiles:changed_or_unverifiable_since_boot"]


def test_runtime_experiment_identity_detects_acpx_runtime_chunk_drift(
    monkeypatch,
    tmp_path,
) -> None:
    package_root = tmp_path / "acpx-package"
    dist_dir = package_root / "dist"
    dist_dir.mkdir(parents=True)
    acpx_bin = dist_dir / "cli.js"
    acpx_bin.write_text(
        "#!/usr/bin/env node\nimport './runtime.js';\n",
        encoding="utf-8",
    )
    acpx_bin.chmod(0o755)
    runtime_chunk = dist_dir / "runtime.js"
    runtime_chunk.write_text("export const marker = 'v1';\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        '{"name":"acpx","version":"1.0.0",'
        '"bin":{"acpx":"dist/cli.js"},"dependencies":{}}',
        encoding="utf-8",
    )
    agent_bin = tmp_path / "claude-agent"
    agent_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_bin.chmod(0o755)
    home = tmp_path / "home"
    config_dir = home / ".acpx"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": {"argv": [str(agent_bin)]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="sonnet",
        agent="claude",
        acpx_bin=str(acpx_bin),
    )
    captured = {
        "code_version": {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
        "model_preset": "claude-sonnet",
        "model_profiles": daemon.experiment_metadata.model_profiles_from_backends(
            [backend],
            repo_root=tmp_path,
        ),
    }
    runtime_chunk.write_text("export const marker = 'v2';\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(
        daemon.code_version,
        "current_code_version",
        lambda _root: {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "active_model_preset",
        lambda _path: "claude-sonnet",
    )
    monkeypatch.setattr(
        daemon.llm,
        "build_default_router_from_env",
        lambda **_kwargs: type("Router", (), {"backends": [backend]})(),
    )

    issues = daemon._experiment_runtime_identity_issues(captured)

    assert issues == ["runtime.model_profiles:changed_or_unverifiable_since_boot"]


def test_runtime_experiment_identity_detects_acpx_adapter_drift(
    monkeypatch,
    tmp_path,
) -> None:
    acpx_bin = tmp_path / "acpx"
    acpx_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    acpx_bin.chmod(0o755)
    agent_bin = tmp_path / "codex-agent"
    agent_bin.write_text("#!/bin/sh\n# adapter-v1\nexit 0\n", encoding="utf-8")
    agent_bin.chmod(0o755)
    home = tmp_path / "home"
    config_dir = home / ".acpx"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"codex": {"argv": [str(agent_bin)]}}}),
        encoding="utf-8",
    )
    profile_home = tmp_path / "codex-home"
    profile_home.mkdir()
    (profile_home / "config.toml").write_text(
        'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "medium"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    backend = AcpxBackend(
        provider="acpx",
        model="gpt-5.6-luna",
        agent="codex",
        codex_home=str(profile_home),
        acpx_bin=str(acpx_bin),
    )
    captured = {
        "code_version": {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
        "model_preset": "codex-luna-medium",
        "model_profiles": daemon.experiment_metadata.model_profiles_from_backends(
            [backend],
            repo_root=tmp_path,
        ),
    }
    agent_bin.write_text("#!/bin/sh\n# adapter-v2\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(
        daemon.code_version,
        "current_code_version",
        lambda _root: {
            "git_commit": "a" * 40,
            "git_tracked_dirty": False,
        },
    )
    monkeypatch.setattr(
        daemon.experiment_metadata,
        "active_model_preset",
        lambda _path: "codex-luna-medium",
    )
    monkeypatch.setattr(
        daemon.llm,
        "build_default_router_from_env",
        lambda **_kwargs: type("Router", (), {"backends": [backend]})(),
    )

    issues = daemon._experiment_runtime_identity_issues(captured)

    assert issues == ["runtime.model_profiles:changed_or_unverifiable_since_boot"]
