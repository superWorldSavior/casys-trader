"""Tests TDD — SqliteBroker (broker_store.py).

Couvre :
  - Parité SimBroker ↔ SqliteBroker sur une séquence complète d'ordres
  - dry_run (pas de mutation)
  - Rollback transactionnel (exception mid-submit → état inchangé)
  - Shadow JSON (broker.json = miroir des tables après submit)
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from trader.state_db.broker_store import SqliteBroker
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_broker_from_json
from trader.execution.broker import IbkrCommissionModel, Order, SimBroker


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_sim_broker(tmp_path: Path, starting_cash: float = 100_000.0) -> SimBroker:
    """Crée un SimBroker neuf (JSON backend)."""
    return SimBroker(tmp_path / "broker_sim.json", starting_cash=starting_cash)


def _make_sqlite_broker(
    tmp_path: Path,
    starting_cash: float = 100_000.0,
    commission_model=None,
) -> tuple[StateDb, SqliteBroker]:
    """Crée un SqliteBroker neuf via import_broker_from_json (pas de JSON source)."""
    db = StateDb(tmp_path / "casys.db")
    # Broker neuf : import d'un JSON absent → insert starting_cash
    import_broker_from_json(db, tmp_path / "_absent_.json", starting_cash=starting_cash)
    broker = SqliteBroker(db, commission_model=commission_model)
    return db, broker


def _positions_approx(positions: dict) -> dict:
    """Convertit les positions en dict serialisable pour comparaison approchée."""
    return {sym: {"quantity": pos.quantity, "avg_price": pos.avg_price} for sym, pos in positions.items()}


def _assert_brokers_equal(sim: SimBroker, sqlite: SqliteBroker) -> None:
    """Vérifie que cash() et positions() sont identiques (± epsilon float)."""
    assert sqlite.cash() == pytest.approx(sim.cash(), rel=1e-9), (
        f"cash mismatch: sqlite={sqlite.cash()} sim={sim.cash()}"
    )
    sim_pos = _positions_approx(sim.positions())
    sqlite_pos = _positions_approx(sqlite.positions())
    assert set(sim_pos.keys()) == set(sqlite_pos.keys()), (
        f"symbols mismatch: sqlite={set(sqlite_pos)} sim={set(sim_pos)}"
    )
    for sym in sim_pos:
        assert sqlite_pos[sym]["quantity"] == pytest.approx(sim_pos[sym]["quantity"], rel=1e-9)
        assert sqlite_pos[sym]["avg_price"] == pytest.approx(sim_pos[sym]["avg_price"], rel=1e-9)


# Séquence de référence : open long, add, reduce, reverse, add short, close
_SEQUENCE: list[tuple[Order, float, str, float]] = [
    (Order("AAPL", "BUY", 10.0), 150.0, "t1", 1.0),  # open long 10 @ 150
    (Order("AAPL", "BUY", 5.0), 155.0, "t2", 1.0),  # add 5 → 15 long
    (Order("AAPL", "SELL", 3.0), 158.0, "t3", 1.0),  # reduce → 12 long
    (Order("AAPL", "SELL", 15.0), 160.0, "t4", 1.0),  # reverse: -3 short
    (Order("AAPL", "SELL", 2.0), 162.0, "t5", 1.0),  # add short → -5
    (Order("AAPL", "BUY", 5.0), 155.0, "t6", 1.0),  # close short → 0
]


# ---------------------------------------------------------------------------
# Classe 1 — Parité SimBroker ↔ SqliteBroker
# ---------------------------------------------------------------------------


class TestParite:
    def test_initial_state_identical(self, tmp_path: Path) -> None:
        """Cash identique à l'état initial (aucun ordre)."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        _assert_brokers_equal(sim, sqlite)

    def test_parity_after_each_step(self, tmp_path: Path) -> None:
        """Après chaque submit, cash() et positions() identiques."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        for order, price, ts, fx_rate in _SEQUENCE:
            sim.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)
            sqlite.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)
            _assert_brokers_equal(sim, sqlite)

    def test_fills_getter_returns_broker_json_shape_from_sqlite(self, tmp_path: Path) -> None:
        """fills() retourne les fills au même format que broker.json."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t1", dry_run=False)
        sqlite.submit(Order("AAPL", "SELL", 4.0), 155.0, "t2", dry_run=False)

        assert sqlite.fills() == [
            {
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 10.0,
                "price": 150.0,
                "ts": "t1",
                "commission": 0.0,
                "commission_currency": "USD",
                "commission_model": "none",
                "fx_rate": 1.0,
            },
            {
                "symbol": "AAPL",
                "side": "SELL",
                "quantity": 4.0,
                "price": 155.0,
                "ts": "t2",
                "commission": 0.0,
                "commission_currency": "USD",
                "commission_model": "none",
                "fx_rate": 1.0,
            },
        ]

    def test_causal_links_flow_from_order_to_fill_and_sqlite_row(self, tmp_path: Path) -> None:
        db, sqlite = _make_sqlite_broker(tmp_path / "sq")
        order = Order(
            "AAPL",
            "BUY",
            10.0,
            process_instance_id="instance-1",
            attempt_id="attempt-1",
            decision_id="decision-1",
        )

        fill = sqlite.submit(order, 150.0, "t1", dry_run=False)

        assert fill is not None
        assert fill.model_dump()["process_instance_id"] == "instance-1"
        assert sqlite.fills()[0]["attempt_id"] == "attempt-1"
        row = db.query_one("SELECT * FROM broker_fills WHERE symbol='AAPL'")
        assert row is not None
        assert row["decision_id"] == "decision-1"

    def test_position_zero_not_in_positions_api(self, tmp_path: Path) -> None:
        """positions() filtre les q==0 (même comportement que SimBroker)."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        # Ouvrir et fermer → q=0
        for order, price, ts, fx_rate in _SEQUENCE:
            sim.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)
            sqlite.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)

        # Après la séquence, AAPL est à q=0 → absent de positions()
        assert "AAPL" not in sim.positions()
        assert "AAPL" not in sqlite.positions()

    def test_position_poussiere_pas_dans_positions_api(self, tmp_path: Path) -> None:
        """positions() filtre aussi les résidus float déjà présents en table."""
        db, sqlite = _make_sqlite_broker(tmp_path)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO broker_positions(symbol, quantity, avg_price) VALUES (?,?,?)",
                ("2330.TW", 4.440892098500626e-16, 2425.0),
            )

        assert "2330.TW" not in sqlite.positions()

    def test_position_poussiere_fractionnee_cloture_comme_zero(self, tmp_path: Path) -> None:
        """Même cas live 2330.TW : 20 - 10 - 6.66666667 - 3.33333333."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        orders = [
            (Order("2330.TW", "BUY", 20.0), 2425.0, "t1", 0.031),
            (Order("2330.TW", "SELL", 10.0), 2455.0, "t2", 0.031),
            (Order("2330.TW", "SELL", 6.66666667), 2450.0, "t3", 0.031),
            (Order("2330.TW", "SELL", 3.33333333), 2480.0, "t4", 0.031),
        ]

        for order, price, ts, fx_rate in orders:
            sim.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)
            sqlite.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)

        assert "2330.TW" not in sim.positions()
        assert "2330.TW" not in sqlite.positions()

    def test_position_poussiere_d_une_cloture_arrondie_comme_zero(self, tmp_path: Path) -> None:
        """Une clôture à 8 décimales ne laisse pas une ligne à $0 dans le TUI."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        orders = [
            (Order("IBM", "SELL", 32.06701335376155), 296.73, "t1", 1.0),
            (Order("IBM", "BUY", 32.06701335), 298.17, "t2", 1.0),
        ]

        for order, price, ts, fx_rate in orders:
            sim.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)
            sqlite.submit(order, price, ts, dry_run=False, fx_rate=fx_rate)

        assert "IBM" not in sim.positions()
        assert "IBM" not in sqlite.positions()

    def test_parity_with_fx_rate(self, tmp_path: Path) -> None:
        """Parité sur un symbole TWD (fx_rate != 1.0)."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        order = Order("2379.TW", "BUY", 10.0)
        sim.submit(order, 870.0, "t", dry_run=False, fx_rate=0.031)
        sqlite.submit(order, 870.0, "t", dry_run=False, fx_rate=0.031)
        _assert_brokers_equal(sim, sqlite)

    def test_parity_with_commission_model(self, tmp_path: Path) -> None:
        """Parité avec IbkrCommissionModel (frais déduits du cash)."""
        model = IbkrCommissionModel()
        sim = SimBroker(
            tmp_path / "sim" / "broker.json",
            starting_cash=100_000.0,
            commission_model=model,
        )
        _, sqlite = _make_sqlite_broker(tmp_path / "sq", commission_model=model)

        order = Order("USO", "SELL", 35.0)
        sim.submit(order, 127.70, "t1", dry_run=False)
        sqlite.submit(order, 127.70, "t1", dry_run=False)
        _assert_brokers_equal(sim, sqlite)

    def test_avg_price_reversal(self, tmp_path: Path) -> None:
        """Retournement de position : avg_price = prix d'exécution."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        sqlite.submit(Order("SPY", "BUY", 10.0), 100.0, "t1", dry_run=False)
        # Retournement : vend 15 alors qu'on a 10 long → -5 short
        sqlite.submit(Order("SPY", "SELL", 15.0), 110.0, "t2", dry_run=False)

        pos = sqlite.positions()["SPY"]
        assert pos.quantity == pytest.approx(-5.0)
        assert pos.avg_price == pytest.approx(110.0)

    def test_avg_price_weighted_average(self, tmp_path: Path) -> None:
        """Ajout en même sens : moyenne pondérée."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        sqlite.submit(Order("SPY", "BUY", 10.0), 100.0, "t1", dry_run=False)
        sqlite.submit(Order("SPY", "BUY", 10.0), 120.0, "t2", dry_run=False)

        pos = sqlite.positions()["SPY"]
        assert pos.quantity == pytest.approx(20.0)
        assert pos.avg_price == pytest.approx(110.0)


# ---------------------------------------------------------------------------
# Classe 2 — dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_returns_none(self, tmp_path: Path) -> None:
        """dry_run=True retourne None."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        result = sqlite.submit(Order("AAPL", "BUY", 1.0), 150.0, "t", dry_run=True)
        assert result is None

    def test_dry_run_does_not_mutate_cash(self, tmp_path: Path) -> None:
        """dry_run=True ne mute pas le cash."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        cash_before = sqlite.cash()
        sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t", dry_run=True)
        assert sqlite.cash() == pytest.approx(cash_before)

    def test_dry_run_does_not_mutate_positions(self, tmp_path: Path) -> None:
        """dry_run=True ne mute pas les positions."""
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")
        sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t", dry_run=True)
        assert sqlite.positions() == {}

    def test_dry_run_does_not_create_fills(self, tmp_path: Path) -> None:
        """dry_run=True n'écrit pas de fill dans broker_fills."""
        db, sqlite = _make_sqlite_broker(tmp_path / "sq")
        sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t", dry_run=True)
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 0


# ---------------------------------------------------------------------------
# Classe 3 — Rollback transactionnel
# ---------------------------------------------------------------------------


class _CountingCursor:
    """Wrapper autour d'un sqlite3.Cursor.

    Délègue tous les appels au curseur réel mais lève RuntimeError après
    `fail_after` appels à execute() (pour tester le rollback transactionnel).
    sqlite3.Cursor.execute est read-only en Python 3.13 → pas de monkeypatch direct.
    """

    def __init__(self, real_cur, fail_after: int, err_msg: str = "injected failure"):
        self._cur = real_cur
        self._fail_after = fail_after
        self._err_msg = err_msg
        self._count = 0

    def execute(self, sql, params=()):
        result = self._cur.execute(sql, params)
        self._count += 1
        if self._count >= self._fail_after:
            raise RuntimeError(self._err_msg)
        return result

    def __getattr__(self, name):
        return getattr(self._cur, name)


class TestTransactionRollback:
    def test_exception_mid_transaction_rolls_back(self, tmp_path: Path, monkeypatch) -> None:
        """Exception après le 1er execute dans la transaction → rollback complet.

        Stratégie : on wrape StateDb.transaction() avec un générateur qui yield
        un _CountingCursor. sqlite3.Cursor.execute est read-only (Python 3.13),
        donc le wrapper est la seule approche portable.
        """
        db, sqlite = _make_sqlite_broker(tmp_path / "sq")

        cash_before = sqlite.cash()

        original_transaction = db.transaction

        @contextmanager
        def failing_transaction():
            with original_transaction() as cur:
                wrapper = _CountingCursor(cur, fail_after=1, err_msg="simulated mid-transaction failure")
                yield wrapper

        monkeypatch.setattr(db, "transaction", failing_transaction)

        with pytest.raises(RuntimeError, match="simulated mid-transaction failure"):
            sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t", dry_run=False)

        # Cash et positions doivent être inchangés (rollback garanti par original_transaction)
        assert sqlite.cash() == pytest.approx(cash_before)
        assert sqlite.positions() == {}

        # Aucun fill enregistré
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 0

    def test_exception_on_second_write_rolls_back(self, tmp_path: Path, monkeypatch) -> None:
        """Exception sur l'UPDATE cash (2e execute) → rollback complet."""
        db, sqlite = _make_sqlite_broker(tmp_path / "sq")

        cash_before = sqlite.cash()

        original_transaction = db.transaction

        @contextmanager
        def failing_on_update():
            with original_transaction() as cur:
                wrapper = _CountingCursor(cur, fail_after=2, err_msg="update failure")
                yield wrapper

        monkeypatch.setattr(db, "transaction", failing_on_update)

        with pytest.raises(RuntimeError, match="update failure"):
            sqlite.submit(Order("AAPL", "BUY", 10.0), 150.0, "t", dry_run=False)

        assert sqlite.cash() == pytest.approx(cash_before)
        assert sqlite.positions() == {}


# ---------------------------------------------------------------------------
# Classe 4 — Shadow supprimé
# ---------------------------------------------------------------------------


class TestNoShadow:
    def test_constructor_rejects_json_path(self, tmp_path: Path) -> None:
        db, _ = _make_sqlite_broker(tmp_path)

        with pytest.raises(TypeError):
            SqliteBroker(db, json_path=tmp_path / "broker.json")  # type: ignore[call-arg]

    def test_regenerate_shadow_api_removed(self) -> None:
        assert not hasattr(SqliteBroker, "regenerate_shadow")

    def test_no_shadow_written_after_submit(self, tmp_path: Path) -> None:
        db, broker = _make_sqlite_broker(tmp_path)

        broker.submit(Order("AAPL", "BUY", 5.0), 100.0, "t1", dry_run=False)
        broker.submit(Order("AAPL", "SELL", 5.0), 105.0, "t2", dry_run=False)

        assert db.query_all("SELECT * FROM broker_fills ORDER BY seq")
        assert not (tmp_path / "broker.json").exists()


# ---------------------------------------------------------------------------
# Classe 6 — Parité supplémentaire (FIX 5)
# ---------------------------------------------------------------------------


class TestPariteSupplementaire:
    """Cas de parité SimBroker ↔ SqliteBroker non couverts dans TestParite."""

    def test_short_to_long_reversal(self, tmp_path: Path) -> None:
        """Retournement short → long : parité exacte avec SimBroker."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        # Ouvrir short de -5
        sim.submit(Order("SPY", "SELL", 5.0), 100.0, "t1", dry_run=False)
        sqlite.submit(Order("SPY", "SELL", 5.0), 100.0, "t1", dry_run=False)
        _assert_brokers_equal(sim, sqlite)

        # Retournement : achète 10 → net +5 long
        sim.submit(Order("SPY", "BUY", 10.0), 105.0, "t2", dry_run=False)
        sqlite.submit(Order("SPY", "BUY", 10.0), 105.0, "t2", dry_run=False)
        _assert_brokers_equal(sim, sqlite)

        pos = sqlite.positions()["SPY"]
        assert pos.quantity == pytest.approx(5.0)
        assert pos.avg_price == pytest.approx(105.0)  # avg = prix du retournement

    def test_partial_reduction_of_short(self, tmp_path: Path) -> None:
        """Réduction partielle d'un short : parité exacte avec SimBroker."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        # Ouvrir short de -8
        sim.submit(Order("SPY", "SELL", 8.0), 100.0, "t1", dry_run=False)
        sqlite.submit(Order("SPY", "SELL", 8.0), 100.0, "t1", dry_run=False)

        # Réduire de 3 → -5 short
        sim.submit(Order("SPY", "BUY", 3.0), 98.0, "t2", dry_run=False)
        sqlite.submit(Order("SPY", "BUY", 3.0), 98.0, "t2", dry_run=False)
        _assert_brokers_equal(sim, sqlite)

        pos = sqlite.positions()["SPY"]
        assert pos.quantity == pytest.approx(-5.0)
        assert pos.avg_price == pytest.approx(100.0)  # avg short inchangé

    def test_exact_close_long(self, tmp_path: Path) -> None:
        """Fermeture exacte d'un long : q==0 absent de positions(), parité SimBroker."""
        sim = _make_sim_broker(tmp_path / "sim")
        _, sqlite = _make_sqlite_broker(tmp_path / "sq")

        sim.submit(Order("TSLA", "BUY", 7.0), 250.0, "t1", dry_run=False)
        sqlite.submit(Order("TSLA", "BUY", 7.0), 250.0, "t1", dry_run=False)

        sim.submit(Order("TSLA", "SELL", 7.0), 260.0, "t2", dry_run=False)
        sqlite.submit(Order("TSLA", "SELL", 7.0), 260.0, "t2", dry_run=False)

        _assert_brokers_equal(sim, sqlite)
        assert "TSLA" not in sqlite.positions()
        assert "TSLA" not in sim.positions()

    def test_commission_ibkr_non_usd_symbol(self, tmp_path: Path) -> None:
        """Commission IBKR sur symbole TWD (fx_rate << 1) : parité SimBroker."""
        model = IbkrCommissionModel()
        sim = SimBroker(
            tmp_path / "sim" / "broker.json",
            starting_cash=100_000.0,
            commission_model=model,
        )
        _, sqlite = _make_sqlite_broker(tmp_path / "sq", commission_model=model)

        # 2379.TW : prix en TWD, fx_rate = 0.031 (TWD→USD)
        order = Order("2379.TW", "BUY", 100.0)
        sim.submit(order, 870.0, "t1", dry_run=False, fx_rate=0.031)
        sqlite.submit(order, 870.0, "t1", dry_run=False, fx_rate=0.031)
        _assert_brokers_equal(sim, sqlite)

    def test_commission_ibkr_eu_symbol(self, tmp_path: Path) -> None:
        """Commission IBKR sur symbole EUR (^FCHI proxy) : parité SimBroker."""
        model = IbkrCommissionModel()
        sim = SimBroker(
            tmp_path / "sim" / "broker.json",
            starting_cash=100_000.0,
            commission_model=model,
        )
        _, sqlite = _make_sqlite_broker(tmp_path / "sq", commission_model=model)

        # Symbole EU, prix en EUR, fx_rate ~1.08 (EUR→USD)
        order = Order("AIR.PA", "BUY", 5.0)
        sim.submit(order, 170.0, "t1", dry_run=False, fx_rate=1.08)
        sqlite.submit(order, 170.0, "t1", dry_run=False, fx_rate=1.08)
        _assert_brokers_equal(sim, sqlite)
