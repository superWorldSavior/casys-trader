from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
import sys

import pytest

from trader.application.world_model.capture import SAMPLING_POLICY_VERSION
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID
from trader.runtime import cli


_ARGS = [
    "world",
    "dynamics",
    "--venue",
    "US",
    "--symbol",
    "SPY",
    "--start",
    "2026-10-01T00:00:00Z",
    "--as-of",
    "2026-10-09T09:00:00Z",
    "--json",
]


def _reporter(monkeypatch: pytest.MonkeyPatch, build):
    module = ModuleType("trader.reporting.read_models.world_dynamics")
    module.build_world_dynamics_payload = build
    monkeypatch.setitem(sys.modules, module.__name__, module)


def test_cli_delegates_bounded_request_with_existing_contract_defaults(monkeypatch, capsys, tmp_path: Path) -> None:
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return {"status": "no_data", "authority": "shadow_only", "decision_effect": "none"}

    _reporter(monkeypatch, build)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(_ARGS) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_data"
    assert payload["command"] == "dynamics"
    assert seen == {
        "db_path": tmp_path / "world_model.db",
        "venue": "US",
        "symbol": "SPY",
        "bar_interval": "1h",
        "market_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "sampling_policy_version": SAMPLING_POLICY_VERSION,
        "start_at": "2026-10-01T00:00:00Z",
        "as_of": "2026-10-09T09:00:00Z",
        "limit": 5000,
        "steps": 4,
        "paths": 200,
        "min_support": 40,
        "seed": 0,
    }
    assert not list(tmp_path.iterdir())


def test_cli_forwards_explicit_settings_and_descriptive_support_is_success(monkeypatch, capsys, tmp_path: Path) -> None:
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return {"status": "insufficient_support"}

    _reporter(monkeypatch, build)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    args = _ARGS + [
        "--interval",
        "15m",
        "--sampling-policy-version",
        "fixture.v1",
        "--limit",
        "120",
        "--steps",
        "2",
        "--paths",
        "17",
        "--min-support",
        "10",
        "--seed",
        "4",
    ]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "insufficient_support"
    assert {
        key: seen[key] for key in ("bar_interval", "sampling_policy_version", "limit", "steps", "paths", "seed")
    } == {
        "bar_interval": "15m",
        "sampling_policy_version": "fixture.v1",
        "limit": 120,
        "steps": 2,
        "paths": 17,
        "seed": 4,
    }


@pytest.mark.parametrize("status", ["missing_db", "schema_unavailable", "limit_exceeded", "unavailable"])
def test_cli_returns_nonzero_for_report_errors(monkeypatch, capsys, tmp_path: Path, status: str) -> None:
    _reporter(monkeypatch, lambda **kwargs: {"status": status})
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(_ARGS) == 1
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_cli_renders_domain_request_errors_without_live_authority(monkeypatch, capsys, tmp_path: Path) -> None:
    def invalid(**kwargs):
        raise ValueError("as_of requires an explicit timezone")

    _reporter(monkeypatch, invalid)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(_ARGS) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid_request"
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["recommendation"] == "NO_GO"
    assert payload["causal_claim"] is False
    assert payload["pnl_claim"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("option", ["--venue", "--symbol", "--start", "--as-of"])
def test_cli_requires_explicit_market_and_causal_window(option: str) -> None:
    args = list(_ARGS)
    index = args.index(option)
    del args[index : index + 2]
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_cli_real_missing_ledger_returns_an_error_without_creating_state(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(_ARGS) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "missing_db"
    assert payload["authority"] == "shadow_only"
    assert not list(tmp_path.iterdir())


def test_cli_real_empty_ledger_is_a_descriptive_no_data_result(monkeypatch, capsys, tmp_path: Path) -> None:
    from tests.state_db.test_world_dynamics_query import _database

    path = _database(tmp_path, [])
    before = path.read_bytes()
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(_ARGS) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_data"
    assert payload["data"]["read_episodes"] == 0
    assert payload["rollout"]["paths"] == 0
    assert path.read_bytes() == before


def test_cli_real_replay_returns_simulations_and_preserves_the_ledger(monkeypatch, capsys, tmp_path: Path) -> None:
    from tests.state_db.test_world_dynamics_query import _database, _episode, _row

    path = _database(tmp_path, [_row(_episode(hour)) for hour in range(8)])
    before = path.read_bytes()
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert (
        cli.main(_ARGS + ["--as-of", "2026-10-01T07:00:01Z", "--min-support", "2", "--steps", "2", "--paths", "20"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["read_episodes"] == 8
    assert payload["data"]["adjacent_transitions"] == 7
    assert payload["rollout"]["paths"] == 20
    assert len(payload["rollout"]["distribution"]) == 2
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["pnl_claim"] is False
    assert path.read_bytes() == before
    assert not path.with_name("world_model.db.storage.lock").exists()
