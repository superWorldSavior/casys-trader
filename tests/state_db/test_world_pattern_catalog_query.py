from __future__ import annotations

from pathlib import Path

from tests.package_layout._helpers import REPO_ROOT
from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery


_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_catalog_query.py"
COHORT_ID = "world_cohort:v1:" + "c" * 64
EVAL_FP = "b" * 64


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
    assert catalog.list_evaluating_hypotheses(
        evaluation_cohort_id=COHORT_ID,
        evaluation_dataset_fingerprint=EVAL_FP,
    ) == ()
    assert catalog.list_recorded_occurrences() == ()
    assert not missing.exists()
