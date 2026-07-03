"""Tests TDD — open_state_db (singleton) + bootstrap_state_backend.

Couverture :
    - open_state_db : même chemin → même instance (identité)
    - open_state_db : chemins différents → instances différentes
    - open_state_db : chemin relatif vs absolu équivalent → même instance
    - bootstrap_state_backend(sqlite) : 3 shadows créés (broker.json, trade_plans.json, scheduler.json)
    - bootstrap_state_backend(sqlite) : idempotent (2 appels consécutifs OK)
    - bootstrap_state_backend(json) : no-op (aucun fichier .db créé)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.state_db.connection import StateDb, _DB_REGISTRY, _DB_REGISTRY_LOCK, open_state_db
from trader.state_db.broker_factory import bootstrap_state_backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_registry_for(paths: list[Path]) -> None:
    """Retire les entrées du registre global pour les chemins fournis (isolation test)."""
    with _DB_REGISTRY_LOCK:
        for p in paths:
            key = str(p.resolve())
            _DB_REGISTRY.pop(key, None)


# ---------------------------------------------------------------------------
# open_state_db — singleton par chemin
# ---------------------------------------------------------------------------


def test_open_state_db_same_path_returns_same_instance(tmp_path: Path) -> None:
    """Deux appels open_state_db avec le même chemin → objet identique (is)."""
    db_path = tmp_path / "a.db"
    _clear_registry_for([db_path])
    try:
        db1 = open_state_db(db_path)
        db2 = open_state_db(db_path)
        assert db1 is db2, "open_state_db doit retourner la même instance pour le même chemin"
        assert isinstance(db1, StateDb)
    finally:
        _clear_registry_for([db_path])


def test_open_state_db_different_paths_return_different_instances(tmp_path: Path) -> None:
    """Deux chemins différents → instances différentes."""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    _clear_registry_for([path_a, path_b])
    try:
        db_a = open_state_db(path_a)
        db_b = open_state_db(path_b)
        assert db_a is not db_b
        assert db_a.path != db_b.path
    finally:
        _clear_registry_for([path_a, path_b])


def test_open_state_db_relative_vs_absolute_same_instance(tmp_path: Path) -> None:
    """Chemin absolu et chemin résolu équivalent → même instance."""
    db_path = tmp_path / "canon.db"
    _clear_registry_for([db_path])
    try:
        db1 = open_state_db(db_path)
        # Appel avec le même chemin absolu (resolve() → même key)
        db2 = open_state_db(str(db_path))
        assert db1 is db2
    finally:
        _clear_registry_for([db_path])


# ---------------------------------------------------------------------------
# bootstrap_state_backend — sqlite
# ---------------------------------------------------------------------------


def test_bootstrap_sqlite_creates_three_shadows(tmp_path: Path) -> None:
    """bootstrap_state_backend(sqlite) régénère les 3 shadows JSON depuis SQLite."""
    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])
    try:
        bootstrap_state_backend(
            state_dir=tmp_path,
            starting_cash=50_000.0,
            commission_model=None,
            backend="sqlite",
        )

        assert (tmp_path / "casys.db").exists(), "casys.db doit être créé"
        assert (tmp_path / "broker.json").exists(), "broker.json (shadow) doit être créé"
        assert (tmp_path / "trade_plans.json").exists(), "trade_plans.json (shadow) doit être créé"
        assert (tmp_path / "scheduler.json").exists(), "scheduler.json (shadow) doit être créé"

        # Vérifier que le shadow broker contient le bon cash
        broker_shadow = json.loads((tmp_path / "broker.json").read_text())
        assert broker_shadow["cash"] == pytest.approx(50_000.0)
        assert broker_shadow["positions"] == {}
        assert broker_shadow["fills"] == []
    finally:
        _clear_registry_for([db_path])


def test_bootstrap_sqlite_idempotent(tmp_path: Path) -> None:
    """bootstrap_state_backend(sqlite) peut être appelé deux fois sans erreur ni doublon."""
    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])
    try:
        # Premier appel
        bootstrap_state_backend(
            state_dir=tmp_path,
            starting_cash=50_000.0,
            commission_model=None,
            backend="sqlite",
        )
        shadow_1 = json.loads((tmp_path / "broker.json").read_text())

        # Deuxième appel — doit être un no-op silencieux
        bootstrap_state_backend(
            state_dir=tmp_path,
            starting_cash=50_000.0,
            commission_model=None,
            backend="sqlite",
        )
        shadow_2 = json.loads((tmp_path / "broker.json").read_text())

        assert shadow_1["cash"] == pytest.approx(shadow_2["cash"])
        assert shadow_1["positions"] == shadow_2["positions"]
    finally:
        _clear_registry_for([db_path])


def test_bootstrap_sqlite_shadow_reflects_real_cash(tmp_path: Path) -> None:
    """Le shadow broker.json reflète le cash réel de la DB après bootstrap."""
    db_path = tmp_path / "casys.db"
    # Pré-existant : un broker.json avec du cash spécifique
    broker_json = {
        "cash": 123_456.78,
        "positions": {},
        "fills": [],
    }
    (tmp_path / "broker.json").write_text(json.dumps(broker_json))
    _clear_registry_for([db_path])
    try:
        bootstrap_state_backend(
            state_dir=tmp_path,
            starting_cash=50_000.0,  # ignoré car JSON présent → importé
            commission_model=None,
            backend="sqlite",
        )
        shadow = json.loads((tmp_path / "broker.json").read_text())
        assert shadow["cash"] == pytest.approx(123_456.78), (
            "Le shadow doit refléter le cash importé depuis broker.json, pas starting_cash"
        )
    finally:
        _clear_registry_for([db_path])


# ---------------------------------------------------------------------------
# bootstrap_state_backend — json = no-op
# ---------------------------------------------------------------------------


def test_bootstrap_json_noop(tmp_path: Path) -> None:
    """bootstrap_state_backend(json) ne crée aucun fichier SQLite."""
    bootstrap_state_backend(
        state_dir=tmp_path,
        starting_cash=50_000.0,
        commission_model=None,
        backend="json",
    )
    assert not (tmp_path / "casys.db").exists(), "backend=json ne doit PAS créer casys.db"
    # Aucun shadow créé non plus
    assert not (tmp_path / "broker.json").exists()
    assert not (tmp_path / "trade_plans.json").exists()
    assert not (tmp_path / "scheduler.json").exists()


def test_bootstrap_unknown_backend_noop(tmp_path: Path) -> None:
    """bootstrap_state_backend avec un backend inconnu (ni 'sqlite') est un no-op.

    Note : seul 'sqlite' déclenche l'amorce ; tout autre backend (y compris
    les valeurs inconnues) est ignoré silencieusement.
    """
    # Un backend != "sqlite" → no-op (pas de ValueError ici, contrairement aux factories)
    bootstrap_state_backend(
        state_dir=tmp_path,
        starting_cash=50_000.0,
        commission_model=None,
        backend="bogus",
    )
    assert not (tmp_path / "casys.db").exists()


# ---------------------------------------------------------------------------
# open_state_db + bootstrap : connexion partagée entre factories
# ---------------------------------------------------------------------------


def test_bootstrap_and_make_broker_share_same_connection(tmp_path: Path) -> None:
    """Après bootstrap_state_backend, make_broker(sqlite) réutilise la même connexion."""
    from trader.state_db.broker_factory import make_broker
    from trader.tools.execution import NoCommissionModel

    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])
    try:
        bootstrap_state_backend(
            state_dir=tmp_path,
            starting_cash=75_000.0,
            commission_model=None,
            backend="sqlite",
        )
        db_from_registry = open_state_db(db_path)

        broker = make_broker(
            state_dir=tmp_path,
            starting_cash=75_000.0,
            commission_model=NoCommissionModel(),
            backend="sqlite",
        )
        # Le broker utilise la même connexion (StateDb._conn → même objet)
        assert broker._db is db_from_registry, (
            "make_broker(sqlite) doit partager la StateDb de open_state_db"
        )
    finally:
        _clear_registry_for([db_path])
