"""Independent parity expectations for the World prediction SQLite -> Parquet cutover.

These tests seed a real WorldModelStore, archive closed-day rows with the existing
export pipeline, then activate cold storage through the public tier API. They do
not drive the unfinished operator CLI. Logical store/reader/replay outputs after
mixed and all-cold activation must match the pre-cutover snapshots.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pytest

from tests.infrastructure.test_world_model_store import _episode, _observation, _prediction
from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.prediction_archive import WorldPredictionArchiveService
from trader.application.world_model.service import WorldModelService
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID, AnchorBar, WorldEpisode, WorldObservation
from trader.domain.world_prediction_archive import WorldPredictionArchiveManifest, WorldPredictionArchivePlan
from trader.infrastructure.state_db.sqlite_in import SQLITE_HOST_PARAMETER_LIMIT
from trader.infrastructure.state_db.world_model_query import read_world_model_ledger
from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore
from trader.infrastructure.state_db.world_prediction_archive_source import SQLiteWorldPredictionArchiveSource
from trader.infrastructure.state_db.world_prediction_cutover import prepare_prediction_cutover
from trader.infrastructure.state_db.world_prediction_parquet_store import DuckDbWorldPredictionParquetStore
from trader.infrastructure.state_db.world_prediction_tiers import (
    PredictionReadUnavailable,
    cold_prediction_for_replay,
    ensure_cold_schema,
    prediction_count,
    read_prediction_identities,
    read_prediction_rows,
    register_cold_partition,
)


UTC = timezone.utc
LIVE_NOW = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
LIVE_RECORDED_AT = LIVE_NOW + timedelta(minutes=1)
ARCHIVE_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ARCHIVE_BEFORE = date(2026, 8, 28)
RUNTIME_ARCHIVE_BEFORE = date(2026, 8, 29)
RUNTIME_ARCHIVE_CLOCK = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
LIVE_REPLAY_AT = RUNTIME_ARCHIVE_CLOCK + timedelta(minutes=1)
LIVE_NEXT_WRITE_AT = LIVE_REPLAY_AT + timedelta(minutes=1)
CUTOVER_ID = "parity-cutover-20260907"
DELETE_TRIGGER = "world_predictions_no_delete"
UPDATE_TRIGGER = "world_predictions_no_update"
SQLITE_VARIABLE_LIMIT = 999
HORIZON_4H = "elapsed_4h.v1"
HORIZON_1D = "elapsed_1d.v1"
READER_COLUMNS = (
    "prediction_id",
    "run_id",
    "episode_id",
    "horizon_code",
    "model_kind",
    "model_version",
    "predicted_at",
    "input_sha256",
    "prediction_json",
    "prediction_sha256",
    "payload_json",
    "payload_sha256",
    "recorded_at",
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)

assert SQLITE_HOST_PARAMETER_LIMIT == SQLITE_VARIABLE_LIMIT


class RecordClock:
    """Single frozen source for WorldModelStore._utc_now and the injected clock."""

    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("record clock must be timezone-aware")
        self.current = current.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.current

    def isoformat(self) -> str:
        return self.current.isoformat()

    def set(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("record clock must be timezone-aware")
        self.current = current.astimezone(UTC)


def bind_world_record_clock(monkeypatch: pytest.MonkeyPatch, clock: RecordClock) -> None:
    monkeypatch.setattr("trader.infrastructure.state_db.world_model_store._utc_now", clock.isoformat)


def sqlite_connection(store: WorldModelStore) -> sqlite3.Connection:
    connection = store._db._conn
    assert connection is not None
    return connection


def stored_prediction_record(rows: Sequence[Mapping[str, Any]], prediction_id: str) -> dict[str, Any]:
    for row in rows:
        if row["prediction_id"] != prediction_id:
            continue
        record = row["prediction_record"]
        if not isinstance(record, Mapping):
            raise TypeError("prediction_record must be a mapping")
        return deepcopy(dict(record))
    raise KeyError(prediction_id)


def archive_root_for(db_path: Path) -> Path:
    return db_path.parent / "world_model_archive"


def _sqlite_asc(value: object) -> tuple[int, object]:
    if value is None:
        return (0, "")
    return (1, value)


def store_sort_key(row: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        _sqlite_asc(row["run_id"]),
        _sqlite_asc(row.get("episode_observed_at")),
        _sqlite_asc(row["episode_id"]),
        _sqlite_asc(row["prediction_id"]),
    )


def recorded_sort_key(row: Mapping[str, Any]) -> tuple[object, ...]:
    return (_sqlite_asc(row["recorded_at"]), _sqlite_asc(row["prediction_id"]))


def expected_identities(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {
        (row["episode_id"], row["horizon_code"], row.get("model_kind"), row.get("model_version")) for row in rows
    }
    ordered = sorted(
        unique,
        key=lambda item: (_sqlite_asc(item[0]), _sqlite_asc(item[1]), _sqlite_asc(item[2]), _sqlite_asc(item[3])),
    )
    return [
        {
            "episode_id": episode_id,
            "horizon_code": horizon_code,
            "model_kind": model_kind,
            "model_version": model_version,
        }
        for episode_id, horizon_code, model_kind, model_version in ordered
    ]


def _observation_at(symbol: str, ts: str) -> WorldObservation:
    return _observation(
        symbol=symbol,
        as_of_bar_ts=ts,
        available_at=ts,
        captured_at=ts,
        anchor=AnchorBar(ts=ts, open=100.0, high=102.0, low=99.0, close=101.0, volume=1_000.0, source="fixture"),
    )


def _as_register_rows(
    form: str,
    manifest: WorldPredictionArchiveManifest,
    tuples: Sequence[tuple[object, ...]],
    connection: sqlite3.Connection,
) -> Sequence[object]:
    if form == "tuple":
        return tuples
    names = manifest.column_names
    if form == "dict":
        return [dict(zip(names, row, strict=True)) for row in tuples]
    if form != "row":
        raise ValueError(f"unknown register row form {form!r}")
    id_index = names.index("prediction_id")
    prediction_ids = [str(row[id_index]) for row in tuples]
    selected = ", ".join(f'"{name}"' for name in names)
    placeholders = ",".join("?" for _ in prediction_ids)
    return list(
        connection.execute(
            f"SELECT {selected} FROM world_shadow_predictions "
            f"WHERE prediction_id IN ({placeholders}) "
            "ORDER BY recorded_at, prediction_id",
            prediction_ids,
        ).fetchall()
    )


def _prediction_ids_from_rows(manifest: WorldPredictionArchiveManifest, rows: Sequence[object]) -> list[str]:
    names = manifest.column_names
    id_index = names.index("prediction_id")
    collected: list[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            collected.append(str(row["prediction_id"]))
        else:
            collected.append(str(row[id_index]))
    return collected


def archive_closed_day_partitions(
    db_path: Path,
    *,
    before: date = ARCHIVE_BEFORE,
    clock: datetime = ARCHIVE_NOW,
) -> tuple[dict[str, Any], list[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[tuple[object, ...], ...]]]]:
    root = archive_root_for(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(root, clock=lambda: clock)
    report = WorldPredictionArchiveService(source=source, sink=sink, clock=lambda: clock).execute(
        before=before,
        apply=True,
    )
    assert report["source_retained"] is True
    assert report["authority"] == "shadow_only"
    assert report["decision_effect"] == "none"
    partitions: list[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[tuple[object, ...], ...]]] = []
    for plan in source.plan_before(before):
        manifest = sink.published(plan)
        assert manifest is not None
        assert isinstance(manifest, WorldPredictionArchiveManifest)
        assert manifest.source_retained is True
        rows = tuple(source.iter_partition(plan))
        assert len(rows) == manifest.row_count
        assert manifest.relative_path
        partitions.append((plan, manifest, rows))
    return report, partitions


def activate_registered_partitions(
    store: WorldModelStore,
    partitions: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, Sequence[tuple[object, ...]]]],
    *,
    cutover_id: str = CUTOVER_ID,
    activated_at: datetime = ARCHIVE_NOW,
    remove_hot: bool = True,
    recorded_dates: set[date] | None = None,
) -> list[str]:
    selected = [
        item for item in partitions if recorded_dates is None or item[0].recorded_date in recorded_dates
    ]
    connection = sqlite_connection(store)
    forms = ("tuple", "dict", "row")
    prepared: list[tuple[WorldPredictionArchiveManifest, Sequence[object], list[str]]] = []
    for index, (_plan, manifest, tuples) in enumerate(selected):
        rows = _as_register_rows(forms[index % len(forms)], manifest, tuples, connection)
        prepared.append((manifest, rows, _prediction_ids_from_rows(manifest, rows)))

    registered_ids: list[str] = []
    with store._db.transaction() as cur:
        ensure_cold_schema(cur)
        for manifest, rows, prediction_ids in prepared:
            count = register_cold_partition(
                cur,
                manifest=manifest,
                rows=rows,
                cutover_id=cutover_id,
                activated_at=activated_at.isoformat(),
            )
            assert count == len(rows)
            registered_ids.extend(prediction_ids)
        if remove_hot and registered_ids:
            trigger = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (DELETE_TRIGGER,),
            ).fetchone()
            assert trigger is not None and trigger[0]
            cur.execute(f"DROP TRIGGER {DELETE_TRIGGER}")
            placeholders = ",".join("?" for _ in registered_ids)
            cur.execute(
                f"DELETE FROM world_shadow_predictions WHERE prediction_id IN ({placeholders})",
                registered_ids,
            )
            cur.execute(trigger[0])

    restored = store._db.query_one(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (DELETE_TRIGGER,),
    )
    assert restored is not None
    return registered_ids


def hot_prediction_ids(store: WorldModelStore) -> set[str]:
    return {str(row["prediction_id"]) for row in store._db.query_all("SELECT prediction_id FROM world_shadow_predictions")}


def parquet_path(db_path: Path, manifest: WorldPredictionArchiveManifest) -> Path:
    return archive_root_for(db_path) / manifest.relative_path


def manifest_path(db_path: Path, manifest: WorldPredictionArchiveManifest) -> Path:
    return parquet_path(db_path, manifest).with_name("manifest.json")


def _filter_store_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
    episode_id: str | None = None,
    horizon_code: str | None = None,
    horizon_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    horizon = horizon_code if horizon_code is not None else horizon_id
    selected = []
    for row in rows:
        if run_id is not None and row["run_id"] != run_id:
            continue
        if episode_id is not None and row["episode_id"] != episode_id:
            continue
        if horizon is not None and row["horizon_code"] != horizon:
            continue
        selected.append(row)
    ordered = sorted(selected, key=store_sort_key)
    if limit is None:
        return list(ordered)
    return list(ordered[: int(limit)])


@dataclass
class SeededWorld:
    store: WorldModelStore
    path: Path
    clock: RecordClock
    payloads: dict[str, dict[str, Any]]
    episodes: dict[str, dict[str, Any]]
    before_predictions: list[dict[str, Any]]
    before_identities: list[dict[str, Any]]
    before_counts: dict[str, int]
    before_reader_rows: list[dict[str, Any]]
    archive_report: dict[str, Any] | None = None
    partitions: list[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[tuple[object, ...], ...]]] = field(
        default_factory=list
    )
    registered_ids: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.store.close()


def open_seeded_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SeededWorld:
    clock = RecordClock(datetime(2026, 8, 24, 8, 0, tzinfo=UTC))
    bind_world_record_clock(monkeypatch, clock)
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=clock)

    late = _episode(observation=_observation_at("MSFT", "2026-08-21T00:00:00+00:00"))
    early = _episode(observation=_observation_at("AAPL", "2026-08-20T00:00:00+00:00"))
    mid = _episode(observation=_observation_at("GOOG", "2026-08-20T00:00:00+00:00"))
    assert store.append_episode(late)
    assert store.append_episode(early)
    assert store.append_episode(mid)

    payloads = {
        "p-runb-late": _prediction(
            "p-runb-late",
            run_id="run-b",
            episode_id=late["episode_id"],
            horizon_id=HORIZON_1D,
            model_kind="markov",
            model_version="markov-v1",
            predicted_at="2026-08-21T00:00:00+00:00",
        ),
        "p-early-4h": _prediction(
            "p-early-4h",
            run_id="run-a",
            episode_id=early["episode_id"],
            horizon_id=HORIZON_4H,
            predicted_at="2026-08-20T00:00:00+00:00",
        ),
        "p-mid-dup-a": _prediction(
            "p-mid-dup-a",
            run_id="run-a",
            episode_id=mid["episode_id"],
            horizon_id=HORIZON_4H,
            predicted_at="2026-08-20T00:00:00+00:00",
            prediction={"p_up": 0.61, "p_flat": 0.29, "p_down": 0.10, "status": "ok"},
        ),
        "p-mid-dup-b": _prediction(
            "p-mid-dup-b",
            run_id="run-a",
            episode_id=mid["episode_id"],
            horizon_id=HORIZON_4H,
            predicted_at="2026-08-20T00:00:00+00:00",
            prediction={"p_up": 0.62, "p_flat": 0.28, "p_down": 0.10, "status": "ok"},
        ),
        "p-legacy-null": _prediction(
            "p-legacy-null",
            run_id="run-a",
            episode_id=early["episode_id"],
            horizon_id=HORIZON_4H,
            model_kind=None,
            model_version=None,
            predicted_at="2026-08-20T00:00:00+00:00",
        ),
        "p-late-v2": _prediction(
            "p-late-v2",
            run_id="run-a",
            episode_id=late["episode_id"],
            horizon_id=HORIZON_1D,
            model_kind="markov",
            model_version="markov-v2",
            predicted_at="2026-08-21T00:00:00+00:00",
        ),
        "p-runc-early": _prediction(
            "p-runc-early",
            run_id="run-c",
            episode_id=early["episode_id"],
            horizon_id=HORIZON_4H,
            predicted_at="2026-08-20T00:00:00+00:00",
        ),
        "p-early-1d": _prediction(
            "p-early-1d",
            run_id="run-a",
            episode_id=early["episode_id"],
            horizon_id=HORIZON_1D,
            predicted_at="2026-08-20T00:00:00+00:00",
        ),
    }

    clock.set(datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-runb-late"]) is True
    clock.set(datetime(2026, 8, 24, 10, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-early-4h"]) is True
    assert store.append_legacy_prediction(payloads["p-mid-dup-a"]) is True
    clock.set(datetime(2026, 8, 24, 11, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-mid-dup-b"]) is True
    clock.set(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-legacy-null"]) is True
    clock.set(datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-late-v2"]) is True
    assert store.append_legacy_prediction(payloads["p-runc-early"]) is True
    clock.set(datetime(2026, 8, 26, 8, 0, tzinfo=UTC))
    assert store.append_legacy_prediction(payloads["p-early-1d"]) is True

    before_predictions = store.list_predictions()
    assert [row["prediction_id"] for row in before_predictions] == [
        row["prediction_id"] for row in sorted(before_predictions, key=store_sort_key)
    ]
    before_identities = store.list_prediction_identities()
    assert before_identities == expected_identities(before_predictions)
    before_counts = store.counts()
    before_reader_rows = read_sqlite_prediction_dicts(sqlite_connection(store), READER_COLUMNS)
    return SeededWorld(
        store=store,
        path=path,
        clock=clock,
        payloads=payloads,
        episodes={"late": late, "early": early, "mid": mid},
        before_predictions=before_predictions,
        before_identities=before_identities,
        before_counts=before_counts,
        before_reader_rows=before_reader_rows,
    )


def read_sqlite_prediction_dicts(
    connection: sqlite3.Connection,
    columns: Sequence[str],
) -> list[dict[str, Any]]:
    selected = ", ".join(f'"{name}"' for name in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM world_shadow_predictions ORDER BY recorded_at, prediction_id"
    ).fetchall()
    rendered: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            rendered.append({name: row[name] for name in columns})
        else:
            rendered.append({name: row[index] for index, name in enumerate(columns)})
    return rendered


def migrate_world(
    world: SeededWorld,
    *,
    mode: str,
    remove_hot: bool = True,
    before: date = ARCHIVE_BEFORE,
    clock: datetime = ARCHIVE_NOW,
) -> SeededWorld:
    report, partitions = archive_closed_day_partitions(world.path, before=before, clock=clock)
    world.archive_report = report
    world.partitions = partitions
    dates: set[date] | None
    if mode == "mixed":
        dates = {date(2026, 8, 24)}
    elif mode == "all_cold":
        dates = None
    else:
        raise ValueError(mode)
    world.registered_ids = activate_registered_partitions(
        world.store,
        partitions,
        recorded_dates=dates,
        remove_hot=remove_hot,
        activated_at=clock,
    )
    return world


def assert_store_parity(world: SeededWorld) -> None:
    after = world.store.list_predictions()
    assert after == world.before_predictions
    assert [row["prediction_id"] for row in after] == [
        row["prediction_id"] for row in sorted(world.before_predictions, key=store_sort_key)
    ]
    assert world.store.list_prediction_identities() == world.before_identities
    assert read_prediction_identities(sqlite_connection(world.store)) == world.before_identities
    counts = world.store.counts()
    assert counts["episodes"] == world.before_counts["episodes"]
    assert counts["outcome_events"] == world.before_counts["outcome_events"]
    assert counts["predictions"] == world.before_counts["predictions"]
    assert prediction_count(sqlite_connection(world.store)) == world.before_counts["predictions"]


@pytest.fixture()
def seeded_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SeededWorld]:
    world = open_seeded_world(tmp_path, monkeypatch)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture(params=["mixed", "all_cold"])
def migrated_world(seeded_world: SeededWorld, request: pytest.FixtureRequest) -> SeededWorld:
    return migrate_world(seeded_world, mode=str(request.param))


def test_store_list_counts_and_identities_match_before_mixed_and_all_cold(migrated_world: SeededWorld) -> None:
    world = migrated_world
    assert_store_parity(world)
    hot_ids = hot_prediction_ids(world.store)
    registered = set(world.registered_ids)
    if set(hot_ids) == set():
        assert registered == {row["prediction_id"] for row in world.before_predictions}
    else:
        assert registered.isdisjoint(hot_ids)
        assert registered | hot_ids == {row["prediction_id"] for row in world.before_predictions}


def test_store_order_is_run_episode_observed_episode_id_prediction_id(migrated_world: SeededWorld) -> None:
    world = migrated_world
    rows = world.store.list_predictions()
    assert [row["prediction_id"] for row in rows] == [
        row["prediction_id"] for row in sorted(world.before_predictions, key=store_sort_key)
    ]
    early_id = world.episodes["early"]["episode_id"]
    mid_id = world.episodes["mid"]["episode_id"]
    late_id = world.episodes["late"]["episode_id"]
    by_id = {row["prediction_id"]: row for row in rows}
    assert by_id["p-early-4h"]["episode_id"] == early_id
    assert by_id["p-mid-dup-a"]["episode_id"] == mid_id
    assert by_id["p-runb-late"]["episode_id"] == late_id
    run_a = [row for row in rows if row["run_id"] == "run-a"]
    observed = [row["episode_observed_at"] for row in run_a]
    assert observed == sorted(observed)
    same_stamp = [
        row
        for row in world.before_reader_rows
        if row["recorded_at"] == "2026-08-24T10:00:00+00:00"
    ]
    assert {row["prediction_id"] for row in same_stamp} == {"p-early-4h", "p-mid-dup-a"}


@pytest.mark.parametrize(
    "case",
    [
        "run_id",
        "episode_id",
        "horizon_code",
        "horizon_id",
        "combined",
        "absent_run",
        "absent_episode",
        "limit_0",
        "limit_1",
        "limit_2",
        "limit_all",
        "limit_beyond",
    ],
)
def test_store_filters_and_global_limits(migrated_world: SeededWorld, case: str) -> None:
    world = migrated_world
    early_id = world.episodes["early"]["episode_id"]
    total = len(world.before_predictions)
    kwargs: dict[str, Any]
    if case == "run_id":
        kwargs = {"run_id": "run-a"}
    elif case == "episode_id":
        kwargs = {"episode_id": early_id}
    elif case == "horizon_code":
        kwargs = {"horizon_code": HORIZON_4H}
    elif case == "horizon_id":
        kwargs = {"horizon_id": HORIZON_1D}
    elif case == "combined":
        kwargs = {"run_id": "run-a", "episode_id": early_id, "horizon_code": HORIZON_4H}
    elif case == "absent_run":
        kwargs = {"run_id": "run-missing"}
    elif case == "absent_episode":
        kwargs = {"episode_id": "episode-missing"}
    elif case == "limit_0":
        kwargs = {"limit": 0}
    elif case == "limit_1":
        kwargs = {"limit": 1}
    elif case == "limit_2":
        kwargs = {"limit": 2}
    elif case == "limit_all":
        kwargs = {"limit": total}
    else:
        kwargs = {"limit": total + 1}
    expected = _filter_store_rows(world.before_predictions, **kwargs)
    actual = world.store.list_predictions(**kwargs)
    assert actual == expected
    assert [row["prediction_id"] for row in actual] == [row["prediction_id"] for row in expected]


def test_reader_selected_columns_recorded_order_and_global_limit(migrated_world: SeededWorld) -> None:
    world = migrated_world
    connection = sqlite_connection(world.store)
    expected = sorted(world.before_reader_rows, key=recorded_sort_key)
    selected = ("prediction_id", "recorded_at", "payload_json", "prediction_json", "payload_sha256")
    rows = read_prediction_rows(connection, columns=selected, order="recorded")
    assert [row["prediction_id"] for row in rows] == [row["prediction_id"] for row in expected]
    for actual, source in zip(rows, expected, strict=True):
        assert set(actual) == set(selected)
        assert actual["recorded_at"] == source["recorded_at"]
        assert actual["payload_json"] == source["payload_json"]
        assert actual["prediction_json"] == source["prediction_json"]
        assert actual["payload_sha256"] == source["payload_sha256"]
    limited = read_prediction_rows(connection, columns=("prediction_id", "recorded_at"), order="recorded", limit=2)
    assert [row["prediction_id"] for row in limited] == [row["prediction_id"] for row in expected[:2]]
    day24 = [row for row in expected if str(row["recorded_at"]).startswith("2026-08-24")]
    crossed = read_prediction_rows(
        connection,
        columns=("prediction_id", "recorded_at"),
        order="recorded",
        limit=len(day24) + 1,
    )
    assert [row["prediction_id"] for row in crossed] == [row["prediction_id"] for row in expected[: len(day24) + 1]]
    assert str(crossed[-1]["recorded_at"]).startswith("2026-08-25")


def test_reader_prediction_and_episode_id_filters(migrated_world: SeededWorld) -> None:
    world = migrated_world
    connection = sqlite_connection(world.store)
    expected = sorted(world.before_reader_rows, key=recorded_sort_key)
    by_id = {row["prediction_id"]: row for row in expected}
    early_id = world.episodes["early"]["episode_id"]
    mid_id = world.episodes["mid"]["episode_id"]

    assert read_prediction_rows(connection, columns=("prediction_id",), prediction_ids=[]) == []
    assert read_prediction_rows(connection, columns=("prediction_id",), episode_ids=[]) == []

    requested = ["p-mid-dup-b", "unknown", "p-early-4h", "p-early-4h", "ghost"]
    filtered = read_prediction_rows(
        connection,
        columns=("prediction_id", "recorded_at", "episode_id"),
        prediction_ids=requested,
        order="recorded",
    )
    assert [row["prediction_id"] for row in filtered] == ["p-early-4h", "p-mid-dup-b"]
    assert filtered[0]["recorded_at"] == by_id["p-early-4h"]["recorded_at"]
    assert filtered[1]["episode_id"] == mid_id

    by_episode = read_prediction_rows(
        connection,
        columns=("prediction_id", "episode_id", "recorded_at"),
        episode_ids=[early_id, "episode-missing", early_id],
        order="recorded",
    )
    assert [row["prediction_id"] for row in by_episode] == [
        row["prediction_id"] for row in expected if row["episode_id"] == early_id
    ]
    assert {row["episode_id"] for row in by_episode} == {early_id}


@pytest.mark.parametrize("mode", ["hot", "mixed", "all_cold"])
def test_reader_chunks_1100_ids_below_sqlite_variable_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    clock = RecordClock(datetime(2026, 8, 24, 8, 0, tzinfo=UTC))
    bind_world_record_clock(monkeypatch, clock)
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=clock)
    try:
        episode = _episode(observation=_observation_at("IBM", "2026-08-20T00:00:00+00:00"))
        assert store.append_episode(episode)
        ordered_ids = ["pred-1099", "pred-0000", "pred-0500", "pred-0998"]
        stamps = [
            datetime(2026, 8, 24, 1, 0, tzinfo=UTC),
            datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
            datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
            datetime(2026, 8, 26, 4, 0, tzinfo=UTC),
        ]
        for prediction_id, stamp in zip(ordered_ids, stamps, strict=True):
            clock.set(stamp)
            assert store.append_legacy_prediction(
                _prediction(
                    prediction_id,
                    run_id="run-filter",
                    episode_id=episode["episode_id"],
                    horizon_id=HORIZON_4H,
                    predicted_at=stamp.isoformat(),
                )
            )
        world = SeededWorld(
            store=store,
            path=path,
            clock=clock,
            payloads={},
            episodes={"only": episode},
            before_predictions=store.list_predictions(),
            before_identities=store.list_prediction_identities(),
            before_counts=store.counts(),
            before_reader_rows=read_sqlite_prediction_dicts(sqlite_connection(store), READER_COLUMNS),
        )
        if mode != "hot":
            migrate_world(world, mode=mode)
        connection = sqlite_connection(store)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        assert connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == 999
        requested = [f"pred-{index:04d}" for index in range(1100)]
        requested.extend(["pred-0000", "pred-0500", "missing-id"])
        assert len(requested) > SQLITE_VARIABLE_LIMIT
        rows = read_prediction_rows(
            connection,
            columns=("prediction_id", "recorded_at"),
            prediction_ids=requested,
            order="recorded",
        )
        assert [row["prediction_id"] for row in rows] == ordered_ids
        hot_ids = hot_prediction_ids(store)
        if mode == "hot":
            assert hot_ids == set(ordered_ids)
        elif mode == "mixed":
            assert hot_ids == {"pred-0500", "pred-0998"}
            assert set(world.registered_ids) == {"pred-1099", "pred-0000"}
        else:
            assert hot_ids == set()
            assert set(world.registered_ids) == set(ordered_ids)
    finally:
        store.close()


def test_distinct_ids_sharing_business_tuple_remain_distinct(migrated_world: SeededWorld) -> None:
    world = migrated_world
    rows = [row for row in world.store.list_predictions() if row["prediction_id"] in {"p-mid-dup-a", "p-mid-dup-b"}]
    assert [row["prediction_id"] for row in rows] == ["p-mid-dup-a", "p-mid-dup-b"]
    assert rows[0]["episode_id"] == rows[1]["episode_id"]
    assert rows[0]["horizon_code"] == rows[1]["horizon_code"] == HORIZON_4H
    assert rows[0]["model_kind"] == rows[1]["model_kind"]
    assert rows[0]["model_version"] == rows[1]["model_version"]
    assert rows[0]["prediction_json"] != rows[1]["prediction_json"]
    identities = [
        item
        for item in world.store.list_prediction_identities()
        if item["episode_id"] == world.episodes["mid"]["episode_id"] and item["horizon_code"] == HORIZON_4H
    ]
    assert len(identities) == 1
    reader = read_prediction_rows(
        sqlite_connection(world.store),
        columns=("prediction_id", "episode_id", "horizon_code", "model_kind", "model_version"),
        prediction_ids=["p-mid-dup-a", "p-mid-dup-b"],
        order="recorded",
    )
    assert [row["prediction_id"] for row in reader] == ["p-mid-dup-a", "p-mid-dup-b"]


def test_exact_hot_cold_id_overlap_dedups_to_one_row(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="all_cold", remove_hot=False)
    hot_ids = hot_prediction_ids(world.store)
    assert set(world.registered_ids) <= hot_ids
    assert_store_parity(world)
    rows = read_prediction_rows(
        sqlite_connection(world.store),
        columns=("prediction_id", "payload_sha256"),
        order="recorded",
    )
    ids = [row["prediction_id"] for row in rows]
    assert ids == [row["prediction_id"] for row in sorted(world.before_reader_rows, key=recorded_sort_key)]
    assert len(ids) == len(set(ids))
    assert prediction_count(sqlite_connection(world.store)) == len(set(ids))


def test_divergent_hot_cold_same_id_fails_closed(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="mixed", remove_hot=False)
    target = "p-early-4h"
    with world.store._db.transaction() as cur:
        trigger = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (UPDATE_TRIGGER,),
        ).fetchone()
        assert trigger is not None and trigger[0]
        cur.execute(f"DROP TRIGGER {UPDATE_TRIGGER}")
        cur.execute(
            "UPDATE world_shadow_predictions SET prediction_sha256=? WHERE prediction_id=?",
            ("sha256:" + "ab" * 32, target),
        )
        cur.execute(trigger[0])
    with pytest.raises((WorldModelConflictError, PredictionReadUnavailable)):
        world.store.list_predictions()
    with pytest.raises((WorldModelConflictError, PredictionReadUnavailable)):
        read_prediction_rows(
            sqlite_connection(world.store),
            columns=("prediction_id", "prediction_sha256"),
            prediction_ids=[target],
        )


def test_archived_exact_replay_false_and_changed_content_conflicts(migrated_world: SeededWorld) -> None:
    world = migrated_world
    original = stored_prediction_record(world.before_predictions, "p-early-4h")
    assert world.store.append_legacy_prediction(deepcopy(original)) is False
    conflict = deepcopy(original)
    nested = dict(conflict["prediction"])
    nested["p_up"] = 0.01
    conflict["prediction"] = nested
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        world.store.append_legacy_prediction(conflict)


def test_archived_replay_and_new_live_append(migrated_world: SeededWorld) -> None:
    world = migrated_world
    original = stored_prediction_record(world.before_predictions, "p-mid-dup-a")
    assert world.store.append_legacy_prediction(deepcopy(original)) is False
    before_hashes = {
        (row["prediction_id"], row["payload_sha256"], row["prediction_sha256"], row["input_sha256"])
        for row in world.store.list_predictions()
    }
    world.clock.set(LIVE_REPLAY_AT)
    live_episode = _runtime_episode(symbol="SPY")
    assert world.store.append_episode(live_episode) is True
    live = _live_prediction_mapping(live_episode, prediction_id="live-after-cutover")
    assert world.store.append_prediction(live) is True
    union = world.store.list_predictions()
    assert any(row["prediction_id"] == "live-after-cutover" for row in union)
    after_old = {
        (row["prediction_id"], row["payload_sha256"], row["prediction_sha256"], row["input_sha256"])
        for row in union
        if row["prediction_id"] != "live-after-cutover"
    }
    assert after_old == before_hashes
    assert world.store.counts()["predictions"] == world.before_counts["predictions"] + 1
    assert "live-after-cutover" in hot_prediction_ids(world.store)


def test_close_reopen_service_replays_migrated_runtime_prediction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordClock(LIVE_RECORDED_AT)
    bind_world_record_clock(monkeypatch, clock)
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=clock)
    episode = _runtime_episode(symbol="SPY")
    first = _world_runtime(store).capture_and_predict((episode,), now=LIVE_NOW)
    assert first["errors"] == []
    assert first["predictions_appended"] == 1
    rows = store.list_predictions()
    assert len(rows) == 1
    fingerprint = (
        rows[0]["prediction_id"],
        rows[0]["input_sha256"],
        rows[0]["prediction_sha256"],
        rows[0]["payload_sha256"],
    )
    original_payload = deepcopy(rows[0]["prediction_record"])
    world = SeededWorld(
        store=store,
        path=path,
        clock=clock,
        payloads={str(rows[0]["prediction_id"]): original_payload},
        episodes={"live": episode.to_dict()},
        before_predictions=rows,
        before_identities=store.list_prediction_identities(),
        before_counts=store.counts(),
        before_reader_rows=read_sqlite_prediction_dicts(sqlite_connection(store), READER_COLUMNS),
    )
    migrate_world(world, mode="all_cold", before=RUNTIME_ARCHIVE_BEFORE, clock=RUNTIME_ARCHIVE_CLOCK)
    assert store.append_prediction(deepcopy(original_payload)) is False
    store.close()

    restarted = WorldModelStore(path, clock=clock)
    try:
        clock.set(LIVE_REPLAY_AT)
        replay = _world_runtime(restarted).capture_and_predict((episode,), now=LIVE_REPLAY_AT)
        replay_rows = restarted.list_predictions()
        assert replay["errors"] == []
        assert replay["predictions_existing"] == 1
        assert replay["predictions_appended"] == 0
        assert len(replay_rows) == 1
        assert (
            replay_rows[0]["prediction_id"],
            replay_rows[0]["input_sha256"],
            replay_rows[0]["prediction_sha256"],
            replay_rows[0]["payload_sha256"],
        ) == fingerprint
        other = _runtime_episode(symbol="NVDA", at=LIVE_NEXT_WRITE_AT)
        clock.set(LIVE_NEXT_WRITE_AT)
        created = _world_runtime(restarted).capture_and_predict((other,), now=LIVE_NEXT_WRITE_AT)
        assert created["errors"] == []
        assert created["predictions_appended"] == 1
        assert created["predictions_existing"] == 0
        union_ids = {row["prediction_id"] for row in restarted.list_predictions()}
        assert fingerprint[0] in union_ids
        assert len(union_ids) == 2
    finally:
        restarted.close()


def test_identity_hydration_failure_still_protects_archived_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordClock(LIVE_RECORDED_AT)
    bind_world_record_clock(monkeypatch, clock)
    path = tmp_path / "world_model.db"

    class CountingStore(WorldModelStore):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.identity_calls = 0
            self.fail_next_identities = False

        def list_prediction_identities(self) -> list[dict[str, Any]]:
            self.identity_calls += 1
            if self.fail_next_identities:
                self.fail_next_identities = False
                raise RuntimeError("hydrate-failed")
            return super().list_prediction_identities()

    store = CountingStore(path, clock=clock)
    episode = _runtime_episode(symbol="SPY")
    first = _world_runtime(store).capture_and_predict((episode,), now=LIVE_NOW)
    assert first["predictions_appended"] == 1
    payload = store.list_predictions()[0]
    original = deepcopy(payload["prediction_record"])
    world = SeededWorld(
        store=store,
        path=path,
        clock=clock,
        payloads={str(payload["prediction_id"]): original},
        episodes={"live": episode.to_dict()},
        before_predictions=store.list_predictions(),
        before_identities=store.list_prediction_identities(),
        before_counts=store.counts(),
        before_reader_rows=read_sqlite_prediction_dicts(sqlite_connection(store), READER_COLUMNS),
    )
    migrate_world(world, mode="all_cold", before=RUNTIME_ARCHIVE_BEFORE, clock=RUNTIME_ARCHIVE_CLOCK)
    store.fail_next_identities = True
    clock.set(LIVE_REPLAY_AT)
    failed = _world_runtime(store).capture_and_predict((episode,), now=LIVE_REPLAY_AT)
    assert any(error["stage"] == "list_prediction_identities" for error in failed["errors"])
    assert store.append_prediction(deepcopy(original)) is False
    assert store.append_legacy_prediction(deepcopy(original)) is False
    conflict = deepcopy(original)
    nested = dict(conflict["prediction"])
    nested["predicted_class"] = "DOWN"
    conflict["prediction"] = nested
    with pytest.raises(WorldModelConflictError):
        store.append_prediction(conflict)
    store.close()


def test_cold_unavailable_is_not_absent(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="all_cold")
    connection = sqlite_connection(world.store)
    archived_id = "p-early-4h"
    assert cold_prediction_for_replay(connection, archived_id) is not None
    assert cold_prediction_for_replay(connection, "prediction-missing") is None
    target = parquet_path(world.path, world.partitions[0][1])
    original = target.read_bytes()
    target.unlink()
    with pytest.raises(PredictionReadUnavailable):
        cold_prediction_for_replay(connection, archived_id)
    with pytest.raises(PredictionReadUnavailable):
        world.store.append_legacy_prediction(world.payloads[archived_id])
    assert archived_id not in hot_prediction_ids(world.store)
    target.write_bytes(original)
    restored = cold_prediction_for_replay(connection, archived_id)
    assert restored is not None
    assert world.store.append_legacy_prediction(world.payloads[archived_id]) is False


def test_missing_corrupt_cold_raises_unavailable_then_recovers(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="all_cold")
    connection = sqlite_connection(world.store)
    warmed = read_prediction_rows(connection, columns=("prediction_id", "payload_json"), order="recorded")
    assert [row["prediction_id"] for row in warmed] == [
        row["prediction_id"] for row in sorted(world.before_reader_rows, key=recorded_sort_key)
    ]
    target = parquet_path(world.path, world.partitions[0][1])
    original = target.read_bytes()
    target.write_bytes(original[:32] + b"tampered-cold-bytes")
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_rows(connection, columns=("prediction_id", "payload_json"), order="recorded")
    with pytest.raises(PredictionReadUnavailable):
        world.store.list_predictions()
    with pytest.raises(PredictionReadUnavailable):
        prediction_count(connection)
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_identities(connection)
    with pytest.raises(PredictionReadUnavailable):
        world.store.list_prediction_identities()
    target.write_bytes(original)
    recovered = read_prediction_rows(connection, columns=("prediction_id", "payload_json"), order="recorded")
    assert recovered == warmed
    assert_store_parity(world)

    manifest = manifest_path(world.path, world.partitions[-1][1])
    preserved_manifest = manifest.read_text(encoding="utf-8")
    manifest.unlink()
    missing_id = world.partitions[-1][2][0][world.partitions[-1][1].column_names.index("prediction_id")]
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_rows(
            connection,
            columns=("prediction_id",),
            prediction_ids=[missing_id],
        )
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_identities(connection)
    with pytest.raises(PredictionReadUnavailable):
        prediction_count(connection)
    manifest.write_text(preserved_manifest, encoding="utf-8")
    assert read_prediction_rows(connection, columns=("prediction_id",), order="recorded")
    assert read_prediction_identities(connection) == world.before_identities
    assert prediction_count(connection) == world.before_counts["predictions"]


def test_identities_unavailable_when_parquet_missing_then_recover(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="all_cold")
    connection = sqlite_connection(world.store)
    originals = []
    for _plan, manifest, _rows in world.partitions:
        target = parquet_path(world.path, manifest)
        originals.append((target, target.read_bytes()))
        target.unlink()
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_identities(connection)
    with pytest.raises(PredictionReadUnavailable):
        world.store.list_prediction_identities()
    with pytest.raises(PredictionReadUnavailable):
        prediction_count(connection)
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_rows(connection, columns=("prediction_id", "payload_json"))
    for target, payload in originals:
        target.write_bytes(payload)
    assert read_prediction_identities(connection) == world.before_identities
    assert world.store.list_prediction_identities() == world.before_identities
    assert prediction_count(connection) == world.before_counts["predictions"]
    assert_store_parity(world)


def test_prediction_count_validates_storage_availability(seeded_world: SeededWorld) -> None:
    world = migrate_world(seeded_world, mode="mixed")
    connection = sqlite_connection(world.store)
    assert prediction_count(connection) == world.before_counts["predictions"]
    target = parquet_path(world.path, next(item[1] for item in world.partitions if item[0].recorded_date == date(2026, 8, 24)))
    original = target.read_bytes()
    target.write_bytes(b"not-a-parquet")
    with pytest.raises(PredictionReadUnavailable):
        prediction_count(connection)
    with pytest.raises(PredictionReadUnavailable):
        read_prediction_identities(connection)
    target.write_bytes(original)
    assert prediction_count(connection) == world.before_counts["predictions"]
    assert read_prediction_identities(connection) == world.before_identities


def test_count_and_identities_do_not_hydrate_parquet_payloads(
    seeded_world: SeededWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = migrate_world(seeded_world, mode="all_cold")
    connection = sqlite_connection(world.store)
    assert_store_parity(world)
    warmed = read_prediction_rows(connection, columns=("prediction_id", "payload_json"), order="recorded")
    assert [row["prediction_id"] for row in warmed] == [
        row["prediction_id"] for row in sorted(world.before_reader_rows, key=recorded_sort_key)
    ]

    def refuse_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("count/identities must not hydrate Parquet via duckdb.connect")

    monkeypatch.setattr("duckdb.connect", refuse_connect)
    monkeypatch.setattr("trader.infrastructure.state_db.world_prediction_reader.duckdb.connect", refuse_connect)
    assert prediction_count(connection) == world.before_counts["predictions"]
    assert read_prediction_identities(connection) == world.before_identities
    assert world.store.counts()["predictions"] == world.before_counts["predictions"]
    assert world.store.list_prediction_identities() == world.before_identities


def test_unknown_id_append_refuses_when_any_cold_file_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordClock(LIVE_RECORDED_AT)
    bind_world_record_clock(monkeypatch, clock)
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=clock)
    episode = _runtime_episode(symbol="SPY")
    try:
        first = _world_runtime(store).capture_and_predict((episode,), now=LIVE_NOW)
        assert first["errors"] == []
        assert first["predictions_appended"] == 1
        row = store.list_predictions()[0]
        original = deepcopy(row["prediction_record"])
        world = SeededWorld(
            store=store,
            path=path,
            clock=clock,
            payloads={str(row["prediction_id"]): original},
            episodes={"live": episode.to_dict()},
            before_predictions=store.list_predictions(),
            before_identities=store.list_prediction_identities(),
            before_counts=store.counts(),
            before_reader_rows=read_sqlite_prediction_dicts(sqlite_connection(store), READER_COLUMNS),
        )
        migrate_world(world, mode="all_cold", before=RUNTIME_ARCHIVE_BEFORE, clock=RUNTIME_ARCHIVE_CLOCK)
        target = parquet_path(path, world.partitions[0][1])
        original_bytes = target.read_bytes()
        target.unlink()
        clock.set(LIVE_REPLAY_AT)
        unknown_legacy = deepcopy(original)
        unknown_legacy["prediction_id"] = "unknown-legacy-same-tuple"
        unknown_live = deepcopy(original)
        unknown_live["prediction_id"] = "unknown-live-same-tuple"
        hot_before = hot_prediction_ids(store)
        with pytest.raises(PredictionReadUnavailable):
            store.append_legacy_prediction(unknown_legacy)
        with pytest.raises(PredictionReadUnavailable):
            store.append_prediction(unknown_live)
        with pytest.raises(PredictionReadUnavailable):
            store.append_prediction(_live_prediction_mapping(episode, prediction_id="unknown-live-mapping"))
        replay_missing = _world_runtime(store).capture_and_predict((episode,), now=LIVE_REPLAY_AT)
        assert hot_prediction_ids(store) == hot_before
        assert replay_missing["predictions_appended"] == 0
        target.write_bytes(original_bytes)
        assert store.append_prediction(deepcopy(original)) is False
        restored_replay = _world_runtime(store).capture_and_predict((episode,), now=LIVE_REPLAY_AT)
        assert restored_replay["errors"] == []
        assert restored_replay["predictions_appended"] == 0
        assert restored_replay["predictions_existing"] == 1
        assert store.append_legacy_prediction(unknown_legacy) is True
        assert store.append_prediction(unknown_live) is True
        assert {"unknown-legacy-same-tuple", "unknown-live-same-tuple"} <= hot_prediction_ids(store)
    finally:
        store.close()


def test_operator_prepared_candidate_preserves_full_logical_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = open_seeded_world(tmp_path, monkeypatch)
    candidate_store: WorldModelStore | None = None
    source_store: WorldModelStore | None = None
    try:
        before_predictions = world.store.list_predictions()
        before_identities = world.store.list_prediction_identities()
        before_counts = world.store.counts()
        before_ledger = read_world_model_ledger(world.path)
        source_path = world.path
        world.store.close()

        report = prepare_prediction_cutover(
            tmp_path,
            ARCHIVE_BEFORE,
            apply=True,
            adopt=False,
            clock=lambda: ARCHIVE_NOW,
        )
        assert report["ok"] is True
        assert report["mode"] == "prepare"
        assert isinstance(report["candidate_path"], str)
        assert isinstance(report["backup_path"], str)
        assert isinstance(report["source_path"], str)
        assert report["source_retained"] is True
        parity = report["parity"]
        assert isinstance(parity, dict) and parity

        candidate_path = Path(report["candidate_path"])
        backup_path = Path(report["backup_path"])
        assert candidate_path.is_file()
        assert backup_path.is_file()
        assert candidate_path != source_path
        assert candidate_path.parent == source_path.parent
        assert Path(report["source_path"]) == source_path
        assert archive_root_for(candidate_path) == archive_root_for(source_path)
        assert archive_root_for(candidate_path).is_dir()

        candidate_store = WorldModelStore(candidate_path)
        assert candidate_store.list_predictions() == before_predictions
        assert candidate_store.list_prediction_identities() == before_identities
        assert candidate_store.counts() == before_counts
        candidate_connection = sqlite_connection(candidate_store)
        assert int(candidate_connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0]) == 0
        assert int(candidate_connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0]) == (
            before_counts["predictions"]
        )
        assert prediction_count(candidate_connection) == before_counts["predictions"]
        assert read_world_model_ledger(candidate_path) == before_ledger

        source_store = WorldModelStore(source_path, clock=world.clock)
        assert source_store.list_predictions() == before_predictions
        assert source_store.list_prediction_identities() == before_identities
        assert source_store.counts() == before_counts
        assert len(hot_prediction_ids(source_store)) == 8
        assert prediction_count(sqlite_connection(source_store)) == before_counts["predictions"]
    finally:
        if candidate_store is not None:
            candidate_store.close()
        if source_store is not None:
            source_store.close()
        world.close()


def test_export_manifests_keep_source_retained_after_activation(migrated_world: SeededWorld) -> None:
    world = migrated_world
    assert world.archive_report is not None
    assert world.archive_report["source_retained"] is True
    for _plan, manifest, _rows in world.partitions:
        assert manifest.source_retained is True
        payload = json.loads(manifest_path(world.path, manifest).read_text(encoding="utf-8"))
        assert payload["source_retained"] is True
        assert payload["authority"] == "shadow_only"
        assert payload["decision_effect"] == "none"


def _world_runtime(store: WorldModelStore) -> WorldModelService:
    return WorldModelService(
        store=store,
        predictor=HierarchicalDirichletWorldBaseline(),
        labeler=None,
        bar_provider=None,
        horizons=(HORIZON_4H,),
    )


def _live_prediction_mapping(episode: WorldEpisode, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "prediction_id": "live-hash",
        "run_id": "world_shadow.v1",
        "episode_id": episode.episode_id,
        "horizon_code": HORIZON_4H,
        "model_kind": "fixture",
        "model_version": "v1",
        "predicted_at": LIVE_NOW.isoformat(),
        "feature_hash": episode.observation.feature_hash,
        "input_sha256": episode.observation.feature_hash,
        "prediction": {
            "probabilities": {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            "predicted_class": "UP",
            "status": "shadow_only",
            "recommendation": "NO_GO",
            "authority": "shadow_only",
            "decision_effect": "none",
        },
    }
    payload.update(overrides)
    return payload


def _runtime_episode(*, symbol: str, at: datetime = LIVE_NOW) -> WorldEpisode:
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol=symbol,
            bar_interval="1h",
            as_of_bar_ts=at,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=at,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=at,
            captured_at=at + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={"asset_family": "equities", "venue": "US"},
            numeric_features={"return": 0.01, "atr_pct": 0.02},
        )
    )
