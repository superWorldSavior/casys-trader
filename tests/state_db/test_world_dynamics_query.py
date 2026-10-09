from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    canonical_json,
    canonical_sha256,
)
from trader.infrastructure.state_db import world_dynamics_query
from trader.infrastructure.state_db.world_dynamics_query import SqliteWorldDynamicsQuery, WorldDynamicsReadError


_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
_CUTOFF = _START + timedelta(hours=6)
_SAMPLING = "active_tradable_completed_bar.v1"
_COLUMNS = (
    "episode_id",
    "venue",
    "symbol",
    "bar_interval",
    "feature_contract_version",
    "sampling_policy_version",
    "as_of_bar_ts",
    "observed_at",
    "available_at",
    "training_eligible",
    "training_reason",
    "payload_json",
    "payload_sha256",
    "recorded_at",
)


def _episode(
    hour: int,
    *,
    symbol: str = "SPY",
    available_at: datetime | None = None,
    captured_at: datetime | None = None,
    eligible: bool = True,
) -> WorldEpisode:
    ts = _START + timedelta(hours=hour)
    available = available_at or ts
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol=symbol,
            bar_interval="1h",
            as_of_bar_ts=ts,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version=_SAMPLING,
            anchor=AnchorBar(
                ts=ts,
                open=100 + hour,
                high=102 + hour,
                low=99 + hour,
                close=101 + hour,
                volume=1000,
                source="fixture",
            ),
            available_at=available,
            captured_at=captured_at or available,
            freshness="fresh",
            numeric_features={"return": 0.01},
        ),
        training_eligible=eligible,
    )


def _row(episode: WorldEpisode, **overrides: object) -> dict[str, object]:
    obs = episode.observation
    result = {
        "episode_id": episode.episode_id,
        "venue": obs.venue,
        "symbol": obs.symbol,
        "bar_interval": obs.bar_interval,
        "feature_contract_version": obs.feature_contract_version,
        "sampling_policy_version": obs.sampling_policy_version,
        "as_of_bar_ts": obs.as_of_bar_ts.isoformat(),
        "observed_at": obs.as_of_bar_ts.isoformat(),
        "available_at": obs.available_at.isoformat(),
        "training_eligible": int(episode.training_eligible),
        "training_reason": episode.training_reason,
        "payload_json": canonical_json(episode.to_dict()),
        "payload_sha256": f"sha256:{episode.payload_hash}",
        "recorded_at": (obs.available_at + timedelta(seconds=1)).isoformat(),
    }
    result.update(overrides)
    return result


def _database(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "world_model.db"
    with sqlite3.connect(path) as conn:
        declarations = ", ".join(f"{name} {'INTEGER' if name == 'training_eligible' else 'TEXT'}" for name in _COLUMNS)
        conn.execute(f"CREATE TABLE world_episodes ({declarations})")
        for row in rows:
            conn.execute(
                f"INSERT INTO world_episodes ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                tuple(row[name] for name in _COLUMNS),
            )
    return path


def _read(path: Path, **overrides: object):
    kwargs = {
        "venue": "US",
        "symbol": "SPY",
        "bar_interval": "1h",
        "market_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "sampling_policy_version": _SAMPLING,
        "start_at": _START,
        "as_of": _CUTOFF,
        "limit": 100,
    }
    kwargs.update(overrides)
    return SqliteWorldDynamicsQuery(path).read_episodes(**kwargs)


def test_missing_database_is_explicit_and_is_never_created(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "world_model.db"
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "missing_db"
    assert not path.parent.exists()


def test_reads_ordered_exact_market_scope_and_preserves_real_evidence(tmp_path: Path) -> None:
    path = _database(tmp_path, [_row(_episode(3)), _row(_episode(1)), _row(_episode(2, symbol="QQQ"))])
    before = path.read_bytes()
    evidence = _read(path)
    assert [item.episode.observation.as_of_bar_ts.hour for item in evidence] == [1, 3]
    assert evidence[0].recorded_at == _START + timedelta(hours=1, seconds=1)
    assert evidence[0].effective_available_at == evidence[0].recorded_at
    assert path.read_bytes() == before
    assert not path.with_name("world_model.db.storage.lock").exists()


def test_future_availability_capture_and_recording_are_excluded(tmp_path: Path) -> None:
    later = _CUTOFF + timedelta(minutes=1)
    path = _database(
        tmp_path,
        [
            _row(_episode(1)),
            _row(_episode(2, available_at=later)),
            _row(_episode(3, captured_at=later)),
            _row(_episode(4), recorded_at=later.isoformat()),
            _row(_episode(7)),
            _row(_episode(5, eligible=False)),
        ],
    )
    assert [item.episode.observation.as_of_bar_ts.hour for item in _read(path)] == [1]


def test_window_is_inclusive_and_bounds_anchor_time(tmp_path: Path) -> None:
    path = _database(tmp_path, [_row(_episode(0)), _row(_episode(1)), _row(_episode(2))])
    result = _read(path, start_at=_START + timedelta(hours=1), as_of=_START + timedelta(hours=2, seconds=1))
    assert [item.episode.observation.as_of_bar_ts.hour for item in result] == [1, 2]


def test_limit_plus_one_refuses_a_partial_training_series(tmp_path: Path) -> None:
    path = _database(tmp_path, [_row(_episode(1)), _row(_episode(2)), _row(_episode(3))])
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path, limit=2)
    assert error.value.code == "limit_exceeded"
    assert len(_read(path, limit=3)) == 3


@pytest.mark.parametrize("limit", [0, -1, True, 20_001, 1.5])
def test_invalid_limit_is_rejected_before_opening(tmp_path: Path, limit: object) -> None:
    with pytest.raises(ValueError):
        _read(tmp_path / "missing.db", limit=limit)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "override",
    [
        {"episode_id": "changed"},
        {"payload_sha256": "sha256:" + "0" * 64},
        {"available_at": (_START + timedelta(hours=1, seconds=2)).isoformat()},
        {"observed_at": (_START + timedelta(hours=1, seconds=2)).isoformat()},
        {"training_reason": "different"},
    ],
)
def test_indexed_payload_hash_and_clocks_must_match(tmp_path: Path, override: dict[str, object]) -> None:
    path = _database(tmp_path, [_row(_episode(1), **override)])
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "invalid_episode"


def test_indexed_scope_cannot_smuggle_another_instrument_payload(tmp_path: Path) -> None:
    path = _database(tmp_path, [_row(_episode(1, symbol="QQQ"), symbol="SPY")])
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "invalid_episode"


def test_indexed_eligible_flag_cannot_upgrade_an_ineligible_payload(tmp_path: Path) -> None:
    path = _database(tmp_path, [_row(_episode(1, eligible=False), training_eligible=1)])
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "invalid_episode"


def test_malformed_ohlcv_payload_is_not_admitted_even_with_a_matching_hash(tmp_path: Path) -> None:
    row = _row(_episode(1))
    payload = json.loads(row["payload_json"])
    payload["observation"]["anchor"]["high"] = 1
    row.update(payload_json=canonical_json(payload), payload_sha256=canonical_sha256(payload))
    path = _database(tmp_path, [row])
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "invalid_episode"


def test_duplicate_slots_are_preserved_for_application_conflict_admission(tmp_path: Path) -> None:
    first = _row(_episode(1))
    second = _row(_episode(1, available_at=_START + timedelta(hours=1, seconds=2)))
    path = _database(tmp_path, [second, first])
    evidence = _read(path)
    assert len(evidence) == 2
    assert evidence[0].episode.episode_id == evidence[1].episode.episode_id
    assert evidence[0].episode.payload_hash != evidence[1].episode.payload_hash


def test_missing_schema_does_not_migrate_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "schema_unavailable"
    assert path.read_bytes() == before


def test_query_is_readonly_bounded_and_ignores_prediction_storage(tmp_path: Path, monkeypatch) -> None:
    path = _database(tmp_path, [_row(_episode(1))])
    original = world_dynamics_query._readonly_connection
    statements: list[str] = []

    def traced(file: Path):
        conn = original(file)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE accidental (value TEXT)")
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(world_dynamics_query, "_readonly_connection", traced)
    assert len(_read(path, limit=7)) == 1
    assert any("LIMIT 8" in sql for sql in statements)
    assert all("prediction" not in sql.lower() and "outcome" not in sql.lower() for sql in statements)


def test_corrupt_database_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(WorldDynamicsReadError) as error:
        _read(path)
    assert error.value.code == "unavailable"
