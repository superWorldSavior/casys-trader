"""Snapshot derivation for the desktop live-work signal.

These helpers must never open the repository ``state/`` directory.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from trader.infrastructure.queue.ledger import TaskLedger
from trader.infrastructure.state_db.shadow import write_json_atomic

_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "desktop" / "bridge" / "snapshot.py"


def _load_snapshot_bridge():
    name = "desktop_bridge_snapshot_intelligence"
    spec = importlib.util.spec_from_file_location(name, _SNAPSHOT_PATH)
    assert spec is not None and spec.loader is not None
    module = sys.modules.get(name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _activity(state_dir: Path, **kwargs):
    snapshot = _load_snapshot_bridge()
    payload = {
        "daemon_alive": True,
        "daemon_pid": 4242,
        "daemon_phase": "idle_waiting_for_wake",
        "now_ms": 1_000_000,
    }
    payload.update(kwargs)
    return snapshot._intelligence_activity(state_dir, **payload)


def _write_status(path: Path, *, pid: int, status: str) -> None:
    write_json_atomic(
        path,
        {
            "pid": pid,
            "status": status,
            "started_at": "2026-08-23T10:00:00+00:00",
            "updated_at": "2026-08-23T10:00:01+00:00",
        },
    )


def _enqueue_company_task(
    ledger: TaskLedger,
    *,
    status: str,
    scheduled_at_ms: int,
    now_ms: int,
    dedup: str,
    kind: str = "company_micro",
) -> None:
    task_id = ledger.enqueue(
        kind=kind,
        priority=1,
        scheduled_at_ms=scheduled_at_ms,
        now_ms=now_ms,
        dedup_key=dedup,
        partition_key=dedup,
        resource="company-research",
        payload="{}",
    )
    assert task_id is not None
    if status != "pending":
        with sqlite3.connect(ledger.path) as connection:
            connection.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def test_intelligence_activity_is_idle_when_nothing_is_running(tmp_path: Path) -> None:
    activity = _activity(tmp_path)

    assert activity == {
        "context": {"active": False},
        "companies": {"active": False, "running_tasks": 0, "pending_tasks": 0},
        "decisions": {"active": False},
    }


def test_decisions_lane_is_active_only_for_exact_live_phases(tmp_path: Path) -> None:
    for phase in ("cycle_started", "deciding_batch", "decision_recorded"):
        activity = _activity(tmp_path, daemon_phase=phase)
        assert activity["decisions"]["active"] is True, phase

    for phase in ("cycle_completed", "deciding_symbol", "CYCLE_STARTED", None, "idle_waiting_for_wake"):
        activity = _activity(tmp_path, daemon_phase=phase)
        assert activity["decisions"]["active"] is False, phase


def test_decisions_lane_stays_idle_when_daemon_is_dead(tmp_path: Path) -> None:
    activity = _activity(tmp_path, daemon_alive=False, daemon_phase="deciding_batch")
    assert activity["decisions"]["active"] is False


def test_context_lane_requires_owned_running_news_macro_status(tmp_path: Path) -> None:
    snapshot = _load_snapshot_bridge()
    path = tmp_path / snapshot._NEWS_MACRO_RUNNER_STATUS
    _write_status(path, pid=4242, status="running")

    assert _activity(tmp_path)["context"]["active"] is True
    assert _activity(tmp_path, daemon_pid=99)["context"]["active"] is False
    assert _activity(tmp_path, daemon_alive=False)["context"]["active"] is False

    _write_status(path, pid=4242, status="idle")
    assert _activity(tmp_path)["context"]["active"] is False


def test_companies_lane_uses_owned_scan_status_before_queue_tasks(tmp_path: Path) -> None:
    snapshot = _load_snapshot_bridge()
    _write_status(tmp_path / snapshot._COMPANY_SCAN_STATUS, pid=4242, status="running")

    activity = _activity(tmp_path)
    assert activity["companies"]["active"] is True
    assert activity["companies"]["running_tasks"] == 0
    assert activity["companies"]["pending_tasks"] == 0
    assert _activity(tmp_path, daemon_pid=7)["companies"]["active"] is False
    assert _activity(tmp_path, daemon_alive=False)["companies"]["active"] is False


def test_companies_lane_counts_due_company_micro_tasks_only(tmp_path: Path) -> None:
    ledger = TaskLedger(tmp_path / "company_research_tasks.db")
    now_ms = 1_000_000
    _enqueue_company_task(ledger, status="running", scheduled_at_ms=now_ms, now_ms=now_ms, dedup="run")
    _enqueue_company_task(ledger, status="pending", scheduled_at_ms=now_ms, now_ms=now_ms, dedup="due")
    _enqueue_company_task(
        ledger,
        status="pending",
        scheduled_at_ms=now_ms + 60_000,
        now_ms=now_ms,
        dedup="future",
    )
    _enqueue_company_task(
        ledger,
        status="running",
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup="other-kind",
        kind="decide",
    )

    activity = _activity(tmp_path, now_ms=now_ms)
    assert activity["companies"] == {
        "active": True,
        "running_tasks": 1,
        "pending_tasks": 1,
    }


def test_future_company_retries_do_not_activate_companies_lane(tmp_path: Path) -> None:
    ledger = TaskLedger(tmp_path / "company_research_tasks.db")
    now_ms = 5_000
    _enqueue_company_task(
        ledger,
        status="pending",
        scheduled_at_ms=now_ms + 1,
        now_ms=now_ms,
        dedup="later",
    )

    activity = _activity(tmp_path, now_ms=now_ms)
    assert activity["companies"] == {
        "active": False,
        "running_tasks": 0,
        "pending_tasks": 0,
    }


def test_activity_status_reader_fails_closed(tmp_path: Path) -> None:
    snapshot = _load_snapshot_bridge()
    missing = tmp_path / "missing.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    listed = tmp_path / "listed.json"
    listed.write_text("[1]", encoding="utf-8")

    assert snapshot._read_activity_status(missing) == {}
    assert snapshot._read_activity_status(broken) == {}
    assert snapshot._read_activity_status(listed) == {}


def test_due_company_queue_counts_fail_closed_on_corrupt_db(tmp_path: Path) -> None:
    snapshot = _load_snapshot_bridge()
    path = tmp_path / "company_research_tasks.db"
    path.write_text("not a database", encoding="utf-8")

    assert snapshot._due_company_queue_counts(path, now_ms=1) == {
        "running_tasks": 0,
        "pending_tasks": 0,
    }


def test_stale_status_files_never_look_active(tmp_path: Path) -> None:
    snapshot = _load_snapshot_bridge()
    _write_status(tmp_path / snapshot._NEWS_MACRO_RUNNER_STATUS, pid=1, status="running")
    _write_status(tmp_path / snapshot._COMPANY_SCAN_STATUS, pid=1, status="running")

    activity = _activity(tmp_path, daemon_pid=4242, daemon_phase="deciding_batch")
    assert activity["context"]["active"] is False
    assert activity["companies"]["active"] is False
    assert activity["decisions"]["active"] is True
