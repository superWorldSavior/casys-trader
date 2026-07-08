"""Architecture checks for the SQLite learnings store split."""
from __future__ import annotations

import ast
import inspect


def test_agent_store_facade_reexports_infra_learnings_store() -> None:
    """agent.learnings.store remains a non-breaking facade over infrastructure."""
    from trader.agent.learnings import store as facade
    from trader.infrastructure.state_db.learnings_store import LearningsStore

    assert facade.LearningsStore is LearningsStore
    assert facade.__all__ == ["LearningsStore"]


def test_scoring_module_computes_flair_without_io() -> None:
    """FLAIR scoring is a deterministic pure calculation over row dictionaries."""
    from trader.domain.learnings.scoring import compute_outcome_scores

    rows = [
        {"id": 1, "symbol": "AAA", "family": "tech", "verdict": "WIN"},
        {"id": 2, "symbol": "AAA", "family": "tech", "verdict": "LOSS"},
        {"id": 3, "symbol": "AAA", "family": "tech", "verdict": "WIN"},
        {"id": 4, "symbol": "AAA", "family": "tech", "verdict": "LOSS"},
        {"id": 5, "symbol": "AAA", "family": "tech", "verdict": "WIN"},
        {"id": 6, "symbol": "BBB", "family": "tech", "verdict": "NEUTRAL"},
    ]

    result = compute_outcome_scores(rows, shrinkage_k=5.0)

    assert result["scored"] == 6
    assert result["base_rates"] == {"AAA": 0.6}
    assert result["scores"][1] == (1.0 - 0.6) / 6.0
    assert result["scores"][2] == (0.0 - 0.6) / 6.0
    assert result["scores"][6] == 0.0


def test_scoring_module_has_no_io_imports() -> None:
    """The scoring module stays pure: no sqlite, paths, time, numpy, or runtime imports."""
    import trader.domain.learnings.scoring as scoring

    source = inspect.getsource(scoring)
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint({"sqlite3", "pathlib", "datetime", "numpy"})


def test_infra_learnings_store_does_not_import_runtime_or_application() -> None:
    """The SQLite adapter must not depend on runtime/application layers."""
    import trader.infrastructure.state_db.learnings_store as learnings_store

    source = inspect.getsource(learnings_store)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        module.startswith(("trader.application", "trader.runtime"))
        for module in imported_modules
    )
