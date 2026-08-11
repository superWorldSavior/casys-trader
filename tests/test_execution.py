import json

import pytest

from trader.execution.broker import IbkrCommissionModel, Order, SimBroker


def test_sim_broker_persists_causal_links_without_null_keys_for_legacy_fills(tmp_path) -> None:
    state = tmp_path / "broker.json"
    broker = SimBroker(state, starting_cash=100_000.0)

    legacy_fill = broker.submit(Order("MSFT", "BUY", 1.0), 100.0, "t1", dry_run=False)
    linked_fill = broker.submit(
        Order(
            "AAPL",
            "BUY",
            1.0,
            process_instance_id="instance-1",
            attempt_id="attempt-1",
            decision_id="decision-1",
        ),
        100.0,
        "t2",
        dry_run=False,
    )

    assert legacy_fill is not None
    assert "process_instance_id" not in legacy_fill.model_dump()
    assert linked_fill is not None
    assert linked_fill.decision_id == "decision-1"
    fills = json.loads(state.read_text())["fills"]
    assert "attempt_id" not in fills[0]
    assert fills[1]["attempt_id"] == "attempt-1"


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


def test_sim_broker_elimine_la_poussiere_flottante_sur_cloture_fractionnee(tmp_path) -> None:
    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000)

    broker.submit(Order("2330.TW", "BUY", 20.0), 2425.0, "t1", dry_run=False, fx_rate=0.031)
    broker.submit(Order("2330.TW", "SELL", 10.0), 2455.0, "t2", dry_run=False, fx_rate=0.031)
    broker.submit(Order("2330.TW", "SELL", 6.66666667), 2450.0, "t3", dry_run=False, fx_rate=0.031)
    broker.submit(Order("2330.TW", "SELL", 3.33333333), 2480.0, "t4", dry_run=False, fx_rate=0.031)

    assert broker.positions() == {}
    persisted = json.loads((tmp_path / "broker.json").read_text())
    assert persisted["positions"]["2330.TW"]["quantity"] == 0.0
    assert persisted["positions"]["2330.TW"]["avg_price"] == 0.0


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

    # ^FCHI est maintenant correctement reconnu EUR (SYMBOL_CCY).
    # trade_value_eur = 1 * 8372.28 ; commission_eur = 1.0
    # cash_delta_usd = 8372.28 * eur_rate ; fee_usd = 1.0 * eur_rate
    eur_rate = 1.10  # EUR/USD explicite
    fill = broker.submit(Order("^FCHI", "SELL", 1.0), 8372.28, "t1", dry_run=False, fx_rate=eur_rate)

    assert fill is not None
    assert fill.commission == pytest.approx(1.0)
    assert fill.commission_currency == "EUR"
    assert fill.commission_model == "ibkr_france40_cfd"
    # SELL : cash += trade_value_usd − commission_usd
    expected_cash = 20_000.0 + 8372.28 * eur_rate - 1.0 * eur_rate
    assert broker.cash() == pytest.approx(expected_cash)


def test_cash_deducted_in_usd_for_twd_symbol(tmp_path):
    state = tmp_path / "broker.json"
    broker = SimBroker(state_path=state, starting_cash=100_000.0)
    # 10 actions @ 870 TWD, rate 0.031 -> 8700 * 0.031 = 269.7 USD
    broker.submit(Order("2379.TW", "BUY", 10.0), price=870.0, ts="t", dry_run=False, fx_rate=0.031)
    assert broker.cash() == pytest.approx(100_000.0 - 8700.0 * 0.031)
    fill = broker._state.fills[-1]
    assert fill["fx_rate"] == 0.031


def test_cash_unchanged_for_usd_symbol(tmp_path):
    state = tmp_path / "broker.json"
    broker = SimBroker(state_path=state, starting_cash=100_000.0)
    broker.submit(Order("MSFT", "BUY", 2.0), price=100.0, ts="t", dry_run=False, fx_rate=1.0)
    assert broker.cash() == pytest.approx(100_000.0 - 200.0)
