from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.package_layout._helpers import REPO_ROOT
from tests.state_db.test_world_pattern_formation_query import _seed_labeled
from trader.application.world_model.pattern_outcome_link import PatternQueryUnavailable
from trader.domain.world_episode import WorldEpisode, WorldOutcome, canonical_sha256
from trader.infrastructure.state_db import world_pattern_outcome_query as outcome_query
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_outcome_query import SqlitePatternOutcomeLeafQuery


UTC = timezone.utc
AS_OF = datetime(2026, 9, 12, tzinfo=UTC)
POST = datetime(2026, 9, 3, tzinfo=UTC)
_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_outcome_query.py"


def test_outcome_query_is_readonly_and_is_the_only_new_outcome_reader() -> None:
    source = _QUERY.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only=ON" in source
    assert "world_outcome_events" in source
    assert "StateDb(" not in source
    assert "WorldPatternStore(" not in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source


def test_active_leaves_are_returned_and_missing_horizons_stay_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=True, close=False)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    four_h = WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": "elapsed_4h.v1", "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=POST + timedelta(hours=4),
        available_at=POST + timedelta(hours=4, minutes=5),
        computed_at=POST + timedelta(hours=4, minutes=5),
        anchor_close=100.0,
        endpoint_close=100.2,
        endpoint_bar_ts=POST + timedelta(hours=4),
        source="analysis_bars",
        source_raw_sha256=canonical_sha256({"horizon": "elapsed_4h.v1"}),
    )
    world.append_outcome_event(four_h)
    world.close()
    seeded["graph"].close()  # type: ignore[union-attr]

    leaves = SqlitePatternOutcomeLeafQuery(seeded["db_path"]).load_active_observed_leaves(
        episode_ids=(episode.episode_id,),
        horizon_ids=("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"),
        as_of=AS_OF,
    )
    horizons = {item.horizon.horizon_id for item in leaves}
    assert horizons == {"elapsed_4h.v1", "elapsed_1d.v1"}
    assert "elapsed_3d.v1" not in horizons
    assert all(item.status == "observed" for item in leaves)


def _dummy_episode_id(index: int) -> str:
    return "world-episode:v1:" + f"{index:064x}"


def _in_placeholder_counts(statements: list[str], *, column: str) -> list[int]:
    widths: list[int] = []
    token = f"{column} IN"
    for sql in statements:
        compact = " ".join(sql.split())
        if token not in compact:
            continue
        match = re.search(rf"{re.escape(column)} IN \(([^)]*)\)", compact)
        assert match is not None
        body = match.group(1).strip()
        widths.append(0 if not body else body.count(",") + 1)
    return widths


class _TracedReadonly:
    def __init__(self, path: Path, statements: list[str], *, fail_on: str | None = None) -> None:
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        self._connection.set_trace_callback(lambda sql: statements.append(sql))
        self._fail_on = fail_on

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        compact = " ".join(sql.split())
        if self._fail_on is not None and self._fail_on in compact:
            raise sqlite3.DatabaseError("injected operational sqlite failure")
        return self._connection.execute(sql, params)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> _TracedReadonly:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _traced_readonly(path: Path, statements: list[str], *, fail_on: str | None = None) -> _TracedReadonly:
    return _TracedReadonly(path, statements, fail_on=fail_on)


def test_missing_database_returns_empty_without_creating(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "world_model.db"
    leaves = SqlitePatternOutcomeLeafQuery(missing).load_active_observed_leaves(
        episode_ids=("world-episode:v1:" + "a" * 64,),
        horizon_ids=("elapsed_4h.v1",),
        as_of=AS_OF,
    )
    assert leaves == ()
    assert not missing.exists()


def test_outcome_in_list_is_chunked_beyond_sqlite_variable_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=True, close=False)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    four_h = WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": "elapsed_4h.v1", "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=POST + timedelta(hours=4),
        available_at=POST + timedelta(hours=4, minutes=5),
        computed_at=POST + timedelta(hours=4, minutes=5),
        anchor_close=100.0,
        endpoint_close=100.2,
        endpoint_bar_ts=POST + timedelta(hours=4),
        source="analysis_bars",
        source_raw_sha256=canonical_sha256({"horizon": "elapsed_4h.v1"}),
    )
    world.append_outcome_event(four_h)
    world.close()
    seeded["graph"].close()  # type: ignore[union-attr]

    dummy_count = 1099
    dummy_ids = [_dummy_episode_id(index) for index in range(1, dummy_count + 1)]
    episode_ids = (episode.episode_id, *dummy_ids, episode.episode_id, dummy_ids[0])
    assert len(episode_ids) > 999
    statements: list[str] = []
    monkeypatch.setattr(
        outcome_query,
        "_readonly_connection",
        lambda path: _traced_readonly(path, statements),
    )

    leaves = SqlitePatternOutcomeLeafQuery(seeded["db_path"]).load_active_observed_leaves(
        episode_ids=episode_ids,
        horizon_ids=("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"),
        as_of=AS_OF,
    )
    horizons = {item.horizon.horizon_id for item in leaves}
    assert horizons == {"elapsed_4h.v1", "elapsed_1d.v1"}
    assert [item.episode_id for item in leaves] == [episode.episode_id, episode.episode_id]
    in_widths = _in_placeholder_counts(statements, column="episode_id")
    unique_ids = 1 + dummy_count
    assert in_widths
    assert all(width <= 500 for width in in_widths)
    assert max(in_widths) == 500
    assert sum(in_widths) == unique_ids
    assert len(in_widths) >= 3


def test_operational_sqlite_error_is_typed_failure_not_empty_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=True, close=True)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    statements: list[str] = []
    monkeypatch.setattr(
        outcome_query,
        "_readonly_connection",
        lambda path: _traced_readonly(path, statements, fail_on="FROM world_outcome_events"),
    )

    with pytest.raises(PatternQueryUnavailable, match="injected operational sqlite failure"):
        SqlitePatternOutcomeLeafQuery(seeded["db_path"]).load_active_observed_leaves(
            episode_ids=(episode.episode_id,),
            horizon_ids=("elapsed_4h.v1", "elapsed_1d.v1"),
            as_of=AS_OF,
        )
