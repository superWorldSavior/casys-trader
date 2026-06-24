import pytest

from trader.tools.portfolio import Holding, Snapshot


def test_as_context_ajoute_le_pnl_latent_net_quand_un_estimateur_frais_est_fourni() -> None:
    snap = Snapshot(
        cash=1_000.0,
        holdings=[Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0)],
        starting_equity=1_000.0,
    )

    context = snap.as_context(
        fee_estimator=lambda symbol, quantity, avg_price, last_price: 3.456
    )

    holding = context["holdings"][0]
    assert holding["unrealized_pnl"] == 100.0
    assert holding["round_trip_fee"] == 3.456
    assert holding["unrealized_pnl_net"] == 96.54


def test_as_context_sans_estimateur_garde_le_contrat_historique() -> None:
    snap = Snapshot(
        cash=1_000.0,
        holdings=[Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0)],
        starting_equity=1_000.0,
    )

    holding = snap.as_context()["holdings"][0]

    assert holding["unrealized_pnl"] == 100.0
    assert "round_trip_fee" not in holding
    assert "unrealized_pnl_net" not in holding


def test_as_context_ignore_un_holding_quand_l_estimateur_renvoie_none() -> None:
    snap = Snapshot(
        cash=1_000.0,
        holdings=[
            Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0),
            Holding("BTC-USD", quantity=1.0, avg_price=50_000.0, last_price=51_000.0),
        ],
        starting_equity=1_000.0,
    )

    def estimate(symbol: str, quantity: float, avg_price: float, last_price: float) -> float | None:
        return None if symbol == "BTC-USD" else 2.0

    holdings = snap.as_context(fee_estimator=estimate)["holdings"]

    assert holdings[0]["round_trip_fee"] == 2.0
    assert holdings[0]["unrealized_pnl_net"] == 98.0
    assert "round_trip_fee" not in holdings[1]
    assert "unrealized_pnl_net" not in holdings[1]


def test_holding_twd_market_value_et_unrealized_pnl_en_usd() -> None:
    """Holding TWD valorisé en USD via fx_rate."""
    h = Holding("2379.TW", quantity=10.0, avg_price=800.0, last_price=870.0, fx_rate=0.031)
    assert h.market_value == pytest.approx(870.0 * 10 * 0.031, rel=1e-9)
    assert h.unrealized_pnl == pytest.approx((870.0 - 800.0) * 10 * 0.031, rel=1e-9)


def test_holding_usd_defaut_fx_rate_inchange() -> None:
    """fx_rate=1.0 par défaut : holding USD non altéré."""
    h = Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0)
    assert h.fx_rate == 1.0
    assert h.market_value == pytest.approx(1100.0, rel=1e-9)
    assert h.unrealized_pnl == pytest.approx(100.0, rel=1e-9)
