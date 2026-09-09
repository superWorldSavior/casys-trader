"""Hot/cold World prediction reader. Callers must hold a storage lease.

These helpers do not acquire ``prediction_storage_lease``. The QUERY caller
must hold a shared lease spanning SQLite open, the read transaction, and close.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from trader.domain.world_prediction_archive import WorldPredictionArchiveManifest
from trader.infrastructure.state_db.sqlite_in import sqlite_in_chunks, sqlite_placeholders


def read_prediction_rows(
    connection: sqlite3.Connection | sqlite3.Cursor,
    *,
    columns: Sequence[str],
    study_cohort_id: str | None = None,
    prediction_ids: Sequence[str] | None = None,
    episode_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    horizon_code: str | None = None,
    order: str = "recorded",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return requested prediction columns as dictionaries.

    Callers must hold a shared prediction storage lease spanning SQLite open,
    transaction, and close. Metadata is selected and ordered in SQLite before
    any Parquet payload hydration.
    """

    selected = _requested_columns(columns)
    if order not in {"recorded", "episode"}:
        raise ValueError("order must be 'recorded' or 'episode'")
    limit_value = _normalize_limit(limit)
    if prediction_ids is not None and _empty_id_filter(prediction_ids):
        return []
    if episode_ids is not None and _empty_id_filter(episode_ids):
        return []
    executor = _executor(connection)
    try:
        if cold_catalog_present(executor):
            validate_registered_cold_storage(executor)
            _assert_no_divergent_duplicates(executor)
            _assert_no_divergent_json_duplicates(executor)
        metadata = _select_metadata(
            executor,
            requested=selected,
            study_cohort_id=study_cohort_id,
            prediction_ids=prediction_ids,
            episode_ids=episode_ids,
            run_id=run_id,
            horizon_code=horizon_code,
            order=order,
            limit=limit_value,
        )
        return _hydrate_payloads(executor, metadata, selected)
    except PredictionReadUnavailable:
        raise
    except (OSError, sqlite3.Error, duckdb.Error, json.JSONDecodeError) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def read_prediction_identities(connection: sqlite3.Connection | sqlite3.Cursor) -> list[dict[str, Any]]:
    """Return distinct business-tuple identities without hydrating Parquet payloads.

    Callers must hold a shared prediction storage lease spanning SQLite open,
    transaction, and close. Registered canonical files are still validated.
    """

    executor = _executor(connection)
    try:
        if cold_catalog_present(executor):
            validate_registered_cold_storage(executor)
            _assert_no_divergent_duplicates(executor)
            sql = (
                "SELECT episode_id, horizon_code, model_kind, model_version FROM ("
                "SELECT episode_id, horizon_code, model_kind, model_version FROM world_shadow_predictions "
                "UNION "
                "SELECT episode_id, horizon_code, model_kind, model_version FROM world_prediction_cold_index"
                ") ORDER BY episode_id, horizon_code, model_kind, model_version"
            )
        else:
            sql = (
                "SELECT DISTINCT episode_id, horizon_code, model_kind, model_version "
                "FROM world_shadow_predictions "
                "ORDER BY episode_id, horizon_code, model_kind, model_version"
            )
        rows = list(executor.execute(sql))
        return [
            {
                "episode_id": _row_get(row, "episode_id", 0),
                "horizon_code": _row_get(row, "horizon_code", 1),
                "model_kind": _row_get(row, "model_kind", 2),
                "model_version": _row_get(row, "model_version", 3),
            }
            for row in rows
        ]
    except PredictionReadUnavailable:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def prediction_count(connection: sqlite3.Connection | sqlite3.Cursor) -> int:
    """Logical unique ``prediction_id`` count after validating cold availability.

    Callers must hold a shared prediction storage lease spanning SQLite open,
    transaction, and close.
    """

    executor = _executor(connection)
    try:
        if cold_catalog_present(executor):
            validate_registered_cold_storage(executor)
            _assert_no_divergent_duplicates(executor)
            row = executor.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT prediction_id FROM world_shadow_predictions "
                "UNION "
                "SELECT prediction_id FROM world_prediction_cold_index"
                ")"
            ).fetchone()
        else:
            row = executor.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()
        return int(row[0])
    except PredictionReadUnavailable:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def cold_prediction_for_replay(
    connection: sqlite3.Connection | sqlite3.Cursor,
    prediction_id: str,
) -> dict[str, Any] | None:
    """Return indexed cold metadata for one id after validating its partition.

    Distinguishes absent (``None``) from registered-but-unavailable (raises
    ``PredictionReadUnavailable``). Callers must hold a storage lease if they
    opened SQLite themselves.
    """

    if not isinstance(prediction_id, str) or not prediction_id.strip():
        raise ValueError("prediction_id must be a non-empty string")
    executor = _executor(connection)
    try:
        if not cold_catalog_present(executor):
            return None
        validate_registered_cold_storage(executor)
        index_names = ("partition_id", *COLD_INDEX_SCALAR_COLUMNS)
        cursor = executor.execute(
            "SELECT "
            + ", ".join(_ident(name) for name in index_names)
            + " FROM world_prediction_cold_index WHERE prediction_id=?",
            (prediction_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        mapping = _map_sql_row(row, cursor.description, index_names)
        partition_id = mapping.get("partition_id")
        partition_cursor = executor.execute(
            "SELECT manifest_json, relative_path FROM world_prediction_cold_partitions WHERE partition_id=?",
            (partition_id,),
        )
        partition = partition_cursor.fetchone()
        if partition is None:
            raise PredictionReadUnavailable(f"cold index references missing partition: {partition_id}")
        partition_map = _map_sql_row(partition, partition_cursor.description, ("manifest_json", "relative_path"))
        try:
            parsed_json = json.loads(str(partition_map["manifest_json"]))
            if not isinstance(parsed_json, dict):
                raise TypeError("manifest_json must be an object")
            manifest = WorldPredictionArchiveManifest.from_mapping(parsed_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PredictionReadUnavailable(f"cold partition registry manifest is invalid: {partition_id}") from exc
        validate_manifest_partition_files(archive_root=world_model_archive_root(executor), manifest=manifest)
        return mapping
    except PredictionReadUnavailable:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def _executor(connection: sqlite3.Connection | sqlite3.Cursor) -> sqlite3.Connection | sqlite3.Cursor:
    if not isinstance(connection, (sqlite3.Connection, sqlite3.Cursor)):
        raise TypeError("connection must be a sqlite3 Connection or Cursor")
    return connection


def _requested_columns(columns: Sequence[str]) -> tuple[str, ...]:
    if isinstance(columns, (str, bytes)) or not isinstance(columns, Sequence):
        raise TypeError("columns must be a sequence of strings")
    selected: list[str] = []
    seen: set[str] = set()
    for name in columns:
        if not isinstance(name, str) or not name:
            raise TypeError("columns must be a sequence of strings")
        if name not in _ALLOWED_COLUMNS:
            raise ValueError(f"unsupported prediction column: {name}")
        if name in seen:
            continue
        seen.add(name)
        selected.append(name)
    if not selected:
        raise ValueError("columns must be a non-empty sequence")
    return tuple(selected)


def _normalize_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be >= 0")
    if limit < 0:
        raise ValueError("limit must be >= 0")
    return limit


def _empty_id_filter(values: Sequence[str]) -> bool:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("id filters must be a sequence of strings")
    return len(tuple(values)) == 0


def _table_columns(executor: sqlite3.Connection | sqlite3.Cursor, table: str) -> set[str]:
    return {str(row[1]) for row in executor.execute(f"PRAGMA table_info({table})")}


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column_available(
    name: str,
    hot_columns: set[str],
    cold_columns: set[str],
    cold_present: bool,
    *,
    episodes_present: bool,
) -> bool:
    if name == "prediction_id":
        return True
    if name == "episode_observed_at":
        return episodes_present
    if name in PREDICTION_JSON_COLUMNS:
        return name in hot_columns or cold_present
    return name in hot_columns or (cold_present and name in cold_columns)


def _row_get(row: Any, key: str, index: int) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _description_names(description: Any, fallback: Sequence[str] | None = None) -> tuple[str, ...]:
    if description:
        return tuple(str(item[0]) for item in description)
    if fallback is not None:
        return tuple(fallback)
    raise PredictionReadUnavailable("prediction query is missing cursor description")


def _map_sql_row(row: Any, description: Any, fallback: Sequence[str] | None = None) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {str(key): row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return dict(row)
    names = _description_names(description, fallback)
    return {name: value for name, value in zip(names, row, strict=True)}


def _assert_no_divergent_duplicates(executor: sqlite3.Connection | sqlite3.Cursor) -> None:
    hot_columns = _table_columns(executor, "world_shadow_predictions")
    compare = [
        name
        for name in COLD_INDEX_SCALAR_COLUMNS
        if name != "prediction_id" and name in hot_columns
    ]
    if not compare:
        return
    mismatch = " OR ".join(
        f"h.{_ident(name)} IS NOT c.{_ident(name)}" for name in compare
    )
    row = executor.execute(
        "SELECT h.prediction_id FROM world_shadow_predictions AS h "
        "JOIN world_prediction_cold_index AS c ON c.prediction_id = h.prediction_id "
        f"WHERE {mismatch} LIMIT 1"
    ).fetchone()
    if row is not None:
        raise PredictionReadUnavailable(
            f"hot/cold prediction content diverges for {_row_get(row, 'prediction_id', 0)}"
        )


def _assert_no_divergent_json_duplicates(executor: sqlite3.Connection | sqlite3.Cursor) -> None:
    hot_columns = _table_columns(executor, "world_shadow_predictions")
    json_columns = [name for name in PREDICTION_JSON_COLUMNS if name in hot_columns]
    if not json_columns:
        return
    selected = ", ".join(
        ["c.prediction_id", "c.partition_id", *[f"h.{_ident(name)}" for name in json_columns]]
    )
    overlap_cursor = executor.execute(
        "SELECT "
        + selected
        + " FROM world_shadow_predictions AS h "
        + "JOIN world_prediction_cold_index AS c ON c.prediction_id = h.prediction_id"
    )
    overlap_names = ("prediction_id", "partition_id", *json_columns)
    ids_by_partition: dict[str, list[str]] = {}
    hot_json: dict[str, dict[str, Any]] = {}
    for raw in overlap_cursor:
        mapping = _map_sql_row(raw, overlap_cursor.description, overlap_names)
        prediction_id = mapping.get("prediction_id")
        partition_id = mapping.get("partition_id")
        if not prediction_id or not partition_id:
            raise PredictionReadUnavailable("hot/cold overlap is missing partition identity")
        prediction_key = str(prediction_id)
        ids_by_partition.setdefault(str(partition_id), []).append(prediction_key)
        hot_json[prediction_key] = {name: mapping.get(name) for name in json_columns}
    if not ids_by_partition:
        return
    cold_json = _read_partition_payloads(executor, ids_by_partition, json_columns)
    for prediction_id, hot_values in hot_json.items():
        cold_values = cold_json.get(prediction_id)
        if cold_values is None:
            raise PredictionReadUnavailable(
                f"canonical parquet is missing overlapping prediction_id {prediction_id}"
            )
        for name in json_columns:
            if hot_values.get(name) != cold_values.get(name):
                raise PredictionReadUnavailable(
                    f"hot/cold prediction JSON diverges for {prediction_id}"
                )


def _select_metadata(
    executor: sqlite3.Connection | sqlite3.Cursor,
    *,
    requested: tuple[str, ...],
    study_cohort_id: str | None,
    prediction_ids: Sequence[str] | None,
    episode_ids: Sequence[str] | None,
    run_id: str | None,
    horizon_code: str | None,
    order: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    hot_columns = _table_columns(executor, "world_shadow_predictions")
    cold_present = cold_catalog_present(executor)
    cold_columns = _table_columns(executor, "world_prediction_cold_index") if cold_present else set()
    episodes_present = "world_episodes" in _table_names(executor)
    requested = tuple(
        name
        for name in requested
        if _column_available(
            name, hot_columns, cold_columns, cold_present, episodes_present=episodes_present
        )
    )
    extra_binds, unbounded_in_count = _metadata_bind_budget(
        cold_present=cold_present,
        study_cohort_id=study_cohort_id,
        run_id=run_id,
        horizon_code=horizon_code,
        prediction_ids=prediction_ids,
        episode_ids=episode_ids,
        limit=limit,
    )
    chunks = list(
        _filter_chunks(
            prediction_ids,
            episode_ids,
            extra_binds=extra_binds,
            unbounded_in_count=unbounded_in_count,
        )
    )
    if not chunks:
        return []
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    push_limit = len(chunks) == 1
    for pred_chunk, episode_chunk in chunks:
        sql, params = _metadata_sql(
            requested=requested,
            hot_columns=hot_columns,
            cold_columns=cold_columns,
            cold_present=cold_present,
            episodes_present=episodes_present,
            study_cohort_id=study_cohort_id,
            prediction_ids=pred_chunk,
            episode_ids=episode_chunk,
            run_id=run_id,
            horizon_code=horizon_code,
            order=order,
            limit=limit if push_limit else None,
        )
        cursor = executor.execute(sql, params)
        for row in cursor:
            mapped = _map_sql_row(row, cursor.description)
            prediction_id = mapped.get("prediction_id")
            if prediction_id in seen_ids:
                continue
            if prediction_id is not None:
                seen_ids.add(str(prediction_id))
            merged.append(mapped)
    if not push_limit:
        merged.sort(key=lambda row: _sqlite_sort_key(row, order))
        if limit is not None:
            merged = merged[:limit]
    return merged


def _table_names(executor: sqlite3.Connection | sqlite3.Cursor) -> set[str]:
    return {str(row[0]) for row in executor.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _metadata_bind_budget(
    *,
    cold_present: bool,
    study_cohort_id: str | None,
    run_id: str | None,
    horizon_code: str | None,
    prediction_ids: Sequence[str] | None,
    episode_ids: Sequence[str] | None,
    limit: int | None,
) -> tuple[int, int]:
    # HOT+COLD UNION repeats each filter; LIMIT is bound once on the outer query.
    tier_multiplier = 2 if cold_present else 1
    scalar_binds = sum(1 for value in (study_cohort_id, run_id, horizon_code) if value is not None)
    extra_binds = scalar_binds * tier_multiplier + (1 if limit is not None else 0)
    id_lists = int(prediction_ids is not None) + int(episode_ids is not None)
    unbounded_in_count = max(1, id_lists) * tier_multiplier
    return extra_binds, unbounded_in_count


def _filter_chunks(
    prediction_ids: Sequence[str] | None,
    episode_ids: Sequence[str] | None,
    *,
    extra_binds: int,
    unbounded_in_count: int,
) -> Any:
    if prediction_ids is None and episode_ids is None:
        yield None, None
        return
    if prediction_ids is not None and episode_ids is not None:
        pred_chunks = sqlite_in_chunks(
            prediction_ids, unbounded_in_count=unbounded_in_count, extra_binds=extra_binds
        )
        episode_chunks = sqlite_in_chunks(
            episode_ids, unbounded_in_count=unbounded_in_count, extra_binds=extra_binds
        )
        if not pred_chunks or not episode_chunks:
            return
        for pred_chunk in pred_chunks:
            for episode_chunk in episode_chunks:
                yield pred_chunk, episode_chunk
        return
    if prediction_ids is not None:
        chunks = sqlite_in_chunks(
            prediction_ids, unbounded_in_count=unbounded_in_count, extra_binds=extra_binds
        )
        if not chunks:
            return
        for chunk in chunks:
            yield chunk, None
        return
    chunks = sqlite_in_chunks(
        episode_ids or (), unbounded_in_count=unbounded_in_count, extra_binds=extra_binds
    )
    if not chunks:
        return
    for chunk in chunks:
        yield None, chunk


def _metadata_sql(
    *,
    requested: tuple[str, ...],
    hot_columns: set[str],
    cold_columns: set[str],
    cold_present: bool,
    episodes_present: bool,
    study_cohort_id: str | None,
    prediction_ids: Sequence[str] | None,
    episode_ids: Sequence[str] | None,
    run_id: str | None,
    horizon_code: str | None,
    order: str,
    limit: int | None,
) -> tuple[str, tuple[Any, ...]]:
    need_observed = order == "episode" or "episode_observed_at" in requested
    select_internal = ("prediction_id", "recorded_at", "run_id", "episode_id")
    if need_observed:
        select_internal = (*select_internal, "episode_observed_at")
    select_names = list(dict.fromkeys((*select_internal, *requested, "partition_id", "_from_cold")))
    use_join = need_observed and episodes_present
    hot_select = []
    cold_select = []
    for name in select_names:
        hot_select.append(_hot_expr(name, hot_columns, use_join=use_join))
        cold_select.append(_cold_expr(name, cold_columns, cold_present, use_join=use_join))
    hot_where, hot_params = _where_clause(
        alias="p",
        available=hot_columns,
        study_cohort_id=study_cohort_id,
        prediction_ids=prediction_ids,
        episode_ids=episode_ids,
        run_id=run_id,
        horizon_code=horizon_code,
    )
    hot_join = " LEFT JOIN world_episodes AS e ON e.episode_id = p.episode_id" if use_join else ""
    hot_sql = "SELECT " + ", ".join(hot_select) + " FROM world_shadow_predictions AS p " + hot_join + hot_where
    parts = [hot_sql]
    params: list[Any] = list(hot_params)
    if cold_present:
        cold_where, cold_params = _where_clause(
            alias="c",
            available=cold_columns,
            study_cohort_id=study_cohort_id,
            prediction_ids=prediction_ids,
            episode_ids=episode_ids,
            run_id=run_id,
            horizon_code=horizon_code,
        )
        extra = " AND NOT EXISTS (SELECT 1 FROM world_shadow_predictions AS h WHERE h.prediction_id = c.prediction_id)"
        if cold_where:
            cold_where = cold_where + extra
        else:
            cold_where = " WHERE" + extra[4:]
        cold_join = " LEFT JOIN world_episodes AS e ON e.episode_id = c.episode_id" if use_join else ""
        cold_sql = (
            "SELECT "
            + ", ".join(cold_select)
            + " FROM world_prediction_cold_index AS c "
            + cold_join
            + cold_where
        )
        parts.append(cold_sql)
        params.extend(cold_params)
    union = " UNION ALL ".join(parts)
    wrapped = f"SELECT * FROM ({union})"
    if order == "recorded":
        wrapped += " ORDER BY recorded_at, prediction_id"
    else:
        wrapped += " ORDER BY run_id, episode_observed_at, episode_id, prediction_id"
    if limit is not None:
        wrapped += " LIMIT ?"
        params.append(limit)
    return wrapped, tuple(params)


def _hot_expr(name: str, hot_columns: set[str], *, use_join: bool) -> str:
    if name == "episode_observed_at":
        if use_join:
            return "e.observed_at AS episode_observed_at"
        return "NULL AS episode_observed_at"
    if name == "partition_id":
        return "NULL AS partition_id"
    if name == "_from_cold":
        return "0 AS _from_cold"
    if name in PREDICTION_JSON_COLUMNS:
        if name in hot_columns:
            return f"p.{_ident(name)} AS {_ident(name)}"
        return f"NULL AS {_ident(name)}"
    if name in hot_columns:
        return f"p.{_ident(name)} AS {_ident(name)}"
    return f"NULL AS {_ident(name)}"


def _cold_expr(name: str, cold_columns: set[str], cold_present: bool, *, use_join: bool) -> str:
    if name == "episode_observed_at":
        if use_join:
            return "e.observed_at AS episode_observed_at"
        return "NULL AS episode_observed_at"
    if name == "partition_id":
        return "c.partition_id AS partition_id"
    if name == "_from_cold":
        return "1 AS _from_cold"
    if name in PREDICTION_JSON_COLUMNS:
        return f"NULL AS {_ident(name)}"
    if cold_present and name in cold_columns:
        return f"c.{_ident(name)} AS {_ident(name)}"
    return f"NULL AS {_ident(name)}"


def _where_clause(
    *,
    alias: str,
    available: set[str],
    study_cohort_id: str | None,
    prediction_ids: Sequence[str] | None,
    episode_ids: Sequence[str] | None,
    run_id: str | None,
    horizon_code: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if study_cohort_id is not None:
        if "study_cohort_id" not in available:
            clauses.append("0")
        else:
            clauses.append(f"{alias}.{_ident('study_cohort_id')}=?")
            params.append(study_cohort_id)
    if run_id is not None:
        clauses.append(f"{alias}.{_ident('run_id')}=?")
        params.append(run_id)
    if horizon_code is not None:
        clauses.append(f"{alias}.{_ident('horizon_code')}=?")
        params.append(horizon_code)
    if prediction_ids is not None:
        clauses.append(f"{alias}.{_ident('prediction_id')} IN ({sqlite_placeholders(len(prediction_ids))})")
        params.extend(prediction_ids)
    if episode_ids is not None:
        clauses.append(f"{alias}.{_ident('episode_id')} IN ({sqlite_placeholders(len(episode_ids))})")
        params.extend(episode_ids)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _sqlite_sort_key(row: Mapping[str, Any], order: str) -> tuple[Any, ...]:
    names = ("recorded_at", "prediction_id") if order == "recorded" else (
        "run_id",
        "episode_observed_at",
        "episode_id",
        "prediction_id",
    )
    key = []
    for name in names:
        value = row.get(name)
        key.append((0 if value is None else 1, "" if value is None else value))
    return tuple(key)


def _hydrate_payloads(
    executor: sqlite3.Connection | sqlite3.Cursor,
    rows: list[dict[str, Any]],
    requested: tuple[str, ...],
) -> list[dict[str, Any]]:
    payload_columns = [name for name in requested if name in PREDICTION_JSON_COLUMNS]
    cold_ids_by_partition: dict[str, list[str]] = {}
    if payload_columns:
        for row in rows:
            if int(row.get("_from_cold") or 0) != 1:
                continue
            partition_id = row.get("partition_id")
            prediction_id = row.get("prediction_id")
            if not partition_id or not prediction_id:
                raise PredictionReadUnavailable("cold prediction is missing partition identity")
            cold_ids_by_partition.setdefault(str(partition_id), []).append(str(prediction_id))
    payloads: dict[str, dict[str, Any]] = {}
    if cold_ids_by_partition:
        payloads = _read_partition_payloads(executor, cold_ids_by_partition, payload_columns)
    results: list[dict[str, Any]] = []
    for row in rows:
        prediction_id = row.get("prediction_id")
        if int(row.get("_from_cold") or 0) == 1 and payload_columns:
            hydrated = payloads.get(str(prediction_id))
            if hydrated is None:
                raise PredictionReadUnavailable(f"canonical parquet is missing prediction_id {prediction_id}")
            for name in payload_columns:
                row[name] = hydrated.get(name)
        output = {name: row[name] for name in requested if name in row}
        results.append(output)
    return results


def _read_partition_payloads(
    executor: sqlite3.Connection | sqlite3.Cursor,
    ids_by_partition: dict[str, list[str]],
    payload_columns: list[str],
) -> dict[str, dict[str, Any]]:
    archive_root = world_model_archive_root(executor)
    payloads: dict[str, dict[str, Any]] = {}
    for partition_id, prediction_ids in ids_by_partition.items():
        row = executor.execute(
            "SELECT manifest_json FROM world_prediction_cold_partitions WHERE partition_id=?",
            (partition_id,),
        ).fetchone()
        if row is None:
            raise PredictionReadUnavailable(f"cold index references missing partition: {partition_id}")
        manifest_json = str(_row_get(row, "manifest_json", 0))
        try:
            manifest = WorldPredictionArchiveManifest.from_mapping(json.loads(manifest_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PredictionReadUnavailable(f"cold partition registry manifest is invalid: {partition_id}") from exc
        parquet_path = validate_manifest_partition_files(archive_root=archive_root, manifest=manifest)
        payloads.update(_duckdb_payloads(parquet_path, prediction_ids, payload_columns))
    return payloads


def _duckdb_payloads(
    parquet_path: Path,
    prediction_ids: Sequence[str],
    payload_columns: list[str],
) -> dict[str, dict[str, Any]]:
    selected = ", ".join(["prediction_id", *(_ident(name) for name in payload_columns)])
    wanted = tuple(dict.fromkeys(prediction_ids))
    sql = (
        f"SELECT {selected} FROM read_parquet(?, hive_partitioning=false) "
        "WHERE prediction_id = ANY(?)"
    )
    before = partition_file_identity(parquet_path, what="canonical parquet")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET autoinstall_known_extensions=false")
        connection.execute("SET autoload_known_extensions=false")
        cursor = connection.execute(sql, [str(parquet_path), list(wanted)])
        names = [str(item[0]) for item in cursor.description]
        fetched = cursor.fetchall()
    except Exception as exc:
        raise PredictionReadUnavailable(
            f"canonical parquet is unreadable: {parquet_path}: {exc}"
        ) from exc
    finally:
        connection.close()
    after = partition_file_identity(parquet_path, what="canonical parquet")
    if after != before:
        raise PredictionReadUnavailable(f"canonical parquet changed during read: {parquet_path}")
    found: dict[str, dict[str, Any]] = {}
    for raw in fetched:
        mapping = {name: value for name, value in zip(names, raw, strict=True)}
        prediction_id = mapping.get("prediction_id")
        if prediction_id in (None, ""):
            raise PredictionReadUnavailable(f"canonical parquet row is missing prediction_id: {parquet_path}")
        key = str(prediction_id)
        if key in found:
            raise PredictionReadUnavailable(f"canonical parquet has duplicate prediction_id {key}: {parquet_path}")
        found[key] = mapping
    missing = [item for item in wanted if item not in found]
    if missing:
        raise PredictionReadUnavailable(
            f"canonical parquet is missing prediction_id {missing[0]}: {parquet_path}"
        )
    return found


from trader.infrastructure.state_db.world_prediction_tiers import (  # noqa: E402
    COLD_INDEX_SCALAR_COLUMNS,
    PREDICTION_JSON_COLUMNS,
    PredictionReadUnavailable,
    cold_catalog_present,
    partition_file_identity,
    validate_manifest_partition_files,
    validate_registered_cold_storage,
    world_model_archive_root,
)

_ALLOWED_COLUMNS = frozenset(
    {
        *COLD_INDEX_SCALAR_COLUMNS,
        *PREDICTION_JSON_COLUMNS,
        "episode_observed_at",
        "partition_id",
    }
)

__all__ = [
    "cold_prediction_for_replay",
    "prediction_count",
    "read_prediction_identities",
    "read_prediction_rows",
]
