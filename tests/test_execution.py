from trader.tools.execution import Order, SimBroker


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
