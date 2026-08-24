"""Tests TDD — Task 2 : LearningsStore (schéma + ingestion idempotente).

Couvre :
- schéma créé (tables notes, notes_fts, recalls)
- ingestion 3 lignes → {"inserted": 3, "skipped": 0}
- re-run idempotent → {"inserted": 0, "skipped": 3}
- ligne sans decision_id → clé synthétique synth:{ts}|{symbol}
- FTS5 répond à un MATCH
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trader.agent.learnings.store import LearningsStore
from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION


# ---------------------------------------------------------------------------
# Fixtures JSONL
# ---------------------------------------------------------------------------

_ROWS = [
    {
        "ts": "2026-06-08T05:44:50.461276+00:00",
        "symbol": "NG=F",
        "note": "Maintenir la règle: NG=F reste en veille tant que AC n'est pas redevenue positive.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "2026-06-08T05:44:50.461276+00:00|0|NG=F",
    },
    {
        "ts": "2026-06-08T06:01:33.130179+00:00",
        "symbol": "USDJPY=X",
        "note": "Respecter le filtre stricte sur USDJPY: rester en stand-by.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "2026-06-08T06:01:33.130179+00:00|10|USDJPY=X",
    },
    {
        "ts": "2026-06-08T06:29:50.468917+00:00",
        "symbol": "NVDA",
        "note": "Cassure résistance 950 avec volume élevé, momentum fort, ER=0.8.",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "breakout",
        "decision_id": "2026-06-08T06:29:50.468917+00:00|5|NVDA",
    },
]

_ROW_NO_DECISION_ID = {
    "ts": "2026-06-09T10:00:00+00:00",
    "symbol": "SPY",
    "note": "Divergence momentum détectée sur SPY.",
    "action": "HOLD",
    "intent": "HOLD",
    "executed": False,
    "reason": "hold",
    # Pas de decision_id
}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Tests schéma
# ---------------------------------------------------------------------------


def test_schema_cree_les_tables(tmp_path: Path) -> None:
    """__init__ crée les tables notes, notes_fts et recalls."""
    db_path = tmp_path / "learnings.db"
    LearningsStore(db_path)

    conn = sqlite3.connect(str(db_path))
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow') ORDER BY name"
    )}
    conn.close()

    assert "notes" in tables
    assert "recalls" in tables
    # FTS5 crée une table virtuelle visible dans sqlite_master
    vtables = {row[0] for row in sqlite3.connect(str(db_path)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "notes_fts" in vtables


def test_schema_colonnes_notes(tmp_path: Path) -> None:
    """La table notes possède les colonnes attendues (design §4.1)."""
    LearningsStore(tmp_path / "learnings.db")
    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    conn.close()

    expected = {
        "id", "decision_id", "ts", "symbol", "family", "venue",
        "action", "intent", "executed", "reason", "note", "concepts",
        "source", "valid_from", "valid_until", "superseded_by",
        "verdict", "forward_return", "outcome_score", "outcome_semantics_version",
        "q_value", "embedding",
    }
    assert expected.issubset(cols), f"Colonnes manquantes : {expected - cols}"


def test_schema_recalls_colonnes(tmp_path: Path) -> None:
    """La table recalls possède les colonnes attendues."""
    LearningsStore(tmp_path / "learnings.db")
    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(recalls)")}
    conn.close()
    assert {
        "id",
        "decision_id",
        "note_ids",
        "ts",
        "verdict",
        "reward",
        "forward_return",
        "evaluated_at",
        "outcome_semantics_version",
    }.issubset(cols)


def test_schema_migre_les_colonnes_memrl_additives(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT, q_value REAL)")
    conn.execute(
        "CREATE TABLE recalls (id INTEGER PRIMARY KEY, decision_id TEXT, note_ids TEXT, ts TEXT)"
    )
    conn.commit()
    conn.close()

    store = LearningsStore(db_path)
    notes_cols = {row[1] for row in store._conn.execute("PRAGMA table_info(notes)")}
    recalls_cols = {row[1] for row in store._conn.execute("PRAGMA table_info(recalls)")}

    assert "q_updates" in notes_cols
    assert {
        "verdict",
        "reward",
        "forward_return",
        "evaluated_at",
        "outcome_semantics_version",
    } <= recalls_cols


def test_count_initial_zero(tmp_path: Path) -> None:
    """count() renvoie 0 sur un store vide."""
    store = LearningsStore(tmp_path / "learnings.db")
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Tests ingestion
# ---------------------------------------------------------------------------


def test_ingest_trois_lignes(tmp_path: Path) -> None:
    """3 lignes JSONL → inserted=3, skipped=0 ; count=3."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)

    store = LearningsStore(tmp_path / "learnings.db")
    result = store.ingest_jsonl(jsonl, source="test")

    assert result == {"inserted": 3, "skipped": 0}
    assert store.count() == 3


def test_ingest_idempotent(tmp_path: Path) -> None:
    """Re-run ingestion → inserted=0, skipped=3 (dédup par decision_id)."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    result2 = store.ingest_jsonl(jsonl, source="test")

    assert result2 == {"inserted": 0, "skipped": 3}
    assert store.count() == 3  # aucun doublon


def test_ingest_ligne_sans_decision_id_cle_synthetique(tmp_path: Path) -> None:
    """Ligne sans decision_id → clé synthétique synth:{ts}|{symbol}."""
    jsonl = tmp_path / "no_did.jsonl"
    _write_jsonl(jsonl, [_ROW_NO_DECISION_ID])

    store = LearningsStore(tmp_path / "learnings.db")
    result = store.ingest_jsonl(jsonl, source="test")

    assert result["inserted"] == 1
    # Vérifier la clé synthétique
    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    row = conn.execute("SELECT decision_id FROM notes LIMIT 1").fetchone()
    conn.close()
    expected_key = f"synth:{_ROW_NO_DECISION_ID['ts']}|{_ROW_NO_DECISION_ID['symbol']}"
    assert row[0] == expected_key


def test_ingest_renseigne_valid_from(tmp_path: Path) -> None:
    """valid_from = ts de la ligne ingérée."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, [_ROWS[0]])

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    row = conn.execute("SELECT ts, valid_from FROM notes LIMIT 1").fetchone()
    conn.close()
    assert row[0] == _ROWS[0]["ts"]
    assert row[1] == _ROWS[0]["ts"]


def test_ingest_renseigne_family(tmp_path: Path) -> None:
    """family déduite de family_for_symbol(symbol) — None accepté si inconnu."""
    jsonl = tmp_path / "test.jsonl"
    # NVDA est dans nasdaq_single_names, NG=F est dans commodities_futures
    _write_jsonl(jsonl, _ROWS)

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    rows = {r[0]: r[1] for r in conn.execute("SELECT symbol, family FROM notes")}
    conn.close()

    assert rows["NVDA"] == "nasdaq_single_names"
    assert rows["NG=F"] == "commodities_futures"
    # USDJPY=X est dans forex_majors
    assert rows["USDJPY=X"] == "forex_majors"


def test_ingest_fichier_absent_renvoie_zero(tmp_path: Path) -> None:
    """Fichier JSONL absent → inserted=0, skipped=0 sans exception."""
    store = LearningsStore(tmp_path / "learnings.db")
    result = store.ingest_jsonl(tmp_path / "absent.jsonl", source="test")
    assert result == {"inserted": 0, "skipped": 0}


def test_ingest_commit_failure_rolls_back_and_connection_stays_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)
    store = LearningsStore(tmp_path / "learnings.db")
    real_conn = store._conn

    class _FailingCommit:
        def commit(self) -> None:
            raise sqlite3.OperationalError("database is locked")

        def __getattr__(self, name: str):
            return getattr(real_conn, name)

    monkeypatch.setattr(store, "_conn", _FailingCommit())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store.ingest_jsonl(jsonl, source="test")
    monkeypatch.undo()

    assert store.count() == 0
    result = store.ingest_jsonl(jsonl, source="test")
    assert result == {"inserted": 3, "skipped": 0}
    assert store.count() == 3


# ---------------------------------------------------------------------------
# Tests FTS5
# ---------------------------------------------------------------------------


def test_fts5_match_trouve_la_note(tmp_path: Path) -> None:
    """FTS5 notes_fts répond à un MATCH sur un mot du champ note."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    # Le mot "momentum" est dans la note NVDA
    rows = conn.execute(
        "SELECT notes.symbol FROM notes_fts JOIN notes ON notes.id = notes_fts.rowid"
        " WHERE notes_fts MATCH 'momentum'"
    ).fetchall()
    conn.close()

    symbols = [r[0] for r in rows]
    assert "NVDA" in symbols


def test_fts5_match_terme_absent_renvoie_vide(tmp_path: Path) -> None:
    """FTS5 MATCH sur un terme absent → résultat vide."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    rows = conn.execute(
        "SELECT notes.id FROM notes_fts JOIN notes ON notes.id = notes_fts.rowid"
        " WHERE notes_fts MATCH 'xyznotexist'"
    ).fetchall()
    conn.close()
    assert rows == []


# ---------------------------------------------------------------------------
# Tests robustesse
# ---------------------------------------------------------------------------


def test_store_reouverture_preserve_les_donnees(tmp_path: Path) -> None:
    """Rouvrir le store avec le même chemin préserve les notes insérées."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)

    db_path = tmp_path / "learnings.db"
    store1 = LearningsStore(db_path)
    store1.ingest_jsonl(jsonl, source="test")

    store2 = LearningsStore(db_path)  # nouvelle instance, même fichier
    assert store2.count() == 3


def test_ingest_source_externe_prioritaire(tmp_path: Path) -> None:
    """Le paramètre source est bien enregistré dans la colonne source."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, [_ROWS[0]])

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="ledger-backfill")

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    row = conn.execute("SELECT source FROM notes LIMIT 1").fetchone()
    conn.close()
    assert row[0] == "ledger-backfill"


# ---------------------------------------------------------------------------
# Task 3 : apply_verdicts + compute_outcome_scores
# ---------------------------------------------------------------------------

import pytest  # noqa: E402 (import en bas de fichier, acceptable dans les tests)

# Fixtures pour le scoring : SYM_A (4W/1L), SYM_B (1W/4L), SYM_C (1W + 1N)
_SCORING_ROWS_A = [
    {
        "ts": f"2026-06-10T0{i}:00:00+00:00",
        "symbol": "SYM_A",
        "note": f"Note SYM_A {i}",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "test",
        "decision_id": f"did-A-{i}",
    }
    for i in range(5)
]

_SCORING_ROWS_B = [
    {
        "ts": f"2026-06-11T0{i}:00:00+00:00",
        "symbol": "SYM_B",
        "note": f"Note SYM_B {i}",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "test",
        "decision_id": f"did-B-{i}",
    }
    for i in range(5)
]

_SCORING_ROWS_C = [
    {
        "ts": "2026-06-12T01:00:00+00:00",
        "symbol": "SYM_C",
        "note": "Note SYM_C WIN",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "test",
        "decision_id": "did-C-0",
    },
    {
        "ts": "2026-06-12T02:00:00+00:00",
        "symbol": "SYM_C",
        "note": "Note SYM_C NEUTRAL",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "test",
        "decision_id": "did-C-1",
    },
]


def _make_bootstrap_json(path: Path, verdicts: dict) -> None:
    """Crée un fichier bootstrap JSON (format learnings_outcome_bootstrap) avec les verdicts donnés."""
    learnings = [
        {"decision_id": did, **fields}
        for did, fields in verdicts.items()
    ]
    report = {
        "generated_at": "2026-07-02T00:00:00+00:00",
        "outcome_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "params": {},
        "counts": {},
        "unavailable_symbols": [],
        "aggregates": {},
        "learnings": learnings,
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def _setup_scoring_store(tmp_path: Path) -> "LearningsStore":
    """Crée un store peuplé avec SYM_A (4W/1L), SYM_B (1W/4L), SYM_C (1W+1N)."""
    all_rows = _SCORING_ROWS_A + _SCORING_ROWS_B + _SCORING_ROWS_C
    jsonl = tmp_path / "scoring.jsonl"
    _write_jsonl(jsonl, all_rows)

    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    # Verdicts : SYM_A 4W/1L, SYM_B 1W/4L, SYM_C 1W+1N
    verdicts: dict[str, dict] = {}
    for i in range(4):
        verdicts[f"did-A-{i}"] = {"verdict": "WIN", "forward_return": 0.03}
    verdicts["did-A-4"] = {"verdict": "LOSS", "forward_return": -0.04}
    verdicts["did-B-0"] = {"verdict": "WIN", "forward_return": 0.05}
    for i in range(1, 5):
        verdicts[f"did-B-{i}"] = {"verdict": "LOSS", "forward_return": -0.02}
    verdicts["did-C-0"] = {"verdict": "WIN", "forward_return": 0.02}
    verdicts["did-C-1"] = {"verdict": "NEUTRAL", "forward_return": 0.0}

    bootstrap = tmp_path / "bootstrap.json"
    _make_bootstrap_json(bootstrap, verdicts)
    store.apply_verdicts(bootstrap)
    return store


# --- apply_verdicts ---

def test_apply_verdicts_met_a_jour_verdict_et_forward_return(tmp_path: Path) -> None:
    """apply_verdicts met à jour verdict + forward_return et retourne le nombre de notes mises à jour."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, _ROWS)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    did0 = _ROWS[0]["decision_id"]
    did2 = _ROWS[2]["decision_id"]
    bootstrap = tmp_path / "bootstrap.json"
    _make_bootstrap_json(bootstrap, {
        did0: {"verdict": "WIN", "forward_return": 0.05},
        did2: {"verdict": "LOSS", "forward_return": -0.03},
    })

    n = store.apply_verdicts(bootstrap)
    assert n == 2

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    r0 = conn.execute(
        "SELECT verdict, forward_return FROM notes WHERE decision_id=?", (did0,)
    ).fetchone()
    r2 = conn.execute(
        "SELECT verdict, forward_return FROM notes WHERE decision_id=?", (did2,)
    ).fetchone()
    conn.close()

    assert r0[0] == "WIN"
    assert r0[1] == pytest.approx(0.05)
    assert r2[0] == "LOSS"
    assert r2[1] == pytest.approx(-0.03)


def test_apply_verdicts_fichier_absent_retourne_zero(tmp_path: Path) -> None:
    """apply_verdicts sur fichier inexistant retourne 0 sans lever d'exception."""
    store = LearningsStore(tmp_path / "learnings.db")
    n = store.apply_verdicts(tmp_path / "absent.json")
    assert n == 0


def test_apply_verdicts_only_missing_necrase_pas_un_outcome_live(tmp_path: Path) -> None:
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(jsonl, [_ROWS[0]])
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    store._conn.execute(
        """
        UPDATE notes
        SET verdict='LOSS', forward_return=-0.02,
            outcome_semantics_version=?
        """,
        (BENCHMARK_SEMANTICS_VERSION,),
    )
    store._conn.commit()
    bootstrap = tmp_path / "bootstrap.json"
    _make_bootstrap_json(
        bootstrap,
        {_ROWS[0]["decision_id"]: {"verdict": "WIN", "forward_return": 0.05}},
    )

    updated = store.apply_verdicts(bootstrap, only_missing=True)
    row = store._conn.execute(
        "SELECT verdict, forward_return FROM notes"
    ).fetchone()

    assert updated == 0
    assert tuple(row) == ("LOSS", -0.02)


# --- compute_outcome_scores ---

def test_compute_outcome_scores_lift_signe_correct(tmp_path: Path) -> None:
    """Lift signé correct pour les deux côtés (A haute base_rate, B basse base_rate)."""
    store = _setup_scoring_store(tmp_path)
    result = store.compute_outcome_scores()

    assert "scored" in result
    assert "base_rates" in result
    assert result["scored"] > 0

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    a_wins = [r[0] for r in conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_A' AND verdict='WIN'"
    ).fetchall()]
    a_loss_score = conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_A' AND verdict='LOSS'"
    ).fetchone()[0]
    b_win_score = conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_B' AND verdict='WIN'"
    ).fetchone()[0]
    b_losses = [r[0] for r in conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_B' AND verdict='LOSS'"
    ).fetchall()]
    conn.close()

    # SYM_A base_rate=0.8 : WIN doit avoir score > 0 (lift faible), LOSS score < 0 (lift fort)
    for s in a_wins:
        assert s > 0, f"WIN SYM_A (base_rate=0.8) doit avoir score > 0, got {s}"
    assert a_loss_score < 0, f"LOSS SYM_A (base_rate=0.8) doit avoir score < 0, got {a_loss_score}"

    # SYM_B base_rate=0.2 : WIN doit avoir score > 0 (lift fort), LOSS score < 0 (lift faible)
    assert b_win_score > 0, f"WIN SYM_B (base_rate=0.2) doit avoir score > 0, got {b_win_score}"
    for s in b_losses:
        assert s < 0, f"LOSS SYM_B (base_rate=0.2) doit avoir score < 0, got {s}"

    # WIN B (basse base_rate) > WIN A (haute base_rate) : lift plus grand
    assert b_win_score > a_wins[0]
    # LOSS A (haute base_rate) < LOSS B (basse base_rate) : lift plus négatif
    assert a_loss_score < b_losses[0]


def test_compute_outcome_scores_shrinkage_formule(tmp_path: Path) -> None:
    """|score| = |lift| / (1 + k) pour n=1, k=5 → divisé par 6."""
    store = _setup_scoring_store(tmp_path)
    store.compute_outcome_scores(shrinkage_k=5.0)

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    a_loss_score = conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_A' AND verdict='LOSS'"
    ).fetchone()[0]
    b_win_score = conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_B' AND verdict='WIN'"
    ).fetchone()[0]
    conn.close()

    # SYM_A base_rate=0.8 ; LOSS → lift = 0.0 - 0.8 = -0.8 ; score = -0.8/6
    assert a_loss_score == pytest.approx(-0.8 / 6.0, abs=1e-9)
    # SYM_B base_rate=0.2 ; WIN → lift = 1.0 - 0.2 = 0.8 ; score = 0.8/6
    assert b_win_score == pytest.approx(0.8 / 6.0, abs=1e-9)


def test_compute_outcome_scores_neutral_zero(tmp_path: Path) -> None:
    """Notes NEUTRAL → outcome_score = 0.0."""
    store = _setup_scoring_store(tmp_path)
    store.compute_outcome_scores()

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    neutral_scores = [r[0] for r in conn.execute(
        "SELECT outcome_score FROM notes WHERE verdict='NEUTRAL'"
    ).fetchall()]
    conn.close()

    assert neutral_scores, "Il doit exister au moins une note NEUTRAL dans la fixture"
    for s in neutral_scores:
        assert s == pytest.approx(0.0), f"NEUTRAL doit avoir outcome_score=0.0, got {s}"


def test_compute_outcome_scores_fallback_global(tmp_path: Path) -> None:
    """SYM_C (<5 scorables WIN/LOSS) utilise la base_rate globale."""
    store = _setup_scoring_store(tmp_path)
    store.compute_outcome_scores(shrinkage_k=5.0)

    # Global : SYM_A(4W+1L) + SYM_B(1W+4L) + SYM_C(1W) = 6W / 11 scorables
    global_wins = 4 + 1 + 1
    global_losses = 1 + 4
    global_base_rate = global_wins / (global_wins + global_losses)
    expected_c_win = (1.0 - global_base_rate) / 6.0

    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    c_win_score = conn.execute(
        "SELECT outcome_score FROM notes WHERE symbol='SYM_C' AND verdict='WIN'"
    ).fetchone()[0]
    conn.close()

    assert c_win_score == pytest.approx(expected_c_win, abs=1e-9)


# ---------------------------------------------------------------------------
# Task 4 : backfill_embeddings + search hybride + record_recall
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

import numpy as np  # noqa: E402

EMBEDDING_DIMS = 1536


def _make_unit_blob(dim_index: int) -> bytes:
    """Vecteur unitaire float32 avec 1.0 à l'index dim_index (1536 dims)."""
    arr = np.zeros(EMBEDDING_DIMS, dtype=np.float32)
    arr[dim_index] = 1.0
    return arr.tobytes()


_SEARCH_ROWS = [
    {
        "ts": "2026-06-01T10:00:00+00:00",
        "symbol": "NVDA",
        "note": "breakout cassure résistance momentum fort signal haussier",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "breakout",
        "decision_id": "emb-nvda-1",
    },
    {
        "ts": "2026-06-02T10:00:00+00:00",
        "symbol": "NVDA",
        "note": "consolidation range serré avant reprise possible",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_id": "emb-nvda-2",
    },
    {
        "ts": "2026-06-03T10:00:00+00:00",
        "symbol": "AAPL",
        "note": "support testé divergence RSI retournement possible swing",
        "action": "BUY",
        "intent": "BUY",
        "executed": True,
        "reason": "reversal",
        "decision_id": "emb-aapl-1",
    },
]


def _setup_search_store(tmp_path: Path) -> "LearningsStore":
    """Store peuplé avec 3 notes (2 NVDA, 1 AAPL), sans embeddings initiaux."""
    jsonl = tmp_path / "search_test.jsonl"
    _write_jsonl(jsonl, _SEARCH_ROWS)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    return store


# --- backfill_embeddings ---


def test_backfill_embeddings_embede_seulement_les_sans(tmp_path: Path) -> None:
    """backfill_embeddings n'embède que les 2 notes sans embedding ; retourne 2."""
    store = _setup_search_store(tmp_path)

    # Pré-renseigner l'embedding de emb-aapl-1 → ne doit PAS être re-embeddé
    store._conn.execute(
        "UPDATE notes SET embedding = ? WHERE decision_id = 'emb-aapl-1'",
        (_make_unit_blob(0),),
    )
    store._conn.commit()

    calls: list[list[str]] = []

    def fake_embedder(texts: list[str]) -> list[bytes]:
        calls.append(list(texts))
        return [_make_unit_blob(i + 10) for i in range(len(texts))]

    n = store.backfill_embeddings(fake_embedder)

    assert n == 2
    total_texts = sum(len(c) for c in calls)
    assert total_texts == 2


def test_backfill_embeddings_zero_si_tous_ont_embedding(tmp_path: Path) -> None:
    """backfill_embeddings retourne 0 et n'appelle pas l'embedder si toutes les notes sont embeddées."""
    store = _setup_search_store(tmp_path)

    for did in ["emb-nvda-1", "emb-nvda-2", "emb-aapl-1"]:
        store._conn.execute(
            "UPDATE notes SET embedding = ? WHERE decision_id = ?",
            (_make_unit_blob(0), did),
        )
    store._conn.commit()

    called: list = []

    def fake_embedder(texts: list[str]) -> list[bytes]:
        called.append(texts)
        return [_make_unit_blob(0)] * len(texts)

    n = store.backfill_embeddings(fake_embedder)

    assert n == 0
    assert called == [], "L'embedder ne doit pas être appelé si toutes les notes ont déjà un embedding"


# --- search — facettes ---


def test_search_facette_symbol_filtre(tmp_path: Path) -> None:
    """search(symbol='NVDA') retourne uniquement les notes NVDA."""
    store = _setup_search_store(tmp_path)
    results = store.search(symbol="NVDA")
    assert len(results) > 0, "Au moins une note NVDA attendue"
    for r in results:
        assert r["symbol"] == "NVDA", f"Note inattendue : {r['symbol']}"


def test_search_sans_filtre_retourne_toutes_les_notes(tmp_path: Path) -> None:
    """search() sans filtre retourne les 3 notes."""
    store = _setup_search_store(tmp_path)
    results = store.search()
    assert len(results) == 3


# --- search — expiration ---


def test_search_note_expiree_exclue(tmp_path: Path) -> None:
    """Une note dont valid_until est dans le passé est exclue des résultats."""
    store = _setup_search_store(tmp_path)

    past = "2020-01-01T00:00:00+00:00"
    store._conn.execute(
        "UPDATE notes SET valid_until = ? WHERE decision_id = 'emb-nvda-1'", (past,)
    )
    store._conn.commit()

    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    results = store.search(symbol="NVDA", now=now)

    expired_id = store._conn.execute(
        "SELECT id FROM notes WHERE decision_id = 'emb-nvda-1'"
    ).fetchone()[0]
    ids = [r["id"] for r in results]
    assert expired_id not in ids, "La note expirée ne doit pas apparaître dans les résultats"


def test_search_note_non_expiree_incluse(tmp_path: Path) -> None:
    """Une note dont valid_until est dans le futur est incluse."""
    store = _setup_search_store(tmp_path)

    future = "2099-12-31T00:00:00+00:00"
    store._conn.execute(
        "UPDATE notes SET valid_until = ? WHERE decision_id = 'emb-nvda-1'", (future,)
    )
    store._conn.commit()

    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    results = store.search(now=now)

    active_id = store._conn.execute(
        "SELECT id FROM notes WHERE decision_id = 'emb-nvda-1'"
    ).fetchone()[0]
    ids = [r["id"] for r in results]
    assert active_id in ids


# --- search — cosine ---


def test_search_cosine_trouve_note_la_plus_proche(tmp_path: Path) -> None:
    """search(query_vec=...) retourne en tête la note dont l'embedding est le plus proche."""
    store = _setup_search_store(tmp_path)

    # Embeddings orthogonaux : dim0→nvda-1, dim1→nvda-2, dim2→aapl-1
    for did, dim in [("emb-nvda-1", 0), ("emb-nvda-2", 1), ("emb-aapl-1", 2)]:
        store._conn.execute(
            "UPDATE notes SET embedding = ? WHERE decision_id = ?",
            (_make_unit_blob(dim), did),
        )
    store._conn.commit()

    # Query alignée sur dim 0 → nvda-1 doit être en tête
    query = _make_unit_blob(0)
    # now = ts des notes → age_days = 0 pour tous → freshness identique
    now = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    results = store.search(query_vec=query, now=now)

    assert len(results) > 0
    nvda1_id = store._conn.execute(
        "SELECT id FROM notes WHERE decision_id = 'emb-nvda-1'"
    ).fetchone()[0]
    assert results[0]["id"] == nvda1_id, (
        f"nvda-1 (dim 0) devrait être en tête, got id={results[0]['id']}"
    )


# --- search — déterminisme (RRF stable) ---


def test_search_rrf_deterministique(tmp_path: Path) -> None:
    """search avec les mêmes paramètres retourne le même ordre (déterminisme)."""
    store = _setup_search_store(tmp_path)
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    results1 = store.search(text_query="momentum", now=now)
    results2 = store.search(text_query="momentum", now=now)
    assert [r["id"] for r in results1] == [r["id"] for r in results2]


# --- search — limit et troncature ---


def test_search_limit_respecte(tmp_path: Path) -> None:
    """search(limit=1) retourne au plus 1 résultat."""
    store = _setup_search_store(tmp_path)
    results = store.search(limit=1)
    assert len(results) <= 1


def test_search_note_tronquee_240c(tmp_path: Path) -> None:
    """Les notes dans les résultats sont tronquées à 240 caractères max."""
    long_note = "X" * 300
    rows = [
        {
            "ts": "2026-06-10T10:00:00+00:00",
            "symbol": "TSLA",
            "note": long_note,
            "action": "BUY",
            "intent": "BUY",
            "executed": True,
            "reason": "test",
            "decision_id": "long-note-tsla",
        }
    ]
    jsonl = tmp_path / "long.jsonl"
    _write_jsonl(jsonl, rows)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    results = store.search()
    assert len(results) > 0
    for r in results:
        assert len(r["note"]) <= 240, f"Note trop longue : {len(r['note'])} chars"


def test_search_champs_attendus(tmp_path: Path) -> None:
    """Chaque résultat contient les clés id, ts, symbol, verdict, outcome_score, note."""
    store = _setup_search_store(tmp_path)
    results = store.search()
    assert len(results) > 0
    required_keys = {"id", "ts", "symbol", "verdict", "outcome_score", "note"}
    for r in results:
        missing = required_keys - set(r)
        assert not missing, f"Clés manquantes : {missing}"


# --- record_recall ---


def test_record_recall_ecrit_la_ligne(tmp_path: Path) -> None:
    """record_recall persiste une ligne dans la table recalls avec les bons note_ids."""
    store = _setup_search_store(tmp_path)

    store.record_recall(decision_id="test-decision-42", note_ids=[1, 2, 3])

    rows = store._conn.execute(
        "SELECT decision_id, note_ids FROM recalls WHERE decision_id = 'test-decision-42'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "test-decision-42"
    assert json.loads(rows[0][1]) == [1, 2, 3]


def test_record_recall_ts_renseigne(tmp_path: Path) -> None:
    """record_recall renseigne le champ ts (non vide, ISO format)."""
    store = _setup_search_store(tmp_path)
    store.record_recall(decision_id="test-ts-recall", note_ids=[7])

    row = store._conn.execute(
        "SELECT ts FROM recalls WHERE decision_id = 'test-ts-recall'"
    ).fetchone()
    assert row is not None
    assert row[0] and len(row[0]) > 0, "ts doit être non vide"


def test_apply_recall_outcome_met_a_jour_q_value_une_seule_fois(tmp_path: Path) -> None:
    store = _setup_scoring_store(tmp_path)
    note_id = store._conn.execute("SELECT id FROM notes ORDER BY id LIMIT 1").fetchone()[0]
    store.record_recall(decision_id="rewarded-decision", note_ids=[note_id])

    first = store.apply_recall_outcome(
        decision_id="rewarded-decision",
        verdict="WIN",
        reward=1.0,
        forward_return=0.02,
        evaluated_at="2026-07-10T00:00:00+00:00",
        alpha=0.1,
    )
    second = store.apply_recall_outcome(
        decision_id="rewarded-decision",
        verdict="LOSS",
        reward=-1.0,
        forward_return=-0.02,
        evaluated_at="2026-07-11T00:00:00+00:00",
        alpha=0.1,
    )

    row = store._conn.execute(
        "SELECT q_value, q_updates FROM notes WHERE id=?",
        (note_id,),
    ).fetchone()
    assert first["notes_updated"] == 1
    assert second["notes_updated"] == 0
    assert tuple(row) == (0.1, 1)


def test_search_memrl_departage_des_notes_equivalentes(tmp_path: Path) -> None:
    rows = [
        {**_ROWS[0], "decision_id": "memrl-positive", "note": "même setup", "symbol": "SPY"},
        {**_ROWS[0], "decision_id": "memrl-negative", "note": "même setup", "symbol": "SPY"},
    ]
    jsonl = tmp_path / "memrl.jsonl"
    _write_jsonl(jsonl, rows)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    note_ids = [row[0] for row in store._conn.execute("SELECT id FROM notes ORDER BY id")]
    for decision_id, note_id, reward, verdict in (
        ("later-win", note_ids[0], 1.0, "WIN"),
        ("later-loss", note_ids[1], -1.0, "LOSS"),
    ):
        store.record_recall(decision_id=decision_id, note_ids=[note_id])
        store.apply_recall_outcome(
            decision_id=decision_id,
            verdict=verdict,
            reward=reward,
            forward_return=0.01 * reward,
            evaluated_at="2026-07-10T00:00:00+00:00",
        )

    result = store.search(
        symbol="SPY",
        limit=2,
        now=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert [row["id"] for row in result] == note_ids
    assert result[0]["q_value"] == 0.1
    assert result[1]["q_value"] == -0.1


def test_search_exclut_une_note_memrl_nuisible_mesuree(tmp_path: Path) -> None:
    from trader.domain.learnings.scoring import MEMRL_MIN_UPDATES

    rows = [
        {**_ROWS[0], "decision_id": "ok-setup", "note": "même setup", "symbol": "SPY"},
        {**_ROWS[0], "decision_id": "hurt-setup", "note": "même setup", "symbol": "SPY"},
    ]
    jsonl = tmp_path / "hurt.jsonl"
    _write_jsonl(jsonl, rows)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    ok_id, hurt_id = [row[0] for row in store._conn.execute("SELECT id FROM notes ORDER BY id")]
    store._conn.execute(
        "UPDATE notes SET q_value=?, q_updates=? WHERE id=?",
        (0.05, 3, ok_id),
    )
    store._conn.execute(
        "UPDATE notes SET q_value=?, q_updates=? WHERE id=?",
        (-0.4, MEMRL_MIN_UPDATES, hurt_id),
    )
    store._conn.commit()

    result = store.search(symbol="SPY", limit=8, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
    ids = [row["id"] for row in result]
    assert ok_id in ids
    assert hurt_id not in ids


def test_search_vide_si_tous_les_matchs_sont_nuisibles_mesures(tmp_path: Path) -> None:
    from trader.domain.learnings.scoring import MEMRL_MIN_UPDATES

    jsonl = tmp_path / "only-hurt.jsonl"
    _write_jsonl(
        jsonl,
        [{**_ROWS[0], "decision_id": "only-hurt", "note": "setup toxique", "symbol": "SPY"}],
    )
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    note_id = store._conn.execute("SELECT id FROM notes").fetchone()[0]
    store._conn.execute(
        "UPDATE notes SET q_value=?, q_updates=? WHERE id=?",
        (-0.5, MEMRL_MIN_UPDATES, note_id),
    )
    store._conn.commit()

    assert store.search(symbol="SPY", now=datetime(2026, 7, 10, tzinfo=timezone.utc)) == []


# ---------------------------------------------------------------------------
# Findings review Codex 2026-07-02 : thread-safety, query restriction, verdict
# ---------------------------------------------------------------------------

import threading  # noqa: E402


def test_search_record_recall_thread_safe(tmp_path: Path) -> None:
    """Finding 3 : 8 threads × search+record_recall concurrents → aucune exception."""
    store = _setup_search_store(tmp_path)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            store.search(text_query="momentum")
            store.record_recall(decision_id=f"t-{i}", note_ids=[1])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Exceptions dans les threads : {errors}"


def test_search_query_filtre_aux_matches_seulement(tmp_path: Path) -> None:
    """Finding 5 : une note récente WIN sans rapport avec la query ne remonte pas.

    Scénario : 2 notes — "momentum cassure breakout" et "dividende bilan annuel".
    La note dividende est récente et a outcome_score=+0.15 (WIN).
    La query = "momentum breakout" → FTS5 ne match que la première.
    Sans restriction, la note dividende (récente+WIN) pourrait remonter grâce
    au score freshness+outcome. Avec restriction query → elle ne doit pas apparaître.
    """
    rows = [
        {
            "ts": "2026-01-01T00:00:00+00:00",  # ancienne
            "symbol": "NVDA",
            "note": "momentum cassure breakout résistance signal fort",
            "action": "BUY",
            "intent": "BUY",
            "executed": True,
            "reason": "breakout",
            "decision_id": "query-nvda-1",
        },
        {
            "ts": "2026-06-30T00:00:00+00:00",  # très récente → freshness haute
            "symbol": "AAPL",
            "note": "dividende bilan annuel résultats trimestriels",
            "action": "HOLD",
            "intent": "HOLD",
            "executed": False,
            "reason": "hold",
            "decision_id": "query-aapl-1",
        },
    ]
    jsonl = tmp_path / "q.jsonl"
    _write_jsonl(jsonl, rows)
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")

    # Donner un outcome_score élevé à la note AAPL (WIN récente)
    store._conn.execute(
        "UPDATE notes SET outcome_score=0.15, verdict='WIN' WHERE decision_id='query-aapl-1'"
    )
    store._conn.commit()

    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    results = store.search(text_query="momentum breakout", now=now)

    result_ids = {r["symbol"] for r in results}
    assert "AAPL" not in result_ids, (
        "AAPL (dividende, sans rapport avec 'momentum breakout') ne doit pas remonter quand une query est fournie"
    )
    # NVDA doit quand même apparaître (match FTS)
    assert "NVDA" in result_ids, "NVDA (match FTS 'momentum breakout') doit apparaître"


def test_search_verdict_null_retourne_unknown(tmp_path: Path) -> None:
    """Finding 6 : verdict NULL en base → UNKNOWN dans les résultats search."""
    store = _setup_search_store(tmp_path)
    # Les notes insérées n'ont pas de verdict → NULL en base
    results = store.search()
    assert len(results) > 0
    for r in results:
        assert r["verdict"] == "UNKNOWN", (
            f"verdict NULL doit être retourné comme UNKNOWN, got {r['verdict']!r}"
        )


def test_search_query_vide_equivaut_a_pas_de_query(tmp_path) -> None:
    """text_query='' ne doit pas activer la restriction FTS∪cosine (re-review Codex)."""
    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "notes.jsonl"
    _write_jsonl(jsonl, [
        {"ts": "2026-07-01T00:00:00+00:00", "symbol": "SPY", "note": "note dividende", "decision_id": "d1"},
    ])
    store.ingest_jsonl(jsonl, source="runtime")
    with_empty = store.search(symbol="SPY", text_query="")
    without = store.search(symbol="SPY")
    assert len(with_empty) == len(without) == 1


def test_search_query_take_profit_ne_plante_pas(tmp_path: Path) -> None:
    """Incident CRM 2026-08-14 : `take-profit` → OperationalError no such column: profit."""
    jsonl = tmp_path / "notes.jsonl"
    _write_jsonl(jsonl, [
        {
            "ts": "2026-08-14T13:00:00+00:00",
            "symbol": "CRM",
            "note": "Long take-profit at highs after a stretched pullback retest.",
            "decision_id": "crm-tp-1",
        },
        {
            "ts": "2026-08-14T13:00:00+00:00",
            "symbol": "CRM",
            "note": "Dividende sans rapport.",
            "decision_id": "crm-div-1",
        },
    ])
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    now = datetime(2026, 8, 14, 14, tzinfo=timezone.utc)
    # Query de prod : AND strict, termes absents → 0 hit, mais plus de crash.
    assert store.search(
        symbol="CRM",
        text_query="long take-profit at highs stretched pullback retest software us_tech",
        now=now,
    ) == []
    results = store.search(symbol="CRM", text_query="take-profit pullback retest", now=now)
    assert len(results) == 1
    assert "take-profit" in results[0]["note"]


def test_search_query_ticker_avec_point_ne_plante_pas(tmp_path: Path) -> None:
    """Incident INGA.AS 2026-08-14 : `INGA.AS` → fts5 syntax error near \".\"."""
    jsonl = tmp_path / "notes.jsonl"
    _write_jsonl(jsonl, [
        {
            "ts": "2026-08-14T07:00:00+00:00",
            "symbol": "INGA.AS",
            "note": "INGA.AS eu_financials pullback reclaim after daily uptrend.",
            "decision_id": "inga-1",
        },
    ])
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    now = datetime(2026, 8, 14, 8, tzinfo=timezone.utc)
    assert store.search(
        symbol="INGA.AS",
        text_query="INGA.AS eu_financials pullback reclaim long after daily uptrend range 15m swing low",
        now=now,
    ) == []
    results = store.search(
        symbol="INGA.AS",
        text_query="INGA.AS eu_financials pullback reclaim",
        now=now,
    )
    assert len(results) == 1
    assert "eu_financials" in results[0]["note"]


def test_search_query_operateurs_seuls_ne_plante_pas(tmp_path: Path) -> None:
    jsonl = tmp_path / "notes.jsonl"
    _write_jsonl(jsonl, [
        {"ts": "2026-08-14T07:00:00+00:00", "symbol": "SPY", "note": "note dividende", "decision_id": "d1"},
    ])
    store = LearningsStore(tmp_path / "learnings.db")
    store.ingest_jsonl(jsonl, source="test")
    now = datetime(2026, 8, 14, 8, tzinfo=timezone.utc)
    assert store.search(symbol="SPY", text_query="---", now=now) == []
    assert store.search(symbol="SPY", text_query="AND OR NOT", now=now) == []


# ---------------------------------------------------------------------------
# Outcome-weighted curation + global-rule MemRL
# ---------------------------------------------------------------------------


def test_curation_revisions_preservent_un_feedback_arrive_pendant_la_consolidation(
    tmp_path: Path,
) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "note.jsonl"
    _write_jsonl(jsonl, [{
        "ts": "2026-07-10T10:00:00+00:00",
        "symbol": "SPY",
        "note": "Attendre une confirmation avant de renforcer.",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "decision_id": "curation-decision",
    }])
    store.ingest_jsonl(jsonl, source="runtime")

    assert store.curation_counts()["new"] == 1
    snapshot = store.select_curation_candidates()
    assert len(snapshot) == 1
    assert snapshot[0]["status"] == "pending"
    assert {"note_id", "decision_id", "text", "family", "intent", "executed"} <= set(snapshot[0])

    # The outcome arrives while the consolidator still holds revision 1.
    assert store.update_note_outcomes([{
        "id": snapshot[0]["id"],
        "verdict": "WIN",
        "forward_return": 0.02,
    }]) == 1
    assert store.mark_curation_candidates_curated(snapshot) == 1
    counts = store.curation_counts()
    assert counts["new"] == 0
    assert counts["feedback"] == 1

    refreshed = store.select_curation_candidates()
    assert refreshed[0]["status"] == "evaluated"
    assert store.mark_curation_candidates_curated(refreshed) == 1
    assert store.curation_counts()["changed"] == 0

    feedback = store.feedback_by_decision_ids(["curation-decision"])
    assert feedback["curation-decision"]["status"] == "evaluated"
    assert feedback["curation-decision"]["verdict"] == "WIN"
    assert feedback["curation-decision"]["forward_return"] == pytest.approx(0.02)


def test_pending_curation_snapshots_marquent_le_leftover_sans_manger_linflight(
    tmp_path: Path,
) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "two.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "ts": "2026-07-10T10:00:00+00:00",
                "symbol": "AAA",
                "note": "leftover",
                "action": "HOLD",
                "intent": "HOLD",
                "executed": False,
                "decision_id": "snap-a",
            },
            {
                "ts": "2026-07-10T10:00:00+00:00",
                "symbol": "BBB",
                "note": "inflight",
                "action": "HOLD",
                "intent": "HOLD",
                "executed": False,
                "decision_id": "snap-b",
            },
        ],
    )
    store.ingest_jsonl(jsonl, source="runtime")
    snapshot = store.pending_curation_snapshots()
    assert len(snapshot) == 2
    inflight_id = next(
        int(row["id"])
        for row in store._conn.execute("SELECT id, decision_id FROM notes")
        if row["decision_id"] == "snap-b"
    )
    assert store.update_note_outcomes(
        [{"id": inflight_id, "verdict": "WIN", "forward_return": 0.02}]
    ) == 1
    assert store.mark_curation_candidates_curated(snapshot) == 2
    counts = store.curation_counts()
    assert counts["new"] == 0
    assert counts["feedback"] == 1
    store.close()


def test_curation_candidates_are_diverse_and_bounded_by_tranche(tmp_path: Path) -> None:
    rows: list[dict] = []
    verdicts: list[dict] = []
    for symbol, verdict, prefix in (
        ("AAA", "WIN", "a"),
        ("CCC", "WIN", "c"),
        ("BBB", "LOSS", "b"),
        ("DDD", "LOSS", "d"),
    ):
        for index in range(3):
            decision_id = f"{prefix}-{index}"
            rows.append({
                "ts": f"2026-07-0{index + 1}T10:00:00+00:00",
                "symbol": symbol,
                "note": f"{symbol} learning {index}",
                "action": "BUY",
                "intent": "OPEN_LONG",
                "executed": True,
                "decision_id": decision_id,
            })
            verdicts.append({
                "decision_id": decision_id,
                "verdict": verdict,
                "forward_return": 0.03 if verdict == "WIN" else -0.03,
            })

    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "diverse.jsonl"
    _write_jsonl(jsonl, rows)
    store.ingest_jsonl(jsonl, source="runtime")
    store.update_note_outcomes([
        {"id": row[0], "verdict": verdict["verdict"], "forward_return": verdict["forward_return"]}
        for verdict in verdicts
        for row in [store._conn.execute(
            "SELECT id FROM notes WHERE decision_id=?", (verdict["decision_id"],)
        ).fetchone()]
    ])
    store.compute_outcome_scores()
    # Ack the first pass so the next call is purely the historical pool.
    store.mark_curation_candidates_curated(store.select_curation_candidates())

    candidates = store.select_curation_candidates(
        recent_limit=0,
        positive_limit=4,
        counterexample_limit=4,
        max_per_symbol=2,
    )
    assert len(candidates) == 8
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate["symbol"]] = counts.get(candidate["symbol"], 0) + 1
        assert candidate["status"] == "evaluated"
        assert {"verdict", "forward_return", "outcome_score", "q_value", "q_updates"} <= set(candidate)
    assert counts == {"AAA": 2, "CCC": 2, "BBB": 2, "DDD": 2}


def test_global_rule_memrl_is_idempotent_and_keeps_retired_rule_history(tmp_path: Path) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    synced = store.sync_global_rules(["rule-breakout", "rule-risk"], ts="2026-07-10T00:00:00+00:00")
    assert synced == {"created": 2, "reactivated": 0, "retired": 0, "active": 2}
    assert store.record_global_rule_citation(
        decision_id="decision-with-rules",
        rule_ids=["rule-breakout", "rule-risk"],
        ts="2026-07-10T01:00:00+00:00",
    )
    assert store.pending_global_rule_decision_ids() == ["decision-with-rules"]

    first = store.apply_global_rule_outcome(
        decision_id="decision-with-rules",
        verdict="WIN",
        reward=1.0,
        forward_return=0.02,
        evaluated_at="2026-07-11T01:00:00+00:00",
    )
    second = store.apply_global_rule_outcome(
        decision_id="decision-with-rules",
        verdict="LOSS",
        reward=-1.0,
        forward_return=-0.02,
        evaluated_at="2026-07-12T01:00:00+00:00",
    )
    assert first == {
        "citations_updated": 1,
        "rules_updated": 2,
        "rule_ids": ["rule-breakout", "rule-risk"],
    }
    assert second == {"citations_updated": 0, "rules_updated": 0, "rule_ids": []}
    assert store.global_rule_scores()["rule-breakout"]["q_value"] == pytest.approx(0.1)
    assert store.global_rule_scores()["rule-risk"]["q_updates"] == 1

    store.sync_global_rules(["rule-breakout"], ts="2026-07-12T02:00:00+00:00")
    scores = store.global_rule_scores()
    assert scores["rule-risk"]["active"] is False
    assert scores["rule-risk"]["q_value"] == pytest.approx(0.1)
    with pytest.raises(ValueError, match="inactive or unknown"):
        store.record_global_rule_citation(
            decision_id="invalid-citation",
            rule_ids=["rule-risk"],
        )


def test_global_rule_lifecycle_does_not_follow_a_regressing_business_watermark(tmp_path: Path) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    created_clock = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    regressed_clock = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)

    store.sync_global_rules(
        ["rule-short-win"],
        ts="2026-07-10T00:00:00+00:00",
        now=created_clock,
    )
    store.sync_global_rules(
        [],
        ts="2026-07-09T00:00:00+00:00",
        now=regressed_clock,
    )

    lifecycle = store.global_rule_scores()["rule-short-win"]
    assert lifecycle["created_at"] == created_clock.isoformat()
    assert lifecycle["retired_at"] == created_clock.isoformat()
    assert lifecycle["updated_at"] == created_clock.isoformat()


def test_schema_migrates_curation_and_global_rule_tables_additively(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT, q_value REAL)")
    conn.execute(
        "CREATE TABLE recalls (id INTEGER PRIMARY KEY, decision_id TEXT, note_ids TEXT, ts TEXT)"
    )
    conn.commit()
    conn.close()

    store = LearningsStore(db_path)
    note_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(notes)")}
    tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"curation_revision", "curated_revision", "curation_updated_at"} <= note_columns
    assert {"global_rules", "global_rule_citations"} <= tables


def test_benchmark_v2_migration_invalidates_derived_feedback_once(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v1.db"
    jsonl = tmp_path / "legacy-v1.jsonl"
    _write_jsonl(jsonl, [_ROWS[2]])
    store = LearningsStore(db_path)
    store.ingest_jsonl(jsonl, source="runtime")
    note_id = int(store._conn.execute("SELECT id FROM notes").fetchone()[0])
    store.update_note_outcomes([{
        "id": note_id,
        "verdict": "WIN",
        "forward_return": 0.05,
    }])
    store.record_recall(decision_id="legacy-recall", note_ids=[note_id])
    store.apply_recall_outcome(
        decision_id="legacy-recall",
        verdict="WIN",
        reward=1.0,
        forward_return=0.03,
        evaluated_at="2026-07-10T00:00:00+00:00",
    )
    store.sync_global_rules(["legacy-rule"], ts="2026-07-09T00:00:00+00:00")
    store.record_global_rule_citation(
        decision_id="legacy-citation",
        rule_ids=["legacy-rule"],
        ts="2026-07-09T01:00:00+00:00",
    )
    store.apply_global_rule_outcome(
        decision_id="legacy-citation",
        verdict="LOSS",
        reward=-1.0,
        forward_return=-0.04,
        evaluated_at="2026-07-10T01:00:00+00:00",
    )
    store._conn.execute(
        """
        UPDATE notes
        SET outcome_score=0.4, q_value=0.7, q_updates=9,
            outcome_semantics_version=1,
            curated_revision=curation_revision
        """
    )
    store._conn.execute("UPDATE recalls SET outcome_semantics_version=1")
    store._conn.execute(
        "UPDATE global_rule_citations SET outcome_semantics_version=1"
    )
    store._conn.execute(
        "UPDATE global_rules SET q_value=-0.6, q_updates=8"
    )
    revision_before = int(
        store._conn.execute("SELECT curation_revision FROM notes").fetchone()[0]
    )
    store._conn.execute(
        "DELETE FROM learnings_metadata WHERE key='outcome_semantics_version'"
    )
    store._conn.commit()
    store.close()

    migrated = LearningsStore(db_path)
    note = migrated._conn.execute(
        """
        SELECT verdict, forward_return, outcome_score,
               outcome_semantics_version, q_value, q_updates,
               curation_revision, curated_revision
        FROM notes
        """
    ).fetchone()
    recall = migrated._conn.execute(
        """
        SELECT verdict, reward, forward_return, evaluated_at,
               outcome_semantics_version
        FROM recalls
        """
    ).fetchone()
    citation = migrated._conn.execute(
        """
        SELECT verdict, reward, forward_return, evaluated_at,
               outcome_semantics_version
        FROM global_rule_citations
        """
    ).fetchone()

    assert tuple(note) == (
        None,
        0.05,
        None,
        None,
        0.0,
        0,
        revision_before + 1,
        revision_before + 1,
    )
    assert tuple(recall) == (None, None, None, None, None)
    assert tuple(citation) == (None, None, None, None, None)
    assert migrated.global_rule_scores()["legacy-rule"]["q_value"] == 0.0
    assert migrated.global_rule_scores()["legacy-rule"]["q_updates"] == 0
    assert migrated.pending_recall_decision_ids() == ["legacy-recall"]
    assert migrated.pending_global_rule_decision_ids() == ["legacy-citation"]
    assert migrated.feedback_by_decision_ids([_ROWS[2]["decision_id"]]) == {
        _ROWS[2]["decision_id"]: {
            "status": "pending",
            "verdict": None,
            "forward_return": None,
            "outcome_score": None,
            "q_value": 0.0,
            "q_updates": 0,
        }
    }
    assert migrated.curation_counts()["changed"] == 0
    assert migrated.select_curation_candidates() == []

    fresh_jsonl = tmp_path / "fresh-after-migration.jsonl"
    fresh_row = {
        **_ROWS[0],
        "decision_id": "fresh-after-v2-migration",
        "ts": "2026-07-11T00:00:00+00:00",
    }
    _write_jsonl(fresh_jsonl, [fresh_row])
    migrated.ingest_jsonl(fresh_jsonl, source="runtime")
    assert migrated.curation_counts() == {
        "new": 1,
        "feedback": 0,
        "changed": 1,
        "oldest_changed_at": fresh_row["ts"],
    }
    assert [
        candidate["decision_id"]
        for candidate in migrated.select_curation_candidates()
    ] == ["fresh-after-v2-migration"]
    migrated.close()

    reopened = LearningsStore(db_path)
    assert reopened._conn.execute(
        "SELECT curation_revision FROM notes"
    ).fetchone()[0] == revision_before + 1


def test_fresh_store_is_marked_v2_without_resetting_later_ingestion(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    store = LearningsStore(db_path)
    jsonl = tmp_path / "fresh.jsonl"
    _write_jsonl(jsonl, [_ROWS[0]])
    store.ingest_jsonl(jsonl, source="runtime")
    store.close()

    reopened = LearningsStore(db_path)
    row = reopened._conn.execute(
        "SELECT curation_revision, outcome_semantics_version FROM notes"
    ).fetchone()
    marker = reopened._conn.execute(
        "SELECT value FROM learnings_metadata WHERE key='outcome_semantics_version'"
    ).fetchone()[0]

    assert tuple(row) == (1, None)
    assert marker == str(BENCHMARK_SEMANTICS_VERSION)


def test_apply_verdicts_rejects_legacy_bootstrap(tmp_path: Path) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "note.jsonl"
    _write_jsonl(jsonl, [_ROWS[0]])
    store.ingest_jsonl(jsonl, source="runtime")
    bootstrap = tmp_path / "legacy-bootstrap.json"
    bootstrap.write_text(
        json.dumps({
            "learnings": [{
                "decision_id": _ROWS[0]["decision_id"],
                "verdict": "WIN",
                "forward_return": 0.04,
            }]
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outcome_semantics_version"):
        store.apply_verdicts(bootstrap)

    assert store.feedback_by_decision_ids([_ROWS[0]["decision_id"]])[
        _ROWS[0]["decision_id"]
    ]["status"] == "pending"


def test_legacy_feedback_is_pending_and_not_a_historical_positive(tmp_path: Path) -> None:
    rows = [
        {**_ROWS[2], "decision_id": "legacy-positive", "symbol": "OLD"},
        {**_ROWS[2], "decision_id": "v2-positive", "symbol": "NEW"},
    ]
    jsonl = tmp_path / "mixed.jsonl"
    _write_jsonl(jsonl, rows)
    store = LearningsStore(tmp_path / "mixed.db")
    store.ingest_jsonl(jsonl, source="runtime")
    ids = {
        row[0]: row[1]
        for row in store._conn.execute("SELECT decision_id, id FROM notes")
    }
    store.update_note_outcomes([
        {"id": ids["legacy-positive"], "verdict": "WIN", "forward_return": 0.09},
        {"id": ids["v2-positive"], "verdict": "WIN", "forward_return": 0.03},
    ])
    store._conn.execute(
        """
        UPDATE notes
        SET outcome_semantics_version=1
        WHERE decision_id='legacy-positive'
        """
    )
    store._conn.commit()
    store.mark_curation_candidates_curated(store.select_curation_candidates())

    candidates = store.select_curation_candidates(
        recent_limit=0,
        positive_limit=10,
        counterexample_limit=0,
    )
    feedback = store.feedback_by_decision_ids(["legacy-positive"])

    assert [row["decision_id"] for row in candidates] == ["v2-positive"]
    assert feedback["legacy-positive"]["status"] == "pending"
    assert feedback["legacy-positive"]["verdict"] is None


def test_pending_memrl_replay_is_chronological_after_v2_reset(tmp_path: Path) -> None:
    store = LearningsStore(tmp_path / "chronological.db")
    store.record_recall(decision_id="recall-old", note_ids=[])
    store.record_recall(decision_id="recall-new", note_ids=[])
    store.sync_global_rules(["rule"], ts="2026-07-01T00:00:00+00:00")
    store.record_global_rule_citation(
        decision_id="citation-new",
        rule_ids=["rule"],
        ts="2026-07-03T00:00:00+00:00",
    )
    store.record_global_rule_citation(
        decision_id="citation-old",
        rule_ids=["rule"],
        ts="2026-07-02T00:00:00+00:00",
    )

    assert store.pending_recall_decision_ids() == ["recall-old", "recall-new"]
    assert store.pending_global_rule_decision_ids() == [
        "citation-old",
        "citation-new",
    ]
