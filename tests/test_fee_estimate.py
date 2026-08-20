"""TDD pour round_trip_cost — estimation déterministe du coût aller-retour.

Le helper traduit un modèle de commission (fonction pure de symbol/qty/price) en
deux signaux décisionnels injectables dans le cockpit : le coût aller-retour en
devise (`fee_rt`) et le seuil de rentabilité en points de base (`be_bps`).
"""

from __future__ import annotations

import pytest

from trader.domain.contracts import Commission
from trader.execution.broker import (
    IbkrCommissionModel,
    NoCommissionModel,
    round_trip_cost,
)


def test_round_trip_cost_index_cfd_convertit_les_frais_eur_en_bps_usd() -> None:
    # ^FCHI : plafond 10k USD = 9_090.91 EUR à 1.10 USD/EUR. Le minimum
    # de 1 EUR mord par jambe : 2 EUR = 2.20 USD = 2.20 bps du plafond USD.
    cost = round_trip_cost(
        IbkrCommissionModel(),
        "^FCHI",
        price=8000.0,
        ref_notional=10_000.0,
        fx_rate=1.10,
    )
    assert cost is not None
    assert cost["currency"] == "EUR"
    assert cost["fee_rt"] == 2.0
    assert cost["be_bps"] == 2.2


def test_round_trip_cost_us_stock_minimum_par_ordre() -> None:
    # AAPL à 200$ sur 10k → 50 actions. 50*0.0035=0.175 < min 0.35 → 0.35/jambe.
    # Aller-retour = 0.70 USD, be = 0.70/10000*1e4 = 0.70 bps.
    cost = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    assert cost is not None
    assert cost["currency"] == "USD"
    assert cost["fee_rt"] == 0.70
    assert cost["be_bps"] == 0.70


def test_round_trip_cost_us_stock_non_divisor_does_not_fractionalize_full_notional() -> None:
    # The cockpit derives 131.319 shares from its reference notional.  A mixed
    # quantity must split whole/fractional components instead of applying 1%
    # to the complete USD 10k reference amount.
    quantity = 131.319
    ref_notional = 10_000.0
    price = ref_notional / quantity

    cost = round_trip_cost(
        IbkrCommissionModel(),
        "AAPL",
        price=price,
        ref_notional=ref_notional,
    )

    assert cost is not None
    assert cost["fee_rt"] == pytest.approx(1.4)
    assert cost["be_bps"] == pytest.approx(1.4)
    assert cost["fee_rt"] < 2.0


def test_round_trip_cost_minimum_petit_notionnel_coute_plus_cher() -> None:
    # Même symbole, notionnel 10x plus petit : le minimum par ordre mord davantage,
    # donc be_bps est strictement plus élevé. C'est le piège du scalp à petite taille.
    big = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    small = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=1_000.0)
    assert small["be_bps"] > big["be_bps"]


def test_round_trip_cost_sans_commission_est_zero() -> None:
    cost = round_trip_cost(NoCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    assert cost == {
        "fee_rt": 0.0,
        "currency": "USD",
        "be_bps": 0.0,
        "cost_scope": "no_commission_model",
        "cost_estimate_is_all_in": False,
    }


def test_round_trip_cost_prix_invalide_renvoie_none() -> None:
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=0.0, ref_notional=10_000.0) is None
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=0.0) is None


def test_round_trip_cost_modele_absent_renvoie_none() -> None:
    assert round_trip_cost(None, "AAPL", price=200.0, ref_notional=10_000.0) is None


def test_round_trip_cost_symbole_non_modelise_renvoie_none() -> None:
    # Un symbole hors paliers IBKR (modèle "ibkr_unknown") ne doit PAS s'afficher
    # comme gratuit : coût inconnu => None => colonne vide dans le cockpit.
    cost = round_trip_cost(IbkrCommissionModel(), "BTC-USD", price=60000.0, ref_notional=10_000.0)
    assert cost is None


def test_round_trip_cost_taiwan_convertit_le_plafond_usd_en_notional_twd() -> None:
    # Plafond 10k USD, USD/TWD=0.031, prix 1000 TWD : le notionnel natif vaut
    # 322_580.65 TWD (pas 10_000 TWD). Commission 0.08% par jambe, donc
    # 516.13 TWD aller-retour = 16 USD = 16 bps du plafond USD.
    cost = round_trip_cost(
        IbkrCommissionModel(),
        "2330.TW",
        price=1_000.0,
        ref_notional=10_000.0,
        fx_rate=0.031,
    )

    assert cost == {
        "fee_rt": 516.13,
        "currency": "TWD",
        "be_bps": 16.0,
        "cost_scope": "broker_commission_only",
        "cost_estimate_is_all_in": False,
    }


def test_round_trip_cost_calculates_buy_and_sell_instead_of_doubling_buy() -> None:
    class SideAwareModel:
        cost_scope = "broker_plus_known_venue_costs"
        cost_estimate_is_all_in = False

        def calculate(self, order, price):
            amount = 1.0 if order.side == "BUY" else 4.0
            return Commission(amount=amount, currency="USD", model="side_aware")

    cost = round_trip_cost(
        SideAwareModel(),
        "AAPL",
        price=100.0,
        ref_notional=10_000.0,
    )

    assert cost == {
        "fee_rt": 5.0,
        "currency": "USD",
        "be_bps": 5.0,
        "cost_scope": "broker_plus_known_venue_costs",
        "cost_estimate_is_all_in": False,
    }


def test_round_trip_cost_ambiguous_completeness_metadata_stays_fail_closed() -> None:
    class AmbiguousMetadataModel:
        cost_scope = ""
        cost_estimate_is_all_in = "false"

        def calculate(self, order, price):
            return Commission(amount=1.0, currency="USD", model="ambiguous")

    cost = round_trip_cost(
        AmbiguousMetadataModel(),
        "AAPL",
        price=100.0,
        ref_notional=10_000.0,
    )

    assert cost is not None
    assert cost["cost_scope"] == "commission_model_only"
    assert cost["cost_estimate_is_all_in"] is False


@pytest.mark.parametrize(
    "amount",
    [float("nan"), float("inf"), float("-inf"), -0.01],
)
def test_round_trip_cost_invalid_commission_amount_fail_closed(amount: float) -> None:
    class InvalidAmountModel:
        def calculate(self, order, price):
            return Commission(amount=amount, currency="USD", model="invalid_amount")

    assert round_trip_cost(
        InvalidAmountModel(),
        "AAPL",
        price=100.0,
        ref_notional=10_000.0,
    ) is None


def test_round_trip_cost_incompatible_leg_currencies_fail_closed() -> None:
    class IncompatibleCurrencyModel:
        def calculate(self, order, price):
            currency = "USD" if order.side == "BUY" else "EUR"
            return Commission(amount=1.0, currency=currency, model="incompatible")

    assert round_trip_cost(
        IncompatibleCurrencyModel(),
        "AAPL",
        price=100.0,
        ref_notional=10_000.0,
    ) is None


def test_round_trip_cost_third_currency_fail_closed() -> None:
    class ThirdCurrencyModel:
        def calculate(self, order, price):
            return Commission(amount=1.0, currency="EUR", model="third_currency")

    assert round_trip_cost(
        ThirdCurrencyModel(),
        "AAPL",
        price=100.0,
        ref_notional=10_000.0,
    ) is None


@pytest.mark.parametrize("fx_rate", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_round_trip_cost_non_usd_sans_taux_valide_fail_closed(fx_rate: float | None) -> None:
    assert round_trip_cost(
        IbkrCommissionModel(),
        "ASML.AS",
        price=1_000.0,
        ref_notional=10_000.0,
        fx_rate=fx_rate,
    ) is None


def test_round_trip_cost_usd_ne_depend_pas_d_un_taux_fx_externe() -> None:
    without_rate = round_trip_cost(
        IbkrCommissionModel(),
        "AAPL",
        price=200.0,
        ref_notional=10_000.0,
    )
    with_irrelevant_rate = round_trip_cost(
        IbkrCommissionModel(),
        "AAPL",
        price=200.0,
        ref_notional=10_000.0,
        fx_rate=float("nan"),
    )

    assert without_rate == with_irrelevant_rate


def test_round_trip_cost_place_connue_sans_tarif_fail_closed() -> None:
    assert round_trip_cost(
        IbkrCommissionModel(),
        "^TWII",
        price=25_000.0,
        ref_notional=10_000.0,
        fx_rate=0.031,
    ) is None


def test_round_trip_cost_prix_nan_renvoie_none() -> None:
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=float("nan"), ref_notional=10_000.0) is None
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=float("inf"), ref_notional=10_000.0) is None
