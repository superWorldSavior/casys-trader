from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.reporting.read_models import world_dynamics_status as status_mod
from trader.reporting.read_models.world_dynamics_status import read_world_dynamics_status


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def _publication(tmp_path: Path, **updates) -> dict:
    payload = {
        "schema_version": "world_dynamics_status.v1",
        "status": "insufficient_support", "reason": "collecting_adjacent_bars", "enabled": True,
        "authority": "shadow_only", "decision_effect": "none", "recommendation": "NO_GO",
        "causal_claim": False, "pnl_claim": False,
        "pid": os.getpid(), "started_at": (NOW - timedelta(hours=2)).isoformat(),
        "updated_at": NOW.isoformat(), "captured_at": NOW.isoformat(), "last_run_at": NOW.isoformat(),
        "series": [{"symbol": "SPY", "support": 12, "adjacent_transitions": 12,
                    "status": "insufficient_support", "exclusions": {"nonadjacent_bar": 2}}],
        "report_paths": [str(tmp_path / "world_dynamics" / "reports" / "spy.json")],
        **updates,
    }
    (tmp_path / "world_dynamics_status.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "daemon.pid").write_text(str(payload["pid"]), encoding="utf-8")
    return payload


def test_missing_status_is_explicit_and_never_creates_state(tmp_path: Path) -> None:
    result = read_world_dynamics_status(tmp_path / "absent", now=NOW)
    assert result["status"] == "not_started"
    assert result["enabled"] is None
    assert result["reason"] == "status_not_written"
    assert result["report_command"] == "casys-trader world dynamics status --json"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw", ["{bad", "[]", '{"schema_version":"other.v1"}'])
def test_corrupt_status_remains_readable_without_creating_a_database(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "world_dynamics_status.json"
    path.write_text(raw, encoding="utf-8")
    result = read_world_dynamics_status(tmp_path, now=NOW)
    assert result["status"] == "unavailable"
    assert result["reason"] == "invalid_status_file"
    assert result["decision_effect"] == "none"
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_text(encoding="utf-8") == raw


def test_live_daemon_preserves_last_report_without_claiming_busy(tmp_path: Path) -> None:
    payload = _publication(tmp_path, running=True)
    result = read_world_dynamics_status(tmp_path, now=NOW)
    assert result["status"] == "insufficient_support"
    assert result["process_alive"] is True
    assert result["owner_matches_pid_file"] is True
    assert "running" not in result
    assert result["worker_state_as_of"] == payload["updated_at"]
    assert result["series"] == payload["series"]
    assert result["last_report"]["status"] == "insufficient_support"
    assert not (tmp_path / "world_model.db").exists()


def test_dead_owner_preserves_results_but_reports_stopped(monkeypatch, tmp_path: Path) -> None:
    _publication(tmp_path)
    monkeypatch.setattr(status_mod, "_process_alive", lambda _pid: False)
    result = read_world_dynamics_status(tmp_path, now=NOW)
    assert result["status"] == "worker_stopped"
    assert result["reason"] == "process_not_alive"
    assert result["running"] is False
    assert result["last_report"]["status"] == "insufficient_support"
    assert result["last_report"]["series"][0]["support"] == 12


def test_changed_daemon_identity_does_not_reuse_previous_worker_status(tmp_path: Path) -> None:
    _publication(tmp_path)
    (tmp_path / "daemon.pid").write_text(str(os.getpid() + 1), encoding="utf-8")
    result = read_world_dynamics_status(tmp_path, now=NOW)
    assert result["status"] == "worker_stopped"
    assert result["reason"] == "daemon_owner_changed"
    assert result["last_report"]["status"] == "insufficient_support"


@pytest.mark.parametrize("updates", [
    {"authority": "live"}, {"decision_effect": "trade"}, {"pnl_claim": True},
    {"pid": True}, {"pid": 2**64}, {"updated_at": "2026-10-09T08:00:00Z"}, {"series": {}},
])
def test_invalid_operational_contract_is_explicit(tmp_path: Path, updates: dict) -> None:
    _publication(tmp_path, **updates)
    result = read_world_dynamics_status(tmp_path, now=NOW)
    assert result["status"] == "unavailable"
    assert result["reason"] == "invalid_status_file"
