"""TDD pour round_trip_cost — estimation déterministe du coût aller-retour.

Le helper traduit un modèle de commission (fonction pure de symbol/qty/price) en
deux signaux décisionnels injectables dans le cockpit : le coût aller-retour en
devise (`fee_rt`) et le seuil de rentabilité en points de base (`be_bps`).
"""

from __future__ import annotations

from trader.execution.broker import (
    IbkrCommissionModel,
    NoCommissionModel,
    round_trip_cost,
)


def test_round_trip_cost_index_cfd_proportionnel() -> None:
    # ^FCHI : 0.0001 du notionnel par jambe, min 1 EUR. À 10k notionnel le taux
    # proportionnel (1.0 EUR) domine le minimum → aller-retour = 2 bps.
    cost = round_trip_cost(IbkrCommissionModel(), "^FCHI", price=8000.0, ref_notional=10_000.0)
    assert cost is not None
    assert cost["currency"] == "EUR"
    assert cost["fee_rt"] == 2.0
    assert cost["be_bps"] == 2.0


def test_round_trip_cost_us_stock_minimum_par_ordre() -> None:
    # AAPL à 200$ sur 10k → 50 actions. 50*0.0035=0.175 < min 0.35 → 0.35/jambe.
    # Aller-retour = 0.70 USD, be = 0.70/10000*1e4 = 0.70 bps.
    cost = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    assert cost is not None
    assert cost["currency"] == "USD"
    assert cost["fee_rt"] == 0.70
    assert cost["be_bps"] == 0.70


def test_round_trip_cost_minimum_petit_notionnel_coute_plus_cher() -> None:
    # Même symbole, notionnel 10x plus petit : le minimum par ordre mord davantage,
    # donc be_bps est strictement plus élevé. C'est le piège du scalp à petite taille.
    big = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    small = round_trip_cost(IbkrCommissionModel(), "AAPL", price=200.0, ref_notional=1_000.0)
    assert small["be_bps"] > big["be_bps"]


def test_round_trip_cost_sans_commission_est_zero() -> None:
    cost = round_trip_cost(NoCommissionModel(), "AAPL", price=200.0, ref_notional=10_000.0)
    assert cost == {"fee_rt": 0.0, "currency": "USD", "be_bps": 0.0}


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


def test_round_trip_cost_prix_nan_renvoie_none() -> None:
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=float("nan"), ref_notional=10_000.0) is None
    assert round_trip_cost(IbkrCommissionModel(), "AAPL", price=float("inf"), ref_notional=10_000.0) is None
