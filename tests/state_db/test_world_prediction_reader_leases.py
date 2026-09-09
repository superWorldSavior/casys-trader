"""Shared-lease coverage for World Model read-only SQLite adapters."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

from tests.state_db.test_world_pattern_evaluation_query import _scan
from tests.state_db.test_world_pattern_formation_query import _request
from trader.application.world_model.pattern_outcome_link import PatternQueryUnavailable
from trader.infrastructure.state_db import world_graph_explorer_query as explorer_query
from trader.infrastructure.state_db import world_pattern_catalog_query as catalog_query
from trader.infrastructure.state_db import world_pattern_evaluation_query as evaluation_query
from trader.infrastructure.state_db import world_pattern_formation_query as formation_query
from trader.infrastructure.state_db import world_pattern_outcome_query as outcome_query
from trader.infrastructure.state_db import world_resource_probe as probe_mod
from trader.infrastructure.state_db.world_graph_explorer_query import SqliteWorldGraphExplorerQuery
from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery
from trader.infrastructure.state_db.world_pattern_evaluation_query import SqlitePatternEvaluationSource
from trader.infrastructure.state_db.world_pattern_formation_query import SqlitePatternFormationSource
from trader.infrastructure.state_db.world_pattern_outcome_query import SqlitePatternOutcomeLeafQuery
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    PredictionStorageLease,
    cutover_journal_path,
    storage_lock_path,
)
from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe
from trader.reporting.read_models import world_macro_status as macro_mod
from trader.reporting.read_models.world_macro_status import _read_attach


AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)
_BUSY = "exclusive cutover"


class ReaderCase(NamedTuple):
    name: str
    invoke: Callable[[Path], Any]
    busy: str


def _invoke_formation(db_path: Path) -> Any:
    return SqlitePatternFormationSource(db_path).load_formation_batch(_request())


def _invoke_evaluation(db_path: Path) -> Any:
    return SqlitePatternEvaluationSource(db_path).load_evaluation_batch(_scan())


def _invoke_catalog(db_path: Path) -> Any:
    return SqlitePatternCatalogQuery(db_path).list_hypotheses()


def _invoke_outcome(db_path: Path) -> Any:
    return SqlitePatternOutcomeLeafQuery(db_path).load_active_observed_leaves(
        episode_ids=("world-episode:v1:seed",),
        horizon_ids=("elapsed_1d.v1",),
        as_of=AS_OF,
    )


def _invoke_graph(db_path: Path) -> Any:
    return SqliteWorldGraphExplorerQuery(db_path).load_current_records()


def _invoke_probe(db_path: Path) -> Any:
    return FilesystemWorldResourceProbe(db_path).measure()


def _invoke_macro(db_path: Path) -> Any:
    return _read_attach(db_path.parent)


READERS = (
    ReaderCase("formation", _invoke_formation, "batch_unavailable"),
    ReaderCase("evaluation", _invoke_evaluation, "batch_unavailable"),
    ReaderCase("catalog", _invoke_catalog, "empty"),
    ReaderCase("outcome", _invoke_outcome, "raises_pattern"),
    ReaderCase("graph", _invoke_graph, "graph"),
    ReaderCase("probe", _invoke_probe, "raises_busy"),
    ReaderCase("macro", _invoke_macro, "macro"),
)


def _legacy_marker_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    return db_path


def _file_tree(directory: Path) -> dict[str, int]:
    if not directory.exists():
        return {}
    return {item.name: item.stat().st_size for item in directory.iterdir() if item.is_file()}


def _connection_is_closed(connection: sqlite3.Connection) -> bool:
    try:
        sqlite3.Connection.execute(connection, "SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


@contextmanager
def _busy_lease(_path: Path, *, create: bool = False):
    assert create is False
    raise PredictionStorageBusyError(_BUSY)
    yield


def _expect_busy(kind: str, invoke: Callable[[Path], Any], db_path: Path) -> None:
    if kind == "raises_pattern":
        with pytest.raises(PatternQueryUnavailable, match="PredictionStorageBusyError"):
            invoke(db_path)
        return
    if kind == "raises_busy":
        with pytest.raises(PredictionStorageBusyError, match=_BUSY):
            invoke(db_path)
        return
    result = invoke(db_path)
    if kind == "batch_unavailable":
        assert result.records == ()
        assert result.rejection_counts == {"unavailable": 1}
        return
    if kind == "empty":
        assert result == ()
        return
    if kind == "graph":
        assert result.status == "unavailable"
        assert result.exists is True
        assert result.records == ()
        assert str(result.error).startswith("PredictionStorageBusyError:")
        return
    if kind == "macro":
        assert result["status"] == "unavailable"
        assert result["exists"] is True
        assert str(result["error"]).startswith("PredictionStorageBusyError:")
        return
    raise AssertionError(f"unknown busy kind: {kind}")


def _reader_module(name: str) -> Any:
    return {
        "formation": formation_query,
        "evaluation": evaluation_query,
        "catalog": catalog_query,
        "outcome": outcome_query,
        "graph": explorer_query,
        "probe": probe_mod,
        "macro": macro_mod,
    }[name]


def _install_busy_lease(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    monkeypatch.setattr(module, "shared_prediction_storage_lease", _busy_lease, raising=False)


def _install_order_probes(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    live: list[sqlite3.Connection],
    *,
    connect_mode: str,
) -> None:
    real_enter = PredictionStorageLease.__enter__
    real_close = PredictionStorageLease.close
    real_connect = sqlite3.connect

    def enter(self: PredictionStorageLease) -> PredictionStorageLease:
        events.append("lease_enter")
        return real_enter(self)

    def close(self: PredictionStorageLease) -> None:
        if not self._closed:
            assert all(_connection_is_closed(connection) for connection in live)
            events.append("lease_release")
        real_close(self)

    monkeypatch.setattr(PredictionStorageLease, "__enter__", enter)
    monkeypatch.setattr(PredictionStorageLease, "close", close)

    if connect_mode == "connect_error":

        def boom(*_args: object, **_kwargs: object) -> sqlite3.Connection:
            raise sqlite3.OperationalError("cannot open")

        monkeypatch.setattr(sqlite3, "connect", boom)
        return

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
            text = str(sql)
            compact = text.lstrip().upper()
            fail_setup = connect_mode == "setup_error"
            fail_query = connect_mode == "query_error" and (
                compact.startswith("SELECT") or "PAGE_COUNT" in compact
            )
            if fail_setup or fail_query:
                raise sqlite3.DatabaseError("injected query failure")
            return super().execute(sql, *args)

        def close(self) -> None:
            if not getattr(self, "_lease_test_closed", False):
                events.append("db_close")
                self._lease_test_closed = True
            super().close()

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        events.append("db_open")
        live.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)


@pytest.mark.parametrize("reader", READERS, ids=lambda item: item.name)
def test_busy_lease_uses_typed_contract_and_does_not_open_sqlite(
    reader: ReaderCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _legacy_marker_db(tmp_path)
    opens: list[object] = []
    real_connect = sqlite3.connect

    def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
        opens.append(args[0] if args else kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    _install_busy_lease(monkeypatch, _reader_module(reader.name))

    _expect_busy(reader.busy, reader.invoke, db_path)
    assert opens == []
    assert not storage_lock_path(db_path).exists()
    assert not cutover_journal_path(db_path).exists()


@pytest.mark.parametrize("reader", READERS, ids=lambda item: item.name)
@pytest.mark.parametrize("db_kind", ("missing", "existing"))
def test_reads_do_not_create_storage_lock_or_cutover_journal(
    reader: ReaderCase,
    db_kind: str,
    tmp_path: Path,
) -> None:
    if db_kind == "missing":
        db_path = tmp_path / "world_model.db"
    else:
        db_path = _legacy_marker_db(tmp_path)
    before = _file_tree(tmp_path)
    reader.invoke(db_path)
    after = _file_tree(tmp_path)
    assert after == before
    assert not storage_lock_path(db_path).exists()
    assert not cutover_journal_path(db_path).exists()
    if db_kind == "missing":
        assert not db_path.exists()


@pytest.mark.parametrize("reader", READERS, ids=lambda item: item.name)
@pytest.mark.parametrize("connect_mode", ("success", "query_error", "setup_error", "connect_error"))
def test_connection_closes_before_shared_lease_release(
    reader: ReaderCase,
    connect_mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _legacy_marker_db(tmp_path)
    events: list[str] = []
    live: list[sqlite3.Connection] = []
    _install_order_probes(monkeypatch, events, live, connect_mode=connect_mode)

    if connect_mode == "success":
        reader.invoke(db_path)
    else:
        try:
            reader.invoke(db_path)
        except (OSError, sqlite3.Error, PatternQueryUnavailable):
            pass

    if connect_mode == "connect_error":
        assert events == ["lease_enter", "lease_release"]
        assert live == []
        return

    assert events == ["lease_enter", "db_open", "db_close", "lease_release"]
    assert live
    assert all(_connection_is_closed(connection) for connection in live)
