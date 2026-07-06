import pytest

from trader.execution.portfolio import Holding, Snapshot


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


def test_as_context_inclut_fx_rate_dans_chaque_holding() -> None:
    """as_context sérialise fx_rate pour que la TUI puisse calculer le notional USD."""
    snap = Snapshot(
        cash=0.0,
        holdings=[
            Holding("2379.TW", quantity=100.0, avg_price=800.0, last_price=870.0, fx_rate=0.031),
            Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0, fx_rate=1.0),
        ],
        starting_equity=1_000.0,
    )

    holdings = snap.as_context()["holdings"]

    assert holdings[0]["fx_rate"] == pytest.approx(0.031, rel=1e-9)
    assert holdings[1]["fx_rate"] == pytest.approx(1.0, rel=1e-9)


def test_as_context_fx_rate_usd_par_defaut() -> None:
    """Un Holding sans fx_rate explicite expose fx_rate=1.0 dans le contexte."""
    snap = Snapshot(
        cash=0.0,
        holdings=[Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=110.0)],
        starting_equity=1_000.0,
    )

    holding = snap.as_context()["holdings"][0]

    assert holding["fx_rate"] == pytest.approx(1.0, rel=1e-9)


def test_as_context_expose_cash_disponible_net_des_shorts() -> None:
    """Le cash broker garde le produit du short, mais le contexte expose aussi
    un cash disponible net de l'obligation de rachat."""
    snap = Snapshot(
        cash=115_000.0,
        holdings=[
            Holding("AAPL", quantity=10.0, avg_price=100.0, last_price=100.0),
            Holding("SPY", quantity=-50.0, avg_price=300.0, last_price=280.0),
        ],
        starting_equity=100_000.0,
    )

    context = snap.as_context()

    assert context["cash"] == 115_000.0
    assert context["cash_ledger"] == 115_000.0
    assert context["cash_available"] == 101_000.0
    assert context["short_exposure_usd"] == 14_000.0
    assert context["long_exposure_usd"] == 1_000.0
    assert context["gross_exposure_usd"] == 15_000.0
    assert context["net_exposure_usd"] == -13_000.0


class _Pos:
    def __init__(self, symbol: str, quantity: float, avg_price: float) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.avg_price = avg_price


class _Broker:
    def __init__(self, cash: float, positions: dict) -> None:
        self._cash = cash
        self._positions = positions

    def positions(self) -> dict:
        return self._positions

    def cash(self) -> float:
        return self._cash


def test_snapshot_valorise_au_avg_price_quand_le_prix_est_invalide() -> None:
    """Backstop falaise : une position détenue sans prix valide (price_of renvoie
    0.0 — le défaut `prices.get(s, 0.0)` côté daemon) ne doit JAMAIS être
    valorisée à $0. On garde le coût (avg_price), unrealized=0, pas de fausse
    falaise d'équité (cf. STMN.SW 30/06)."""
    from trader.execution import portfolio

    broker = _Broker(cash=25_447.0, positions={"STMN.SW": _Pos("STMN.SW", 60.0, 104.6)})

    snap = portfolio.snapshot(broker, price_of=lambda s: 0.0, starting_equity=100_000.0)

    h = snap.holdings[0]
    assert h.last_price == pytest.approx(104.6)  # avg_price, PAS 0
    assert h.market_value == pytest.approx(60.0 * 104.6)
    assert snap.equity == pytest.approx(25_447.0 + 60.0 * 104.6)


def test_snapshot_garde_le_prix_quand_il_est_valide() -> None:
    """Non-régression : un prix valide est utilisé tel quel."""
    from trader.execution import portfolio

    broker = _Broker(cash=1_000.0, positions={"AAPL": _Pos("AAPL", 10.0, 100.0)})

    snap = portfolio.snapshot(broker, price_of=lambda s: 110.0, starting_equity=1_000.0)

    assert snap.holdings[0].last_price == pytest.approx(110.0)
