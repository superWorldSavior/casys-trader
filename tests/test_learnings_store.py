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
