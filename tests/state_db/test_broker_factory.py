"""Tests TDD pour make_broker (trader/state_db/broker_factory.py).

Couverture :
    - backend="json" → SimBroker, crée/lit broker.json
    - backend="sqlite" avec broker.json existant → SqliteBroker, état migré
    - backend="sqlite" sans broker.json + starting_cash=50000 → cash==50000
    - backend="bogus" → ValueError
    - défaut absent de l'env (comportement json si CASYS_STATE_BACKEND non posé)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trader.state_db.broker_factory import make_broker
from trader.tools.execution import NoCommissionModel, Order, SimBroker
from trader.state_db.broker_store import SqliteBroker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NO_COMMISSION = NoCommissionModel()


def _broker_json_fixture(state_dir: Path, cash: float = 42_000.0) -> None:
    """Écrit un broker.json minimal dans state_dir."""
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "cash": cash,
        "positions": {
            "AAPL": {"symbol": "AAPL", "quantity": 10.0, "avg_price": 170.0}
        },
        "fills": [
            {
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 10.0,
                "price": 170.0,
                "ts": "2026-07-01T10:00:00+00:00",
            }
        ],
    }
    (state_dir / "broker.json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Test 1 — backend="json" → SimBroker
# ---------------------------------------------------------------------------


def test_make_broker_json_returns_simbroker(tmp_path: Path) -> None:
    """make_broker(backend="json") doit retourner un SimBroker."""
    broker = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="json",
    )
    assert isinstance(broker, SimBroker)
    # Crée bien broker.json
    assert (tmp_path / "broker.json").exists()
    assert broker.cash() == pytest.approx(100_000.0)


def test_make_broker_json_reads_existing_state(tmp_path: Path) -> None:
    """make_broker(backend="json") lit un broker.json existant (cash migré)."""
    _broker_json_fixture(tmp_path, cash=42_000.0)
    broker = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="json",
    )
    assert isinstance(broker, SimBroker)
    assert broker.cash() == pytest.approx(42_000.0)


# ---------------------------------------------------------------------------
# Test 2 — backend="sqlite" avec broker.json existant → SqliteBroker migré
# ---------------------------------------------------------------------------


def test_make_broker_sqlite_migrates_json(tmp_path: Path) -> None:
    """backend='sqlite' avec broker.json existant → SqliteBroker + état migré."""
    _broker_json_fixture(tmp_path, cash=42_000.0)

    broker = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    assert isinstance(broker, SqliteBroker)

    # casys.db créé
    assert (tmp_path / "casys.db").exists()

    # cash reflète le JSON migré (pas starting_cash)
    assert broker.cash() == pytest.approx(42_000.0)

    # positions reflètent le JSON migré
    positions = broker.positions()
    assert "AAPL" in positions
    assert positions["AAPL"].quantity == pytest.approx(10.0)
    assert positions["AAPL"].avg_price == pytest.approx(170.0)


# ---------------------------------------------------------------------------
# Test 3 — backend="sqlite" sans broker.json → broker neuf à starting_cash
# ---------------------------------------------------------------------------


def test_make_broker_sqlite_fresh_no_json(tmp_path: Path) -> None:
    """backend='sqlite' sans broker.json → cash == starting_cash."""
    broker = make_broker(
        state_dir=tmp_path,
        starting_cash=50_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    assert isinstance(broker, SqliteBroker)
    assert broker.cash() == pytest.approx(50_000.0)
    assert broker.positions() == {}


# ---------------------------------------------------------------------------
# Test 4 — backend inconnu → ValueError
# ---------------------------------------------------------------------------


def test_make_broker_unknown_backend_raises(tmp_path: Path) -> None:
    """backend inconnu → ValueError explicite."""
    with pytest.raises(ValueError, match="bogus"):
        make_broker(
            state_dir=tmp_path,
            starting_cash=100_000.0,
            commission_model=_NO_COMMISSION,
            backend="bogus",
        )


# ---------------------------------------------------------------------------
# Test 5 — non-régression : défaut "json" si env absent
# ---------------------------------------------------------------------------


def test_make_broker_default_is_json_when_env_absent(tmp_path: Path, monkeypatch) -> None:
    """Sans CASYS_STATE_BACKEND dans l'env, le défaut 'json' est utilisé → SimBroker."""
    monkeypatch.delenv("CASYS_STATE_BACKEND", raising=False)

    backend = os.getenv("CASYS_STATE_BACKEND", "json")
    broker = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend=backend,
    )
    assert isinstance(broker, SimBroker)


# ---------------------------------------------------------------------------
# Test 6 — idempotence : double appel sqlite → no-op (pas de doublon)
# ---------------------------------------------------------------------------


def test_make_broker_sqlite_idempotent(tmp_path: Path) -> None:
    """Appeler make_broker(sqlite) deux fois de suite → pas d'erreur, même cash."""
    _broker_json_fixture(tmp_path, cash=30_000.0)

    b1 = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    b2 = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    assert b1.cash() == pytest.approx(30_000.0)
    assert b2.cash() == pytest.approx(30_000.0)


# ---------------------------------------------------------------------------
# Test 7 — make_broker(sqlite) régénère le shadow au boot (FIX 3)
# ---------------------------------------------------------------------------


def test_make_broker_sqlite_regenerates_shadow_at_boot(tmp_path: Path) -> None:
    """make_broker(sqlite) régénère broker.json depuis SQLite au boot.

    Scénario : DB SQLite déjà peuplée (1er appel make_broker), broker.json supprimé
    (simule crash entre COMMIT et shadow write), 2e appel make_broker → broker.json
    recréé et reflète l'état des tables.
    """
    _broker_json_fixture(tmp_path, cash=42_000.0)

    # 1er boot : migre + crée broker.json
    b1 = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    json_path = tmp_path / "broker.json"
    assert json_path.exists()

    # Simule crash : supprime le shadow
    json_path.unlink()

    # 2e boot : shadow absent → regenerate_shadow doit le recréer
    b2 = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    assert json_path.exists(), "make_broker(sqlite) doit régénérer broker.json au boot"

    shadow = json.loads(json_path.read_text())
    assert shadow["cash"] == pytest.approx(b2.cash())
    assert shadow["cash"] == pytest.approx(42_000.0)


def test_make_broker_sqlite_regenerates_stale_shadow(tmp_path: Path) -> None:
    """make_broker(sqlite) écrase un broker.json stale avec l'état réel de la DB."""
    _broker_json_fixture(tmp_path, cash=42_000.0)

    # 1er boot : migre
    make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )

    # Injecter un shadow stale
    json_path = tmp_path / "broker.json"
    json_path.write_text(json.dumps({"cash": 0.0, "positions": {}, "fills": []}))

    # 2e boot : regenerate_shadow corrige
    b2 = make_broker(
        state_dir=tmp_path,
        starting_cash=100_000.0,
        commission_model=_NO_COMMISSION,
        backend="sqlite",
    )
    shadow = json.loads(json_path.read_text())
    assert shadow["cash"] == pytest.approx(b2.cash())
    assert shadow["cash"] == pytest.approx(42_000.0)
