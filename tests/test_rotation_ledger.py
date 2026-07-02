"""Tests pour trader.rotation_ledger — journal des décisions de composition d'univers."""

from __future__ import annotations

import json


from trader.rotation_ledger import log_rotation


def test_log_rotation_ecrit_une_ligne_json_relisible(tmp_path):
    """Un appel append une ligne JSON valide avec tous les champs."""
    path = tmp_path / "logs" / "rotation.jsonl"
    log_rotation(
        path,
        as_of="2026-06-15T09:00:00Z",
        default_hot={"AAPL", "MSFT"},
        final_hot={"AAPL", "GOOG"},
        sticky={"AAPL"},
        overrides={"GOOG": "manual"},
        rejects={"TSLA": "momentum_fail"},
        alerts=["drawdown_alert"],
    )
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["source"] == "rotation"
    assert obj["as_of"] == "2026-06-15T09:00:00Z"
    assert set(obj["default_hot_set"]) == {"AAPL", "MSFT"}
    assert set(obj["final_hot_set"]) == {"AAPL", "GOOG"}
    assert obj["sticky"] == sorted({"AAPL"})
    assert obj["overrides"] == {"GOOG": "manual"}
    assert obj["rejects"] == {"TSLA": "momentum_fail"}
    assert obj["alerts"] == ["drawdown_alert"]


def test_log_rotation_deux_appels_deux_lignes(tmp_path):
    """Deux appels successifs → deux lignes distinctes (append-only)."""
    path = tmp_path / "rotation.jsonl"
    log_rotation(
        path,
        as_of="2026-06-15T09:00:00Z",
        default_hot={"A"},
        final_hot={"A"},
        sticky=set(),
        overrides={},
        rejects={},
        alerts=[],
    )
    log_rotation(
        path,
        as_of="2026-06-15T10:00:00Z",
        default_hot={"B"},
        final_hot={"B"},
        sticky=set(),
        overrides={},
        rejects={},
        alerts=["alert2"],
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["as_of"] == "2026-06-15T09:00:00Z"
    assert second["as_of"] == "2026-06-15T10:00:00Z"
    assert second["alerts"] == ["alert2"]


def test_log_rotation_cree_dossier_parent(tmp_path):
    """Le dossier parent est créé automatiquement s'il n'existe pas."""
    path = tmp_path / "a" / "b" / "c" / "rotation.jsonl"
    log_rotation(
        path,
        as_of="2026-06-15T09:00:00Z",
        default_hot=set(),
        final_hot=set(),
        sticky=set(),
        overrides={},
        rejects={},
        alerts=[],
    )
    assert path.exists()


def test_log_rotation_sticky_est_trie(tmp_path):
    """sticky (set) est sérialisé comme liste triée."""
    path = tmp_path / "rotation.jsonl"
    log_rotation(
        path,
        as_of="2026-06-15T09:00:00Z",
        default_hot=set(),
        final_hot=set(),
        sticky={"MSFT", "AAPL", "GOOG"},
        overrides={},
        rejects={},
        alerts=[],
    )
    obj = json.loads(path.read_text().splitlines()[0])
    assert obj["sticky"] == sorted(["MSFT", "AAPL", "GOOG"])
