from __future__ import annotations

import json

from trader.interfaces.cli.world_model import read_world_model_status
from trader.runtime import cli


def test_world_status_does_not_create_a_missing_database(tmp_path) -> None:
    result = read_world_model_status(tmp_path)

    assert result["status"] == "not_started"
    assert result["authority"] == "shadow_only"
    assert result["counts"]["episodes"] == 0
    assert not (tmp_path / "world_model.db").exists()


def test_world_status_reads_an_empty_dedicated_store(tmp_path) -> None:
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db")
    store.close()

    result = read_world_model_status(tmp_path)

    assert result["status"] == "warming_up"
    assert result["counts"] == {
        "episodes": 0,
        "eligible_episodes": 0,
        "outcome_events": 0,
        "active_outcomes": 0,
        "predictions": 0,
    }


def test_cli_world_status_json_is_machine_readable(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)

    assert cli.main(["world", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_started"
    assert payload["decision_effect"] == "none"
