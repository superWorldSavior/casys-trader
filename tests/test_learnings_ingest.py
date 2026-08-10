"""Tests TDD — Task 7 : script d'ingestion du store de recall.

Couvre :
- main() importable depuis scripts.learnings_ingest
- run --no-embeddings sur tmp state fixture → db créée, counts corrects, JSON valide
- idempotence (second run → skipped)
- sources absentes tolérées
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers fixtures
# ---------------------------------------------------------------------------

_ROWS_LEDGER = [
    {
        "ts": "2026-06-08T05:44:50+00:00",
        "symbol": "NVDA",
        "note": "Breakout résistance forte cassure momentum.",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "breakout",
        "decision_id": "ingest-test-1",
    },
    {
        "ts": "2026-06-08T06:00:00+00:00",
        "symbol": "AAPL",
        "note": "Attente confirmation, range serré.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "ingest-test-2",
    },
]

_ROWS_RUNTIME = [
    {
        "ts": "2026-06-09T10:00:00+00:00",
        "symbol": "SPY",
        "note": "Volume faible, éviter.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "ingest-test-3",
    },
]

_ROWS_EVICTED = [
    {
        "ts": "2026-06-07T08:00:00+00:00",
        "symbol": "QQQ",
        "note": "Évincé du buffer vif.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "ingest-evicted-1",
        "evicted_at": "2026-06-08T00:00:00+00:00",
    },
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _setup_state(tmp_path: Path) -> Path:
    """Crée une arborescence state/ de test avec les 3 sources."""
    state_dir = tmp_path / "state"
    archive_dir = state_dir / "archive"
    archive_dir.mkdir(parents=True)

    _write_jsonl(archive_dir / "learnings-from-ledger.jsonl", _ROWS_LEDGER)
    _write_jsonl(archive_dir / "learnings-evicted.jsonl", _ROWS_EVICTED)
    _write_jsonl(state_dir / "learnings.jsonl", _ROWS_RUNTIME)

    return state_dir


def _run_main(argv: list[str]) -> tuple[int, dict]:
    """Exécute main() en capturant stdout ; retourne (exit_code, report_dict)."""
    from scripts.learnings_ingest import main  # noqa: PLC0415

    captured = StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        result = main(argv)
    finally:
        sys.stdout = old_stdout

    exit_code = result if isinstance(result, int) else 0
    report = json.loads(captured.getvalue().strip())
    return exit_code, report


# ---------------------------------------------------------------------------
# Test : importabilité
# ---------------------------------------------------------------------------


def test_main_importable() -> None:
    """La fonction main() est importable depuis scripts.learnings_ingest."""
    from scripts.learnings_ingest import main  # noqa: PLC0415

    assert callable(main)


# ---------------------------------------------------------------------------
# Test : run basique --no-embeddings
# ---------------------------------------------------------------------------


def test_main_no_embeddings_cree_db(tmp_path: Path) -> None:
    """--no-embeddings → la db SQLite est créée dans state_dir."""
    state_dir = _setup_state(tmp_path)

    _run_main(["--state-dir", str(state_dir), "--no-embeddings"])

    assert (state_dir / "learnings.db").exists(), "learnings.db doit exister après l'ingestion"


def test_main_no_embeddings_counts_corrects(tmp_path: Path) -> None:
    """Counts insérés corrects : 2 ledger + 1 évincé + 1 runtime = 4 total."""
    state_dir = _setup_state(tmp_path)

    _, report = _run_main(["--state-dir", str(state_dir), "--no-embeddings"])

    assert report["ingest"]["ledger_backfill"]["inserted"] == 2
    assert report["ingest"]["rationale_backfill"]["inserted"] == 0
    assert report["ingest"]["evicted"]["inserted"] == 1
    assert report["ingest"]["runtime"]["inserted"] == 1
    assert report["total_notes"] == 4


def test_main_no_embeddings_sortie_json_valide(tmp_path: Path) -> None:
    """La sortie stdout est un JSON valide avec les clés attendues."""
    state_dir = _setup_state(tmp_path)

    exit_code, report = _run_main(["--state-dir", str(state_dir), "--no-embeddings"])

    assert exit_code == 0
    assert "db" in report
    assert "ingest" in report
    assert "verdicts_updated" in report
    assert "scoring" in report
    assert "embeddings_backfilled" in report
    assert "total_notes" in report
    # --no-embeddings → 0 embedding
    assert report["embeddings_backfilled"] == 0


# ---------------------------------------------------------------------------
# Test : sources absentes tolérées
# ---------------------------------------------------------------------------


def test_main_sources_absentes_ne_plante_pas(tmp_path: Path) -> None:
    """Aucune source présente → inserted=0 partout, pas d'exception."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "archive").mkdir()

    _, report = _run_main(["--state-dir", str(state_dir), "--no-embeddings"])

    assert report["total_notes"] == 0
    for source_key in ("ledger_backfill", "rationale_backfill", "evicted", "runtime"):
        assert report["ingest"][source_key]["inserted"] == 0


# ---------------------------------------------------------------------------
# Test : idempotence
# ---------------------------------------------------------------------------


def test_main_idempotent(tmp_path: Path) -> None:
    """Deux runs successifs → second run : 0 inserted, 4 skipped."""
    state_dir = _setup_state(tmp_path)

    _, r1 = _run_main(["--state-dir", str(state_dir), "--no-embeddings"])
    _, r2 = _run_main(["--state-dir", str(state_dir), "--no-embeddings"])

    # Premier run : tout inséré
    assert r1["total_notes"] == 4
    # Deuxième run : rien inséré
    for source_key in ("ledger_backfill", "rationale_backfill", "evicted", "runtime"):
        assert r2["ingest"][source_key]["inserted"] == 0, (
            f"Idempotence violée pour {source_key}"
        )
    assert r2["total_notes"] == 4
