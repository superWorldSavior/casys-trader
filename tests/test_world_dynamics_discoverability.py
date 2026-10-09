from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from trader.interfaces.ui.panels.dashboard import build_view
from trader.reporting.read_models.runtime_state import load_runtime_state
from trader.reporting.read_models.world_status import read_world_model_status
from trader.runtime import cli

from tests.read_models.test_world_dynamics_status import NOW, _publication


def test_status_command_has_no_market_window_and_does_not_load_the_replay(monkeypatch, tmp_path: Path, capsys) -> None:
    from trader.reporting.read_models import world_dynamics

    def forbidden(**_kwargs):
        raise AssertionError("operational status must never replay or train")

    monkeypatch.setattr(world_dynamics, "build_world_dynamics_payload", forbidden)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["world", "dynamics", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "dynamics_status"
    assert payload["status"] == "not_started"
    assert not list(tmp_path.iterdir())


def test_status_command_returns_a_saved_shadow_report(monkeypatch, tmp_path: Path, capsys) -> None:
    _publication(tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["world", "dynamics", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient_support"
    assert payload["series"][0]["support"] == 12
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"


def test_status_command_reports_corruption_without_replaying(monkeypatch, tmp_path: Path, capsys) -> None:
    (tmp_path / "world_dynamics_status.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["world", "dynamics", "status", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "invalid_status_file"
    assert not (tmp_path / "world_model.db").exists()


def test_world_status_overlay_is_light_and_keeps_world_coverage_status(tmp_path: Path) -> None:
    _publication(tmp_path)
    result = read_world_model_status(tmp_path, now=NOW)
    assert result["status"] == "not_started"
    assert result["dynamics"]["status"] == "insufficient_support"
    assert not (tmp_path / "world_model.db").exists()


def test_world_status_text_exposes_the_automatic_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    _publication(tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["world", "status"]) == 0
    text = capsys.readouterr().out
    assert "world_dynamics: insufficient_support shadow_only" in text
    assert "casys-trader world dynamics status --json" in text


def test_regular_status_exposes_the_worker_and_its_lookup_command(monkeypatch, tmp_path: Path, capsys) -> None:
    _publication(tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["world_dynamics"]["status"] == "insufficient_support"
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "world_dynamics: insufficient_support shadow_only" in text
    assert "casys-trader world dynamics status --json" in text


def test_runtime_loader_and_tui_show_the_saved_automatic_worker(tmp_path: Path) -> None:
    _publication(tmp_path)
    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path / "config"))
    assert state["world_dynamics"]["status"] == "insufficient_support"
    console = Console(width=220, color_system=None)
    with console.capture() as captured:
        console.print(build_view(state))
    text = captured.get()
    assert "World dynamics shadow : insufficient_support" in text
    assert "casys-trader world dynamics status" in text
    assert "2026-10-09 12:00 UTC" in text
