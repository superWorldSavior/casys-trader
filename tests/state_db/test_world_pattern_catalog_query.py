from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.package_layout._helpers import REPO_ROOT
from tests.state_db.test_world_pattern_store import COHORT_ID, EVAL_FP, _evaluating, _registered, _store
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_pattern import PatternEvaluationStarted
from trader.infrastructure.state_db import world_pattern_catalog_query as catalog_query
from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery


_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_catalog_query.py"


def test_catalog_query_is_readonly_and_never_constructs_the_store() -> None:
    source = _QUERY.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only=ON" in source
    assert "WorldPatternStore(" not in source
    assert "StateDb(" not in source
    assert "apply_current_world_model_schema" not in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source
    assert "world_outcome_events" not in source


def test_missing_database_returns_empty_without_creating(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "world_model.db"
    catalog = SqlitePatternCatalogQuery(missing)
    assert (
        catalog.list_evaluating_hypotheses(
            evaluation_cohort_id=COHORT_ID,
            evaluation_dataset_fingerprint=EVAL_FP,
        )
        == ()
    )
    assert catalog.list_recorded_occurrences() == ()
    assert not missing.exists()


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


def _traced_readonly(path: Path, statements: list[str], opens: list[Path]) -> sqlite3.Connection:
    opens.append(path)
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    connection.set_trace_callback(lambda sql: statements.append(sql))
    return connection


def test_selected_hypothesis_ids_filter_in_sql_on_one_readonly_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    first = _evaluating()
    second = _evaluating(_registered(formation_dataset_fingerprint=canonical_sha256({"dataset": "other-formation"})))
    assert first.hypothesis_id != second.hypothesis_id
    for hypothesis in (first, second):
        store.append_event(hypothesis.registered)
        store.append_event(next(event for event in hypothesis.events if isinstance(event, PatternEvaluationStarted)))

    catalog = SqlitePatternCatalogQuery(store.path)
    assert catalog.list_hypothesis_ids() == tuple(sorted((first.hypothesis_id, second.hypothesis_id)))
    expected_ids = sorted((first.hypothesis_id, second.hypothesis_id))
    assert [item.hypothesis_id for item in store.list_hypotheses()] == expected_ids
    statements: list[str] = []
    opens: list[Path] = []
    monkeypatch.setattr(
        catalog_query,
        "_readonly_connection",
        lambda path: _traced_readonly(path, statements, opens),
    )
    listed = catalog.list_evaluating_hypotheses(
        evaluation_cohort_id=COHORT_ID,
        evaluation_dataset_fingerprint=EVAL_FP,
        hypothesis_ids=(second.hypothesis_id, first.hypothesis_id, first.hypothesis_id),
    )
    assert [item.hypothesis_id for item in listed] == sorted((first.hypothesis_id, second.hypothesis_id))
    selected = catalog.list_evaluating_hypotheses(
        evaluation_cohort_id=COHORT_ID,
        evaluation_dataset_fingerprint=EVAL_FP,
        hypothesis_ids=(first.hypothesis_id,),
    )
    assert [item.hypothesis_id for item in selected] == [first.hypothesis_id]
    assert len(opens) == 2
    compact = [" ".join(sql.split()) for sql in statements]
    assert any("hypothesis_id IN" in sql for sql in compact)
    assert not any("SELECT DISTINCT hypothesis_id FROM world_pattern_hypothesis_events" in sql for sql in compact)
    store.close()


def test_occurrence_in_list_is_chunked_beyond_sqlite_variable_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.close()
    dummy_ids = tuple(f"pattern_occurrence:v1:{index:064x}" for index in range(1, 1101))
    statements: list[str] = []
    opens: list[Path] = []
    monkeypatch.setattr(
        catalog_query,
        "_readonly_connection",
        lambda path: _traced_readonly(path, statements, opens),
    )
    listed = SqlitePatternCatalogQuery(tmp_path / "world_model.db").list_occurrence_ids(occurrence_ids=dummy_ids)
    assert listed == ()
    assert len(opens) == 1
    widths = _in_placeholder_counts(statements, column="occurrence_id")
    assert widths
    assert all(width <= 500 for width in widths)
    assert max(widths) == 500
    assert sum(widths) == 1100
