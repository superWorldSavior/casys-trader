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

from trader.learnings_store import LearningsStore


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
        "verdict", "forward_return", "outcome_score", "q_value", "embedding",
    }
    assert expected.issubset(cols), f"Colonnes manquantes : {expected - cols}"


def test_schema_recalls_colonnes(tmp_path: Path) -> None:
    """La table recalls possède les colonnes attendues."""
    LearningsStore(tmp_path / "learnings.db")
    conn = sqlite3.connect(str(tmp_path / "learnings.db"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(recalls)")}
    conn.close()
    assert {"id", "decision_id", "note_ids", "ts"}.issubset(cols)


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
