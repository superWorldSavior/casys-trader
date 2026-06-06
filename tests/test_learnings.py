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
