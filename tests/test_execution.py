import json

import pytest

from trader.tools.execution import IbkrCommissionModel, Order, SimBroker


def test_sim_broker_enregistre_avg_price_sur_ouverture_short(tmp_path) -> None:
    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000)

    broker.submit(Order("SPY", "SELL", 10.0), 100.0, "2026-06-05T12:00:00+00:00", dry_run=False)

    pos = broker.positions()["SPY"]
    assert pos.quantity == -10.0
    assert pos.avg_price == 100.0


def test_sim_broker_cloture_un_long_sans_garder_position_zero(tmp_path) -> None:
    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000)

    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "t1", dry_run=False)
    broker.submit(Order("SPY", "SELL", 10.0), 101.0, "t2", dry_run=False)

    assert broker.positions() == {}


def test_sim_broker_deduit_la_commission_ibkr_us_etf(tmp_path) -> None:
    broker = SimBroker(
        tmp_path / "broker.json",
        starting_cash=10_000.0,
        commission_model=IbkrCommissionModel(),
    )

    fill = broker.submit(Order("USO", "SELL", 35.0), 127.70, "t1", dry_run=False)

    assert fill is not None
    assert fill.commission == pytest.approx(0.35)
    assert fill.commission_currency == "USD"
    assert fill.commission_model == "ibkr_us_stock_tiered"
    assert broker.cash() == pytest.approx(10_000.0 + 35.0 * 127.70 - 0.35)

    persisted = json.loads((tmp_path / "broker.json").read_text())
    assert persisted["fills"][0]["commission"] == pytest.approx(0.35)
    assert persisted["fills"][0]["commission_model"] == "ibkr_us_stock_tiered"


def test_sim_broker_applique_le_minimum_fx_ibkr(tmp_path) -> None:
    broker = SimBroker(
        tmp_path / "broker.json",
        starting_cash=20_000.0,
        commission_model=IbkrCommissionModel(),
    )

    fill = broker.submit(Order("EURUSD=X", "BUY", 8_000.0), 1.157, "t1", dry_run=False)

    assert fill is not None
    assert fill.commission == pytest.approx(2.0)
    assert fill.commission_model == "ibkr_spot_fx_tiered"
    assert broker.cash() == pytest.approx(20_000.0 - 8_000.0 * 1.157 - 2.0)


def test_sim_broker_approxime_fchi_comme_cfd_indice_ibkr(tmp_path) -> None:
    broker = SimBroker(
        tmp_path / "broker.json",
        starting_cash=20_000.0,
        commission_model=IbkrCommissionModel(),
    )

    fill = broker.submit(Order("^FCHI", "SELL", 1.0), 8372.28, "t1", dry_run=False)

    assert fill is not None
    assert fill.commission == pytest.approx(1.0)
    assert fill.commission_currency == "EUR"
    assert fill.commission_model == "ibkr_france40_cfd"
    assert broker.cash() == pytest.approx(20_000.0 + 8372.28 - 1.0)
