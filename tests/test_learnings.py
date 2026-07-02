from datetime import datetime, timezone

from trader.tools.memory import LearningsStore


def test_recent_renvoie_les_entrees_dans_lordre_dajout(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl")
    now = datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc)
    store.append(symbol="SPY", note="momentum faible, j'attends une cassure", now=now)
    store.append(symbol="QQQ", note="spread vs famille trop tendu", now=now)

    recent = store.recent(limit=10)
    assert [item["symbol"] for item in recent] == ["SPY", "QQQ"]
    assert recent[0]["note"] == "momentum faible, j'attends une cassure"
    assert recent[0]["ts"] == now.isoformat()


def test_recent_limit_renvoie_les_plus_recents(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl")
    for i in range(5):
        store.append(symbol="SPY", note=f"note {i}")

    recent = store.recent(limit=2)
    assert [item["note"] for item in recent] == ["note 3", "note 4"]


def test_append_borne_le_fichier_a_max_entries(tmp_path) -> None:
    path = tmp_path / "learnings.jsonl"
    store = LearningsStore(path, max_entries=3)
    for i in range(10):
        store.append(symbol="SPY", note=f"note {i}")

    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert [item["note"] for item in store.recent(limit=10)] == ["note 7", "note 8", "note 9"]


def test_recent_sur_fichier_absent_renvoie_vide(tmp_path) -> None:
    store = LearningsStore(tmp_path / "absent.jsonl")
    assert store.recent() == []


def test_recent_limit_non_positif_renvoie_vide(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl")
    store.append(symbol="SPY", note="x")
    store.append(symbol="QQQ", note="y")
    assert store.recent(limit=0) == []
    assert store.recent(limit=-3) == []


def test_max_entries_non_positif_ne_stocke_rien(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=0)
    store.append(symbol="SPY", note="x")
    assert store.recent(limit=10) == []


def test_append_renvoie_true_si_ecrit_false_si_vide(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl")
    assert store.append(symbol="SPY", note="vraie note") is True
    assert store.append(symbol="SPY", note="   ") is False


def test_append_ignore_une_note_vide(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl")
    store.append(symbol="SPY", note="   ")
    store.append(symbol="SPY", note="vraie note")
    assert [item["note"] for item in store.recent()] == ["vraie note"]


def test_eviction_archivee_au_lieu_de_jetee(tmp_path) -> None:
    """Le rolling buffer n'a plus le droit de jeter : les évincés partent en archive
    append-only datée (chantier learnings 2026-07-02)."""
    store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=3)
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    for i in range(5):
        store.append(symbol=f"S{i}", note=f"note {i}", now=base.replace(minute=i))

    # Le vif garde les 3 dernières
    assert [r["symbol"] for r in store.all()] == ["S2", "S3", "S4"]
    # Les 2 plus anciennes sont archivées, ts d'origine conservé + evicted_at
    archive = tmp_path / "archive" / "learnings-evicted.jsonl"
    assert archive.exists()
    import json as _json
    rows = [_json.loads(line) for line in archive.read_text().splitlines()]
    assert [r["symbol"] for r in rows] == ["S0", "S1"]
    assert rows[0]["ts"] == base.replace(minute=0).isoformat()
    assert all("evicted_at" in r for r in rows)


def test_pas_deviction_pas_darchive(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=10)
    store.append(symbol="SPY", note="rien à évincer", now=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert not (tmp_path / "archive" / "learnings-evicted.jsonl").exists()


# ---------------------------------------------------------------------------
# Finding 2 — _archive_evicted : OSError → log.warning + vif toujours écrit
# ---------------------------------------------------------------------------


def test_archive_evicted_loggue_warning_si_ioerror(tmp_path) -> None:
    """Finding 2 — échec d'archivage des évincés → log.warning (pas de perte silencieuse).

    Le vif (buffer actif) doit quand même être écrit ; l'archive est best-effort.
    Technique : on place un FICHIER à l'emplacement du répertoire d'archive
    → Path.mkdir lève NotADirectoryError (sous-classe d'OSError).
    """
    import logging

    # Créer un FICHIER là où LearningsStore voudrait créer un répertoire "archive"
    archive_dir = tmp_path / "archive"
    archive_dir.write_text("not a directory")  # bloque le mkdir de _archive_evicted

    store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=2)
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[assignment]
    mod_logger = logging.getLogger("trader.tools.memory")
    mod_logger.addHandler(handler)
    try:
        # Déclenche une éviction (max_entries=2, on append 3 notes)
        for i in range(3):
            store.append(symbol=f"S{i}", note=f"note {i}", now=base.replace(minute=i))
    finally:
        mod_logger.removeHandler(handler)

    # Le vif est bien écrit (2 dernières entrées)
    assert len(store.all()) == 2

    # Un log.warning a été émis
    assert any(rec.levelno == logging.WARNING for rec in records), (
        "Un log.warning doit être émis quand l'archivage des évincés échoue"
    )
